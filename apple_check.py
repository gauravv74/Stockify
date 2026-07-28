#!/usr/bin/env python3
"""Apple India store availability checker.

Apple (apple.com/in) is a national retailer, not a hyperlocal quick-commerce
platform: prices are the same everywhere and most items ship across India. The
only location-specific signals Apple exposes are (a) the estimated home-delivery
date and (b) in-store pickup availability at Apple Stores / authorised resellers
near a pincode. So "availability" here means: is the product orderable online
and/or collectable near this pincode.

Two Apple endpoints drive this, both served only to a real browser session that
carries Akamai bot cookies (a plain HTTP client is challenged with HTTP 541), so
we drive a persistent headless Chromium (Playwright) and issue the calls from
inside a page loaded on apple.com/in:

  * search:   GET /in/search/<query>?tab=accessories  -> the store search page
              embeds product JSON (title, partNumber, MRP). We parse concrete,
              purchasable SKUs (AirPods, Watch, Pencil, cases, base configs...).
  * fulfilment: GET /in/shop/fulfillment-messages?parts.0=<PART>&location=<pin>
              -> per-pincode home-delivery quote + nearby store pickup quotes.

Location is a request parameter (the pincode), not GPS, so a single browser
context is reused across all locations. Only the best-matching product's
fulfilment is fetched per query to keep it to one extra call.

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

# Warm-up must be a /in/shop/ page: the Akamai token minted there is what
# authorises the shop XHR endpoints (a bare homepage load leaves the
# fulfillment-messages call blocked with HTTP 541).
WARMUP_URL = "https://www.apple.com/in/shop/buy-airpods"

# Fetch the store search page and pull out purchasable SKUs. The results are
# embedded in the HTML as JSON blocks shaped like:
#   {"title":"...","link":{...},...,"partNumber":"MXP93HN/A",...,
#    "productPrice":{"priceCurrent":"MRP ₹17900.00 (Incl. of all taxes)"}}
SEARCH_JS = r"""
async (q) => {
  const r = await fetch('/in/search/' + encodeURIComponent(q) + '?tab=accessories',
                        {headers: {accept: 'text/html'}});
  if (!r.ok) return {status: r.status, items: [], links: []};
  const t = await r.text();
  const items = []; const seen = new Set();
  const re = /"title":"((?:[^"\\]|\\.)*?)","link":\{[\s\S]*?"partNumber":"([^"]+)"[\s\S]*?"priceCurrent":"([^"]*)"/g;
  let m;
  while ((m = re.exec(t)) !== null) {
    const name = m[1]
      .replace(/\\u([0-9a-fA-F]{4})/g, (_, h) => String.fromCharCode(parseInt(h, 16)))
      .replace(/\\(.)/g, '$1').trim();
    const part = m[2];
    const pm = m[3].match(/₹\s?([\d,]+(?:\.\d+)?)/);
    const price = pm ? Number(pm[1].replace(/,/g, '')) : null;
    if (seen.has(part)) continue; seen.add(part);
    items.push({name, partNumber: part, price});
    if (items.length >= 20) break;
  }
  // A device (iPhone/iPad/Mac/Watch) is a configurable buy-page product, not a
  // part-number SKU in the accessories tab above. The default (all) search page
  // does list buy-<family> links, so fetch it once to surface them and let the
  // caller pull the real device variants from the matching buy page.
  let links = [];
  try {
    const r2 = await fetch('/in/search/' + encodeURIComponent(q),
                           {headers: {accept: 'text/html'}});
    if (r2.ok) {
      const t2 = await r2.text();
      links = [...new Set(
        [...t2.matchAll(/href="([^"]*\/shop\/buy-[^"?#]*)"/g)].map(x => x[1]))];
    }
  } catch (e) { /* keep accessory results even if link discovery fails */ }
  return {status: 200, items, links};
}
"""

# Real device variants from a buy-<family> page (e.g. /in/shop/buy-iphone/
# iphone-17). The page embeds a clean products array
#   "products":[{"sku":"MG6L4","partNumber":"MG6L4HN/A",
#                "price":{"fullPrice":82900.00},"category":"iphone",
#                "name":"iPhone 17 256GB Mist Blue"}, ...]
# which gives the actual purchasable phone SKUs (with storage + colour + price),
# so a query like "iphone 17" resolves to the phone, not an AppleCare+ plan or a
# case that merely mentions "iPhone 17".
DEVICE_JS = r"""
async (url) => {
  const r = await fetch(url, {headers: {accept: 'text/html'}});
  if (!r.ok) return {status: r.status, items: []};
  const t = await r.text();
  const items = []; const seen = new Set();
  const re = /"partNumber":"([^"]+)","price":\{"fullPrice":([0-9.]+)\},"category":"[^"]*","name":"([^"]+)"/g;
  let m;
  while ((m = re.exec(t)) !== null) {
    const part = m[1];
    if (seen.has(part)) continue; seen.add(part);
    const name = m[3].replace(/\u00a0/g, ' ').replace(/\\(.)/g, '$1')
                     .replace(/\s+/g, ' ').trim();
    items.push({name, partNumber: part, price: Number(m[2])});
    if (items.length >= 60) break;
  }
  return {status: 200, items};
}
"""

# For one part number + pincode, resolve home-delivery quote and nearby store
# pickup availability.
FULFILL_JS = r"""
async ([part, loc]) => {
  const u = '/in/shop/fulfillment-messages?fae=true&little=false&mts.0=regular'
          + '&parts.0=' + encodeURIComponent(part) + '&location=' + encodeURIComponent(loc);
  const r = await fetch(u, {headers: {accept: 'application/json'}});
  if (r.status !== 200) return {status: r.status};
  const j = await r.json();
  const c = (j.body && j.body.content) || {};
  const dm = c.deliveryMessage || {};
  const pm = c.pickupMessage || {};
  const pdm = dm[part] || {};
  const opts = (pdm.regular && pdm.regular.deliveryOptionMessages) || [];
  const delivery = opts.length ? (opts[0].displayName || '') : '';
  const deliveryEligible = !!dm.deliveryEligible;
  const stores = (pm.stores || []).map(s => {
    const pa = Object.values(s.partsAvailability || {})[0] || {};
    const quote = pa.pickupSearchQuote || '';
    const available = (pa.pickupDisplay === 'available') || /available/i.test(quote);
    return {name: s.storeName || '', city: s.city || '', quote, available};
  });
  const pickup = stores.find(s => s.available) || stores[0] || null;
  return {status: 200, delivery, deliveryEligible, pickup, storeCount: stores.length};
}
"""


class Apple:
    """Persistent headless-browser client, serialized behind a lock.

    One browser + one context/page on apple.com/in is shared across the process
    (location is a query parameter, not GPS), and every call is issued as an
    in-page fetch so it inherits the session's Akamai cookies.
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
        # Apple's shop XHRs sit behind Akamai bot manager, which reliably blocks
        # bundled headless Chromium (HTTP 541). Real Chrome passes it, so prefer
        # the installed Chrome channel and fall back to Chromium if unavailable.
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
        await self._ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "window.chrome={runtime:{}};"
            "Object.defineProperty(navigator,'languages',{get:()=>['en-IN','en']});"
            "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});")
        self._page = await self._ctx.new_page()
        # Load an India store page once so the Akamai bot token + store-locale
        # cookies (as_sfa=in) are established for subsequent in-page fetches.
        await self._page.goto(WARMUP_URL, wait_until="domcontentloaded", timeout=60000)
        await self._page.wait_for_timeout(2500)

    async def _reset(self):
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        self._pw = self._browser = self._ctx = self._page = None

    async def _query(self, query, pincode):
        await self._ensure()
        res = await self._page.evaluate(SEARCH_JS, query)
        items = res.get("items") or []
        # For a device query, pull the real phone/tablet/Mac variants from the
        # matching buy-<family> page and put them first, so best_match picks the
        # actual device over accessories / AppleCare+ (which is all the store
        # search returns for e.g. "iphone 17").
        buy_url = _pick_buy_url(query, res.get("links") or [])
        if buy_url:
            d = await self._page.evaluate(DEVICE_JS, buy_url)
            dev_items = d.get("items") or []
            for it in dev_items:
                it["is_device"] = True     # a real buy-page SKU, not an add-on
            if dev_items:
                items = dev_items + items
        for it in items:
            it["variant"] = ""
            it["brand"] = "Apple"
            it["mrp"] = None
            it["inStock"] = True          # listed in the store => orderable
            it["eta"] = ""

        # Enrich the single best match with per-pincode delivery + pickup.
        m = bk.best_match(query, items) if items else None
        if m and m.get("partNumber") and pincode:
            f = None
            for _ in range(3):
                f = await self._page.evaluate(FULFILL_JS, [m["partNumber"], str(pincode)])
                if f and f.get("status") == 200:
                    break
                await self._page.wait_for_timeout(700)
            if f and f.get("status") == 200:
                pk = f.get("pickup") or {}
                online = bool(f.get("deliveryEligible")) or bool(f.get("delivery"))
                pickup_ok = bool(pk.get("available"))
                m["inStock"] = online or pickup_ok
                bits = []
                if f.get("delivery"):
                    bits.append("Delivers " + f["delivery"])
                elif online:
                    bits.append("Available online")
                if pk.get("name"):
                    q = f" ({pk['quote']})" if pk.get("quote") else ""
                    bits.append("Pickup: " + pk["name"] + q)
                m["eta"] = " \u00b7 ".join(bits)
                m["merchant_id"] = pk.get("name") or ""

        return {"serviceable": True, "items": items}

    def check(self, lat, lon, query, pincode=None):
        """Return {serviceable, items:[{name,variant,brand,price,mrp,inStock,eta,partNumber}]}."""
        with self._lock:
            try:
                return self._run(self._query(query, pincode))
            except Exception as e:
                try:
                    self._run(self._reset())
                except Exception:
                    pass
                return {"serviceable": None, "items": [], "error": str(e)}


client = Apple()


def _pick_buy_url(query, links):
    """Choose the buy-<family> page that best matches a device query.

    Compares the query against each link's final path segment (the model slug,
    e.g. "iphone-17"), keeping only slugs that are compatible with the query --
    one token set a subset of the other -- so "iphone 17" matches iphone-17 (and
    not iphone-16), while "iphone 17 pro max" still matches the iphone-17-pro
    page (which lists both Pro and Pro Max SKUs). Among those, the link sharing
    the most tokens wins, then the tightest fit, so "iphone 17" -> iphone-17
    rather than iphone-17-pro. Returns None when no buy link is compatible (e.g.
    a pure accessory search), leaving the original store-search behaviour intact.
    """
    q = set(t for t in bk._norm(query).split() if t)
    if not q:
        return None
    best, best_key = None, None
    for url in links:
        seg = url.rstrip("/").split("/")[-1]
        toks = set(bk._norm(seg).split())
        if not toks or not (q <= toks or toks <= q):
            continue
        key = (-len(q & toks), len(toks ^ q), len(seg))
        if best_key is None or key < best_key:
            best, best_key = url, key
    return best


def match_row(query, result):
    """Normalize a check() result into a row like the other platforms."""
    if result.get("serviceable") is None:
        return {"status": "error", "detail": result.get("error", "")}
    items = result.get("items", [])
    m = bk.best_match(query, items)
    if not m:
        return {"status": "not_found"}
    return {
        "status": "available" if m.get("inStock") else "out_of_stock",
        "available": "yes" if m.get("inStock") else "no",
        "name": m.get("name"), "variant": m.get("variant", ""), "brand": m.get("brand", "Apple"),
        "price": m.get("price"), "mrp": m.get("mrp"), "inventory": "",
        "eta": m.get("eta") or "", "merchant_id": m.get("merchant_id", ""),
    }


if __name__ == "__main__":
    for lat, lon, pin, label in [
        (19.0760, 72.8777, "400001", "Mumbai"),
        (28.6139, 77.2090, "110001", "Delhi"),
        (34.1526, 77.5771, "194101", "Leh (remote)"),
    ]:
        for q in ["airpods pro", "apple pencil", "iphone 16"]:
            r = client.check(lat, lon, q, pin)
            row = match_row(q, r)
            print(f"{label:12} | {q:12} | svc={r.get('serviceable')} "
                  f"items={len(r.get('items', []))} | {row.get('status')} | "
                  f"{(row.get('name') or '')[:34]} Rs{row.get('price')} | {row.get('eta')}")
