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
results), so availability is resolved in two steps, like the Apple checker:

  1. search  -> candidate SKUs + national stock (stockFlag non-empty).
  2. for the best-matching SKU, per-pincode deliverability via
     GET api.croma.com/sku/v1/essentialcombo?pinCode=<pin>&ProductSkus=<code>
     which returns HTTP 200 when the item can be delivered to that pincode and
     HTTP 400 {"Message":"Unavailable in IC"} when it cannot (this mirrors the
     storefront's per-product "Not Available at pincode <city>, <pin>" label,
     and is product-specific: e.g. large TVs are undeliverable to Leh/Andaman
     while phones/earbuds may still be). The TMS "promise" endpoint is not used
     because it needs cart/session state and reports everything unavailable
     from a bare context.

So the matched product resolves to:
  * not_serviceable  -> in the catalogue but not deliverable to this pincode,
  * out_of_stock     -> deliverable but no national stock,
  * available        -> deliverable and in stock.

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

# Per-pincode deliverability for one SKU. HTTP 200 => the item can be delivered
# to the pincode; HTTP 400 {"Message":"Unavailable in IC"} => it cannot. Returns
# {serviceable: true|false|null} (null on an unexpected error, so the caller can
# fall back to national stock rather than wrongly reporting "not serviceable").
SERVICEABLE_JS = r"""
async ([code, pin]) => {
  const f = window.__nf || window.fetch;
  const url = 'https://api.croma.com/sku/v1/essentialcombo?pinCode='
            + encodeURIComponent(pin) + '&ProductSkus=' + encodeURIComponent(code);
  let r;
  try {
    r = await f(url, {headers: {accept: 'application/json'}, credentials: 'include'});
  } catch (e) { return {serviceable: null}; }
  if (r.status === 200) return {serviceable: true};
  if (r.status === 400) {
    let t = '';
    try { t = await r.text(); } catch (e) {}
    // Only treat the explicit "Unavailable in IC" as not-serviceable; other
    // 400s are ambiguous and shouldn't mask a real listing.
    if (/unavailable in ic/i.test(t)) return {serviceable: false};
    return {serviceable: null};
  }
  return {serviceable: null};
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
        # Resolve per-pincode deliverability for the single best match (one extra
        # call, like Apple), and annotate that item so match_row can reflect it.
        m = bk.best_match(query, items) if items else None
        if m and m.get("merchant_id") and pincode:
            svc = await self._page.evaluate(
                SERVICEABLE_JS, [str(m["merchant_id"]), str(pincode)])
            m["pinServiceable"] = svc.get("serviceable")
        return {"serviceable": True, "items": items}

    def check(self, lat, lon, query, pincode=None):
        """Return {serviceable, items:[{name,variant,brand,price,mrp,inStock,eta,merchant_id,pinServiceable}]}."""
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


def match_row(query, result):
    """Normalize a check() result into a row like the other platforms."""
    if result.get("serviceable") is None:
        return {"status": "error", "detail": result.get("error", "")}
    items = result.get("items", [])
    m = bk.best_match(query, items)
    if not m:
        return {"status": "not_found"}
    # Deliverable to this pincode? (None == couldn't resolve -> fall back to
    # national stock so we never wrongly hide a real listing.)
    pin_svc = m.get("pinServiceable")
    if pin_svc is False:
        return {
            "status": "not_serviceable", "available": "no",
            "name": m.get("name"), "variant": m.get("variant", ""), "brand": m.get("brand", ""),
            "price": m.get("price"), "mrp": m.get("mrp"), "inventory": "",
            "eta": "Not deliverable to this pincode",
            "merchant_id": m.get("merchant_id", ""),
        }
    return {
        "status": "available" if m.get("inStock") else "out_of_stock",
        "available": "yes" if m.get("inStock") else "no",
        "name": m.get("name"), "variant": m.get("variant", ""), "brand": m.get("brand", ""),
        "price": m.get("price"), "mrp": m.get("mrp"), "inventory": "",
        "eta": m.get("eta") or "", "merchant_id": m.get("merchant_id", ""),
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
