#!/usr/bin/env python3
"""Swiggy Instamart availability checker.

Swiggy Instamart sits behind AWS WAF and only serves its search API from a
real browser session that has (a) solved the WAF challenge and (b) loaded the
search page (which sets the sid/tid/deviceId cookies). So we drive a persistent
headless Chromium (Playwright) and issue the API calls from inside the page.

Flow per location:
  lat/lng --> /api/instamart/home/v2   (extract storeId; none => not serviceable)
          --> /api/instamart/search/mart/v2?query=&storeId=  (product cards)

Exposes a thread-safe singleton `client` with .check(lat, lon, query).
Matching reuses blinkit_check.best_match (accessory-aware).
"""

import asyncio
import threading

from curl_cffi import requests as cffi_requests
from playwright.async_api import async_playwright

import blinkit_check as bk
import config

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")

# curl_cffi TLS/HTTP fingerprint to impersonate for the search request. Swiggy's
# CloudFront guards /search/v2 with a "JA4-ratelimit-instamart" limiter that 403s
# the headless browser's distinctive fingerprint; issuing the call via curl_cffi
# with a mainstream Chrome JA3/JA4 (matching our UA's Chrome 142) sidesteps it.
IMPERSONATE = "chrome142"

STORE_JS = r"""
async ([lat, lng]) => {
  // Returns rich status so the caller can tell three cases apart:
  //   * storeId present            -> serviceable
  //   * ok JSON, no storeId        -> genuinely NOT serviceable (swiggyNotPresent)
  //   * 202 / empty / non-JSON     -> stale WAF session, needs re-priming
  try {
    const r = await fetch(`/api/instamart/home/v2?lat=${lat}&lng=${lng}`, {headers:{accept:'application/json'}});
    const t = await r.text();
    const m = t.match(/storeId=(\d+)/);
    let ok = false;
    try { JSON.parse(t); ok = true; } catch (e) {}
    // Swiggy explicitly flags out-of-coverage areas with this marker.
    const notPresent = /swiggyNotPresent"?\s*:\s*true/.test(t);
    return {status: r.status, storeId: m ? m[1] : null, ok, notPresent, empty: !t};
  } catch (e) { return {status: 0, storeId: null, ok: false, notPresent: false, empty: true}; }
}
"""

SEARCH_JS = r"""
async ([storeId, q]) => {
  // Real Instamart results endpoint (POST). The GET search/mart/v2 only returns
  // a generic discovery feed; this one honours the query.
  const u = `/api/instamart/search/v2?offset=0&ageConsent=false&voiceSearchTrackingId=`
          + `&storeId=${storeId}&primaryStoreId=${storeId}&secondaryStoreId=`;
  const body = JSON.stringify({
    facets: [], sortAttribute: '', query: q, search_results_offset: '0',
    page_type: 'INSTAMART_AUTO_SUGGEST_PAGE', is_pre_search_tag: false,
  });
  const r = await fetch(u, {method: 'POST',
    headers: {'content-type': 'application/json', accept: 'application/json'}, body});
  if (r.status !== 200) return {status: r.status, items: null};
  const txt = await r.text();
  if (!txt) return {status: 200, items: null, empty: true};
  let j; try { j = JSON.parse(txt); } catch (e) { return {status: 200, items: null, empty: true}; }
  const items = [];
  (function walk(o){
    if (!o || typeof o !== 'object') return;
    if (o.displayName && Array.isArray(o.variations) && o.variations[0]) {
      const v = o.variations[0];
      const p = v.price || {};
      items.push({
        name: o.displayName,
        brand: o.brand || '',
        inStock: !!(o.inStock),
        variant: v.quantityDescription || '',
        mrp: (p.mrp && p.mrp.units) ? Number(p.mrp.units) : null,
        price: (p.offerPrice && p.offerPrice.units) ? Number(p.offerPrice.units)
               : ((p.mrp && p.mrp.units) ? Number(p.mrp.units) : null),
        eta: (v.sla && (v.sla.deliveryTime || v.sla.slaString)) || ''
      });
    }
    for (const k in o) walk(o[k]);
  })(j);
  const seen = new Set(); const out = [];
  for (const it of items) { const k = it.name + '|' + it.variant; if (!seen.has(k)) { seen.add(k); out.push(it); } }
  return {status: 200, items: out};
}
"""

# Endpoint + request body for the search POST, shared by the curl_cffi path.
SEARCH_URL = ("https://www.swiggy.com/api/instamart/search/v2"
              "?offset=0&ageConsent=false&voiceSearchTrackingId="
              "&storeId={store}&primaryStoreId={store}&secondaryStoreId=")


def _search_body(query):
    return {
        "facets": [], "sortAttribute": "", "query": query,
        "search_results_offset": "0",
        "page_type": "INSTAMART_AUTO_SUGGEST_PAGE", "is_pre_search_tag": False,
    }


def _num(v):
    """Coerce a price 'units' value (str/int) to int, or None."""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def extract_items(j):
    """Walk Swiggy's search JSON into flat product rows.

    Mirrors SEARCH_JS's walk exactly so the curl_cffi path yields identical rows
    to the in-page fetch: one entry per (displayName, first-variation) with the
    same name/brand/inStock/variant/mrp/price/eta fields, de-duplicated on
    name|variant.
    """
    items, seen = [], set()

    def walk(o):
        if isinstance(o, dict):
            variations = o.get("variations")
            if o.get("displayName") and isinstance(variations, list) and variations \
                    and isinstance(variations[0], dict):
                v = variations[0]
                p = v.get("price") or {}
                mrp = _num((p.get("mrp") or {}).get("units"))
                offer = _num((p.get("offerPrice") or {}).get("units"))
                sla = v.get("sla") or {}
                row = {
                    "name": o.get("displayName"),
                    "brand": o.get("brand") or "",
                    "inStock": bool(o.get("inStock")),
                    "variant": v.get("quantityDescription") or "",
                    "mrp": mrp,
                    "price": offer if offer is not None else mrp,
                    "eta": sla.get("deliveryTime") or sla.get("slaString") or "",
                }
                key = f"{row['name']}|{row['variant']}"
                if key not in seen:
                    seen.add(key)
                    items.append(row)
            for val in o.values():
                walk(val)
        elif isinstance(o, list):
            for val in o:
                walk(val)

    walk(j)
    return items


class SwiggyInstamart:
    """Persistent headless-browser client, serialized behind a lock."""

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._lock = threading.Lock()
        self._pw = self._browser = self._ctx = self._page = None

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    async def _ensure(self):
        if self._page is not None:
            return
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        ctx_kwargs = dict(
            user_agent=UA, locale="en-US", viewport={"width": 1280, "height": 800})
        proxy = config.playwright_proxy()
        if proxy:
            ctx_kwargs["proxy"] = proxy
        self._ctx = await self._browser.new_context(**ctx_kwargs)
        # Bandwidth saver: drop heavy assets we never read (images/media/fonts).
        # Keeps scripts/xhr/documents so the WAF challenge + API calls still work.
        # Slashes data usage ~5-10x — important when egress is a metered
        # residential/mobile proxy (e.g. a phone hotspot / JioFi).
        await self._ctx.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "media", "font")
            else route.continue_(),
        )
        self._page = await self._ctx.new_page()
        await self._prime()

    # A location that is always serviceable, used purely to confirm the WAF
    # challenge has actually been solved after (re)priming.
    PROBE_LAT, PROBE_LON = "19.0760", "72.8777"  # Mumbai

    async def _prime(self, verify=True):
        # Solve WAF + set instamart session cookies (sid/tid/deviceId). Swiggy's
        # AWS WAF sometimes needs a few seconds (and occasionally a second pass)
        # before the API stops returning HTTP 202 with an empty body, so we
        # verify with a known-good probe and re-navigate if it isn't ready yet.
        for _ in range(3):
            await self._page.goto("https://www.swiggy.com/instamart",
                                  wait_until="domcontentloaded", timeout=45000)
            await self._page.wait_for_timeout(3500)
            await self._page.goto("https://www.swiggy.com/instamart/search?custom_back=true",
                                  wait_until="domcontentloaded", timeout=45000)
            await self._page.wait_for_timeout(2500)
            if not verify:
                return True
            # Poll the home API until it returns real JSON (challenge solved).
            for _ in range(4):
                info = await self._page.evaluate(STORE_JS, [self.PROBE_LAT, self.PROBE_LON])
                if info.get("ok"):
                    return True
                await self._page.wait_for_timeout(1500)
        return False

    async def _reset(self):
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        self._pw = self._browser = self._ctx = self._page = None

    async def _store_lookup(self, lat, lon):
        """Resolve a storeId, re-priming once if the WAF session looks stale.

        Returns (storeId|None, definitive) where `definitive` is True only when we
        trust the answer: a storeId was found, or Swiggy explicitly reported the
        area as out of coverage. A stale WAF session (202 / empty / non-JSON) is
        NOT definitive, so the caller can surface an error instead of a bogus
        "Unserviceable".
        """
        # Try a few times, re-priming (verified) between attempts. Because
        # _prime() confirms the session is live before returning, a storeId or
        # the explicit not-present marker after a fresh prime is trustworthy.
        for attempt in range(3):
            info = await self._page.evaluate(STORE_JS, [str(lat), str(lon)])
            store = info.get("storeId")
            good = bool(info.get("ok"))  # real JSON response, not a WAF challenge
            # storeId -> serviceable; clean JSON with the not-present marker ->
            # truly unserviceable. Both are definitive and need no retry.
            if store:
                return store, True
            if good and info.get("notPresent"):
                return None, True
            # Otherwise the session is likely stale (Swiggy returns HTTP 202 with
            # an empty body while the AWS WAF challenge is pending). Re-prime and
            # retry so an expired session doesn't make every location look dead.
            if attempt < 2:
                await self._prime()
        # Still no clean answer -> treat as an error, not "not serviceable".
        return None, False

    def _search_bad(self, res):
        """A search response we shouldn't trust as a real 'no results'.

        A serviceable store returning zero product cards is almost always a
        stale/rate-limited session rather than a genuine empty catalogue, so we
        treat it as bad and let the caller re-prime and retry.
        """
        items = res.get("items")
        return (res.get("status") != 200 or res.get("empty")
                or items is None or len(items) == 0)

    async def _search_cffi(self, store, query):
        """Run the search POST via curl_cffi impersonating Chrome.

        Swiggy 403s the headless browser's TLS/HTTP fingerprint on /search/v2
        (CloudFront "JA4-ratelimit-instamart"). We reuse the cookies the primed
        browser already holds (WAF token + instamart session), exit through the
        same residential proxy, and present a mainstream Chrome JA3/JA4 so the
        request looks like an ordinary consumer's. Returns the same shape as the
        in-page SEARCH_JS ({status, items, [empty]}).
        """
        # Cookies the browser earned during priming (aws-waf-token, sid, tid,
        # deviceId, ...). Scope to Swiggy so we don't leak unrelated cookies.
        ck_list = await self._ctx.cookies()
        cookies = {c["name"]: c["value"] for c in ck_list
                   if "swiggy" in (c.get("domain") or "")}
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "origin": "https://www.swiggy.com",
            "referer": "https://www.swiggy.com/instamart/search?custom_back=true",
            "user-agent": UA,
        }
        url = SEARCH_URL.format(store=store)
        body = _search_body(query)
        proxies = config.curl_proxies()

        def _do():
            try:
                r = cffi_requests.post(
                    url, json=body, headers=headers, cookies=cookies,
                    impersonate=IMPERSONATE, proxies=proxies, timeout=30,
                )
            except Exception as e:  # network/proxy error -> transient, retryable
                return {"status": 0, "items": None, "empty": True, "err": str(e)}
            if r.status_code != 200:
                return {"status": r.status_code, "items": None}
            try:
                j = r.json()
            except Exception:
                return {"status": 200, "items": None, "empty": True}
            return {"status": 200, "items": extract_items(j)}

        # curl_cffi is synchronous; run it off the event loop so we don't block
        # the client's single loop thread.
        return await asyncio.get_event_loop().run_in_executor(None, _do)

    async def _query(self, lat, lon, query):
        await self._ensure()
        store, definitive = await self._store_lookup(lat, lon)
        if not store:
            if definitive:
                return {"serviceable": False, "store": None, "items": []}
            # Stale/blocked session we couldn't recover -> report as an error so
            # the UI shows a transient failure rather than a false "Unserviceable".
            return {"serviceable": None, "store": None, "items": [],
                    "error": "swiggy session/WAF challenge not solved"}
        res = await self._search_cffi(store, query)
        if self._search_bad(res):
            # Likely a stale session (WAF cookies expired) -> re-prime to refresh
            # them, re-resolve the store (it can change after a fresh session),
            # and retry once via curl_cffi before trusting the empty result.
            await self._prime()
            store2, _ = await self._store_lookup(lat, lon)
            store = store2 or store
            res = await self._search_cffi(store, query)
        return {"serviceable": True, "store": store, "items": res.get("items") or []}

    def check(self, lat, lon, query):
        """Return dict: {serviceable, store, items:[{name,brand,variant,inStock,mrp,price,eta}]}."""
        with self._lock:
            try:
                return self._run(self._query(lat, lon, query))
            except Exception as e:
                # hard failure -> reset browser so next call re-initialises
                try:
                    self._run(self._reset())
                except Exception:
                    pass
                return {"serviceable": None, "store": None, "items": [], "error": str(e)}


client = SwiggyInstamart()


def match_row(query, result):
    """Turn a raw check() result into a normalized row like the Blinkit checker."""
    if result.get("serviceable") is None:
        return {"status": f"error", "detail": result.get("error", "")}
    if result.get("serviceable") is False:
        return {"status": "not_serviceable"}
    items = result.get("items", [])
    # adapt to blinkit_check.best_match (uses name/variant/brand)
    m = bk.best_match(query, items)
    if not m:
        return {"status": "not_found", "store": result.get("store")}
    return {
        "status": "available" if m.get("inStock") else "out_of_stock",
        "available": "yes" if m.get("inStock") else "no",
        "name": m.get("name"), "variant": m.get("variant"), "brand": m.get("brand"),
        "price": m.get("price"), "mrp": m.get("mrp"), "inventory": "",
        "eta": m.get("eta"), "merchant_id": result.get("store"),
    }


if __name__ == "__main__":
    # quick manual test
    for lat, lon, label in [(18.536, 73.893, "Pune KP"), (28.6139, 77.209, "Delhi CP")]:
        r = client.check(lat, lon, "amul milk")
        print(label, "store:", r.get("store"), "items:", len(r.get("items", [])),
              "->", match_row("amul milk", r))
