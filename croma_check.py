#!/usr/bin/env python3
"""Croma availability checker.

Croma (croma.com, Tata's electronics retailer) is a national store, not a
hyperlocal quick-commerce platform: catalogue, prices and stock are the same
across India (delivery reaches most serviceable pincodes), so "availability"
here means: is the product listed and in stock at Croma right now.

Croma is a client-rendered SAP-Hybris SPA behind Akamai. Everything useful is
served by its JSON APIs (api.croma.com), but those are bot-gated: a plain
HTTP client or even a top-level browser navigation to the API gets HTTP 403,
and the storefront additionally wraps `window.fetch` with bot instrumentation.

The trick that gets through: drive a persistent headless Chromium (Playwright,
real Chrome channel + light stealth, like the Apple checker), and **stash the
native `fetch` in an init-script before Croma's bot script can wrap it**. A
call issued with that native fetch from inside a croma.com page carries the
browser's valid Akamai cookies + TLS fingerprint, so the search API returns
JSON. (`ctx.request` / bundled headless Chromium are blocked; real Chrome +
in-page native fetch passes.)

Search endpoint (SAP-Hybris "solr" query syntax, `<query>:relevance`):
  GET api.croma.com/searchservices/v1/search
      ?query=<q>:relevance&currentPage=0&fields=FULL&channel=WEB
  -> { products: [ { code, name, manufacturer, price:{value}, mrp:{value},
       stockFlag:[storeCodes...], url, productMessage } ] }
  A non-empty `stockFlag` (stores holding the item) means in stock.

The search catalogue is national (a pincode parameter does not change search
results), and -- crucially -- the search `stockFlag` is only a *national* signal
("some store lists this SKU"), NOT whether it can be bought at a given pincode.
Relying on it makes the app report "available" for items the storefront shows as
"Not Available for your pincode" (e.g. iPhone 16 at 411067). So availability is
resolved in two steps, like the Apple checker:

  1. search  -> candidate SKUs (variants) for the query.
  2. for the best-matching variant, real per-pincode availability via the OMS
     TMS "promise" endpoint the PDP itself calls:
       POST api.croma.com/inventory/oms/v2/tms/details-pwa/
     A returned home-delivery (HDEL) promise line == deliverable & in stock at
     that pincode; an HDEL line under unavailableLine (NOT_ENOUGH_PRODUCT_CHOICES)
     == listed but not deliverable/in stock there. This mirrors the storefront's
     "Delivery at <pin>: Available / Not Available for your pincode" label
     exactly. If the best-matching variant is unavailable, a few other variants
     of the same model are probed so we don't report the whole model as
     unavailable when a different colour/storage is deliverable.

So the matched product resolves to:
  * out_of_stock  -> listed but not deliverable/in stock at this pincode,
  * available     -> deliverable and in stock at this pincode (HDEL promise).

Exposes a thread-safe singleton `client` with .check(lat, lon, query, pincode).
Matching reuses blinkit_check.best_match (accessory-aware).
"""

import asyncio
import threading

from playwright.async_api import async_playwright

import blinkit_check as bk
import config

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")

WARMUP_URL = "https://www.croma.com/"

# When the best-matching variant isn't deliverable to a pincode, probe up to
# this many matching variants (in-stock first) before declaring the model
# unavailable there. Bounds the number of extra TMS calls per query.
MAX_TMS_PROBES = 6

# Init script: save the pristine fetch before Croma's Akamai bot script wraps
# window.fetch, and apply the same light stealth the Apple checker uses.
STEALTH_JS = (
    "window.__nf = window.fetch.bind(window);"
    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    "window.chrome={runtime:{}};"
    "Object.defineProperty(navigator,'languages',{get:()=>['en-IN','en']});"
    "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
)

# In-page search via the stashed native fetch. Returns structured items or a
# {status} on a non-200 so the caller can surface an error/retry.
SEARCH_JS = r"""
async (q) => {
  const f = window.__nf || window.fetch;
  const url = 'https://api.croma.com/searchservices/v1/search?query='
            + encodeURIComponent(q + ':relevance')
            + '&currentPage=0&fields=FULL&channel=WEB';
  let r;
  try {
    r = await f(url, {headers: {accept: 'application/json'}, credentials: 'include'});
  } catch (e) {
    return {status: -1, error: String((e && e.message) || e), items: []};
  }
  if (!r || r.status !== 200) return {status: r ? r.status : 0, items: []};
  let j;
  try { j = await r.json(); } catch (e) { return {status: 200, items: []}; }
  const prods = (j && j.products) || [];
  const items = [];
  for (const p of prods) {
    const price = p.price && p.price.value != null ? p.price.value : null;
    const mrp = p.mrp && p.mrp.value != null ? p.mrp.value : null;
    const hasStock = Array.isArray(p.stockFlag)
      ? p.stockFlag.length > 0 : !!p.stockFlag;
    items.push({
      name: (p.name || '').replace(/\s+/g, ' ').trim(),
      variant: '',
      brand: p.manufacturer || '',
      price: price,
      mrp: (mrp != null && price != null && mrp < price) ? null : mrp,
      inStock: hasStock,
      eta: p.productMessage || '',
      merchant_id: p.code || '',
    });
  }
  return {status: 200, total: (j.pagination || {}).totalResults, items: items};
}
"""

# Real per-pincode availability for one SKU.
#
# The search `stockFlag` is only a *national* signal ("some store lists this
# SKU"); it does NOT mean the item can be bought/delivered to a given pincode.
# The storefront's PDP resolves the real "Delivery at <pin>: Available / Not
# Available for your pincode" label from the OMS TMS "promise" endpoint, which
# checks live inventory sourcing for the delivery address. We POST the same
# payload the PWA sends (home-delivery line HDEL + store/express lines) and read
# the promise: a returned HDEL `promiseLine` == deliverable & in stock at that
# pincode; an HDEL entry under `unavailableLine` with reason
# NOT_ENOUGH_PRODUCT_CHOICES == listed but not deliverable/in stock there. (The
# STOR/SDEL lines come back SOURCING_RULE_NOT_DEFINED for online orders and are
# ignored.) `categoryType` does not affect the result.
#
# Returns {available: true|false|null} (null only on a network/parse error, so
# the caller can fall back to national stock rather than mislabel a real listing).
TMS_URL = "https://api.croma.com/inventory/oms/v2/tms/details-pwa/"

SERVICEABLE_JS = r"""
async ([code, pin]) => {
  const f = window.__nf || window.fetch;
  const line = (ft, id) => ({
    fulfillmentType: ft, mch: '', itemID: code, lineId: id,
    categoryType: 'general',
    reqEndDate: ft === 'HDEL' ? '2500-01-01' : '', reqStartDate: '',
    requiredQty: '1',
    shipToAddress: {company: '', country: '', city: '', mobilePhone: '',
      state: '', zipCode: pin, extn: {irlAddressLine1: '', irlAddressLine2: ''}},
    extn: {widerStoreFlag: 'N'},
  });
  const body = {promise: {allocationRuleID: 'SYSTEM', checkInventory: 'Y',
    organizationCode: 'CROMA', sourcingClassification: 'EC',
    promiseLines: {promiseLine: [line('HDEL', '1'), line('STOR', '2'), line('SDEL', '3')]}}};
  let r;
  try {
    r = await f('https://api.croma.com/inventory/oms/v2/tms/details-pwa/', {
      method: 'POST',
      headers: {'content-type': 'application/json', accept: 'application/json'},
      credentials: 'include', body: JSON.stringify(body)});
  } catch (e) { return {available: null}; }
  if (!r || r.status !== 200) return {available: null};
  let j;
  try { j = await r.json(); } catch (e) { return {available: null}; }
  const so = (j.promise || {}).suggestedOption || {};
  const avail = (((so.option || {}).promiseLines || {}).promiseLine) || [];
  // Home delivery (HDEL) available for this pincode => in stock & deliverable.
  const hdel = avail.some(l => (l.fulfillmentType || '') === 'HDEL' || l.lineId === '1');
  return {available: hdel};
}
"""


class Croma:
    """Persistent headless-browser client, serialized behind a lock.

    One browser + one context/page on croma.com is shared across the process
    (search is national, so location isn't part of the session), and every
    query is issued as an in-page native fetch so it inherits the session's
    Akamai cookies.
    """

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
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ]
        # Croma sits behind Akamai bot manager, which blocks bundled headless
        # Chromium. Real Chrome passes it, so prefer the installed Chrome
        # channel and fall back to Chromium if unavailable (mirrors Apple).
        try:
            self._browser = await self._pw.chromium.launch(
                headless=True, channel="chrome", args=launch_args)
        except Exception:
            self._browser = await self._pw.chromium.launch(
                headless=True, args=launch_args)
        ctx_kwargs = dict(
            user_agent=UA, locale="en-IN", viewport={"width": 1280, "height": 900})
        proxy = config.playwright_proxy()
        if proxy:
            ctx_kwargs["proxy"] = proxy
        self._ctx = await self._browser.new_context(**ctx_kwargs)
        await self._ctx.add_init_script(STEALTH_JS)
        self._page = await self._ctx.new_page()
        # Load a croma.com page once so the Akamai bot token + cookies are
        # established for subsequent in-page fetches to api.croma.com.
        await self._page.goto(WARMUP_URL, wait_until="domcontentloaded", timeout=60000)
        await self._page.wait_for_timeout(3000)

    async def _reset(self):
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        self._pw = self._browser = self._ctx = self._page = None

    async def _query(self, query, pincode):
        await self._ensure()
        # The Akamai token can take a moment to mint on a cold context; retry a
        # blocked (403/challenge) response after a short wait before giving up.
        res = {}
        for attempt in range(3):
            res = await self._page.evaluate(SEARCH_JS, query)
            if res.get("status") == 200:
                break
            await self._page.wait_for_timeout(1500)
            if attempt == 1:
                # refresh the session once in case the token expired
                await self._page.goto(WARMUP_URL, wait_until="domcontentloaded", timeout=60000)
                await self._page.wait_for_timeout(2000)
        else:
            return {"serviceable": None, "items": [],
                    "error": f"search blocked (status={res.get('status')})"}

        items = res.get("items", [])
        m = bk.best_match(query, items) if items else None
        if not (m and pincode):
            return {"serviceable": True, "items": items, "match": m}

        # Real per-pincode availability comes from the TMS promise, not the
        # national stockFlag. Probe the best match first; if it isn't deliverable
        # here, try a few other variants of the same model (in-stock ones first)
        # so a single out-of-stock colour doesn't hide a deliverable variant.
        candidates = _ranked_variants(query, items, m)
        chosen, saw_error = None, False
        for cand in candidates[:MAX_TMS_PROBES]:
            code = cand.get("merchant_id")
            if not code:
                continue
            svc = await self._page.evaluate(SERVICEABLE_JS, [str(code), str(pincode)])
            avail = svc.get("available")
            cand["pinAvailable"] = avail
            if avail is True:
                chosen = cand
                break
            if avail is None:
                saw_error = True
        # If nothing was deliverable, keep the best match and mark why: False =>
        # confirmed not available at this pincode; None => TMS errored, so
        # match_row falls back to national stock instead of hiding a real listing.
        resolved = chosen or m
        if chosen is None:
            resolved["pinAvailable"] = None if saw_error else False
        return {"serviceable": True, "items": items, "match": resolved}

    def check(self, lat, lon, query, pincode=None):
        """Return {serviceable, items:[...], match:{...,pinAvailable}} (match is the
        query's best variant with its per-pincode TMS availability resolved)."""
        with self._lock:
            try:
                return self._run(self._query(query, pincode))
            except Exception as e:
                try:
                    self._run(self._reset())
                except Exception:
                    pass
                return {"serviceable": None, "items": [], "error": str(e)}


client = Croma()


def _ranked_variants(query, items, best):
    """Variants of the queried model to probe for per-pincode availability.

    Returns the best match first, then the other search items that also match
    every query token (same accessory-aware filter best_match uses), with
    nationally in-stock ones prioritised so we most quickly find a deliverable
    variant. Keeps the extra TMS probes focused on the right model.
    """
    q_tokens = [t for t in bk._norm(query).split() if t]
    query_is_accessory = any(t in bk.ACCESSORY_WORDS for t in q_tokens)

    def matches(p):
        hay = set(bk._norm(
            (p.get("name", "") or "") + " " + (p.get("variant", "") or "") + " "
            + (p.get("brand", "") or "")).split())
        if not all(t in hay for t in q_tokens):
            return False
        if not query_is_accessory and (hay & bk.ACCESSORY_WORDS):
            return False
        return True

    others = [p for p in items
              if p is not best and p.get("merchant_id") and matches(p)]
    # Try smaller-storage variants first (keep results on the base capacity the
    # user expects, e.g. 128GB colours) and in-stock ones ahead of the rest.
    others.sort(key=lambda p: (bk._capacity_gb(p), 0 if p.get("inStock") else 1))
    ranked = ([best] if best and best.get("merchant_id") else []) + others
    return ranked


def match_row(query, result):
    """Normalize a check() result into a row like the other platforms."""
    if result.get("serviceable") is None:
        return {"status": "error", "detail": result.get("error", "")}
    # Prefer the variant _query already resolved (carries the per-pincode TMS
    # verdict); fall back to a fresh best_match for callers that didn't pass one.
    m = result.get("match") or bk.best_match(query, result.get("items", []))
    if not m:
        return {"status": "not_found"}

    # Real per-pincode availability from the TMS promise (see SERVICEABLE_JS):
    #   True  -> deliverable & in stock at this pincode,
    #   False -> listed but not available for this pincode,
    #   None  -> TMS errored, so fall back to the national stockFlag rather than
    #            wrongly hiding a real listing.
    pin_avail = m.get("pinAvailable")
    if pin_avail is None:
        available = bool(m.get("inStock"))
        eta = m.get("eta") or ""
    else:
        available = bool(pin_avail)
        eta = (m.get("eta") or "") if available else "Not available for this pincode"

    return {
        "status": "available" if available else "out_of_stock",
        "available": "yes" if available else "no",
        "name": m.get("name"), "variant": m.get("variant", ""), "brand": m.get("brand", ""),
        "price": m.get("price"), "mrp": m.get("mrp"), "inventory": "",
        "eta": eta, "merchant_id": m.get("merchant_id", ""),
    }


if __name__ == "__main__":
    for lat, lon, pin, label in [
        (19.0760, 72.8777, "400049", "Mumbai"),
        (34.1526, 77.5771, "194101", "Leh-remote"),
    ]:
        for q in ["iphone 15", "samsung 55 inch tv", "boat airdopes", "nonexistentzzz"]:
            r = client.check(lat, lon, q, pin)
            row = match_row(q, r)
            print(f"{label:11} | {q:20} | items={len(r.get('items', [])):2} | "
                  f"{row.get('status'):15} | {(row.get('name') or '')[:36]:36} "
                  f"Rs{row.get('price')}")
