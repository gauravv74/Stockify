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

from playwright.async_api import async_playwright

import blinkit_check as bk

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")

STORE_JS = r"""
async ([lat, lng]) => {
  try {
    const r = await fetch(`/api/instamart/home/v2?lat=${lat}&lng=${lng}`, {headers:{accept:'application/json'}});
    const t = await r.text();
    const m = t.match(/storeId=(\d+)/);
    return m ? m[1] : null;
  } catch (e) { return null; }
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
        self._ctx = await self._browser.new_context(
            user_agent=UA, locale="en-US", viewport={"width": 1280, "height": 800})
        self._page = await self._ctx.new_page()
        await self._prime()

    async def _prime(self):
        # Solve WAF + set instamart session cookies (sid/tid/deviceId).
        await self._page.goto("https://www.swiggy.com/instamart",
                              wait_until="domcontentloaded", timeout=45000)
        await self._page.wait_for_timeout(3500)
        await self._page.goto("https://www.swiggy.com/instamart/search?custom_back=true",
                              wait_until="domcontentloaded", timeout=45000)
        await self._page.wait_for_timeout(2500)

    async def _reset(self):
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        self._pw = self._browser = self._ctx = self._page = None

    async def _query(self, lat, lon, query):
        await self._ensure()
        store = await self._page.evaluate(STORE_JS, [str(lat), str(lon)])
        if not store:
            return {"serviceable": False, "store": None, "items": []}
        res = await self._page.evaluate(SEARCH_JS, [store, query])
        if res.get("empty") or res.get("items") is None:
            # session/WAF likely stale -> re-prime once and retry
            await self._prime()
            res = await self._page.evaluate(SEARCH_JS, [store, query])
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
