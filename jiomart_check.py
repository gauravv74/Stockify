#!/usr/bin/env python3
"""JioMart availability checker.

JioMart (Reliance) runs a unified web app (site_version "JCP") that is a
client-rendered SPA. The delivery location is resolved from the browser's
geolocation (GPS): on load the app reverse-geocodes the coordinates to a
pincode + serving store and rewrites its own `app_location_details` /
`app_geolocation` cookies + localStorage (`pin`, `jio_qc_stores`). Search
results are rendered client-side at `/products?q=<query>` as product tiles
(`div.productCard__cardWrapper`, each carrying an <h3> name, a rupee price,
an optional struck MRP and an "Add to cart" control).

Because JioMart trusts GPS over cookies, we drive a persistent headless
Chromium (Playwright) and, like the Flipkart Minutes checker, open one
context per delivery location with the coordinates spoofed:

Flow per location:
  spoof geolocation(lat,lon) + seed location cookies -> open home -> let the
  app resolve the pincode/store (click "Enable Location" if prompted)
  -> for each query: navigate to /products?q=... (the context keeps the
     resolved location) -> scrape product tiles from the DOM.

A context's geolocation is fixed at creation, so we cache one context per
location and reuse it across all queries for that location, recreating it
only when the location changes.

Exposes a thread-safe singleton `client` with .check(lat, lon, query, pincode).
Matching reuses blinkit_check.best_match (accessory-aware).
"""

import asyncio
import json
import re
import threading
import urllib.parse

from playwright.async_api import async_playwright

import blinkit_check as bk
import config

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")

HOME_URL = "https://www.jiomart.com/"
SEARCH_URL = "https://www.jiomart.com/products?q="

# Scrape JioMart product tiles from the rendered search DOM. Each card is a
# `.productCard__cardWrapper` whose text reads:
#   "<pack> | <name> | ₹<price> | [₹<mrp>] | Quick Delivery"
# The name is also exposed as an <h3>; rupee amounts appear in DOM order as
# [sellingPrice, mrp?]. An "Add to cart" control (and no sold-out marker)
# means the item is in stock.
SCRAPE_JS = r"""
() => {
  const cards = Array.from(document.querySelectorAll('.productCard__cardWrapper'));
  const items = []; const seen = new Set();
  for (const c of cards) {
    const full = (c.innerText || '');
    const t = full.replace(/\s+/g, ' ').trim();
    if (!t) continue;

    // rupee amounts in DOM order: [sellingPrice, mrp?]
    const amounts = []; let m; const re = /₹\s?([\d,]+(?:\.\d+)?)/g;
    while ((m = re.exec(t)) !== null) amounts.push(Number(m[1].replace(/,/g, '')));
    if (!amounts.length) continue;
    const price = amounts[0];
    const mrp = (amounts.length > 1 && amounts[1] > price) ? amounts[1] : null;

    // name: prefer the <h3>, else the longest non-price/pack text line
    const h3 = c.querySelector('h3');
    let name = h3 ? h3.innerText.replace(/\s+/g, ' ').trim() : '';
    const lines = full.split('\n').map(s => s.trim()).filter(Boolean);
    if (!name) {
      for (const l of lines) {
        if (/₹/.test(l)) continue;
        if (/quick delivery|add to cart|^ad$/i.test(l)) continue;
        if (/^\d+\s?(pack|pc|pcs|l|ml|g|kg|unit|combo)\b/i.test(l)) continue;
        if (l.length > name.length) name = l;
      }
    }
    if (!name) continue;

    // variant / pack: a size token inside the name, else the leading pack line
    let variant = '';
    const vm = name.match(/(\d+(?:\.\d+)?\s?(?:ml|l|kg|g|pcs?|pieces?|pack|units?)\b[^)]*\)?)/i);
    if (vm) variant = vm[1].trim();
    if (!variant) {
      const pl = lines.find(l => /^\d+\s?(pack|pc|pcs|l|ml|g|kg|unit|combo)\b/i.test(l));
      if (pl) variant = pl;
    }

    const oos = /sold out|out of stock|notify me|currently unavailable/i.test(t);
    const hasAdd = !!c.querySelector('button');
    const key = name + '|' + variant;
    if (seen.has(key)) continue; seen.add(key);
    // Look for bank/card offer text in the product card
    let cardOffer = null;
    const bankRe = /bank|card|hdfc|icici|sbi|axis|kotak|amex|rupay|visa|master/i;
    for (const l of lines) {
      if (bankRe.test(l) && /\d/.test(l)) {
        const savM = l.match(/₹\s?([\d,]+)/);
        const pctM = l.match(/(\d+)\s*%/);
        cardOffer = { text: l.trim(),
          savings: savM ? Number(savM[1].replace(/,/g, '')) : null,
          percent: pctM ? Number(pctM[1]) : null };
        break;
      }
    }
    items.push({ name, variant, brand: '', price, mrp,
                 inStock: hasAdd && !oos, eta: 'Quick Delivery', cardOffer });
  }
  return items.slice(0, 30);
}
"""


class JioMart:
    """Persistent headless-browser client, serialized behind a lock.

    One browser is shared across the process; a fresh context (carrying the
    spoofed GPS location) is opened per delivery location and cached until the
    location changes, so repeated queries at the same pincode are cheap.
    """

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._lock = threading.Lock()
        self._pw = self._browser = None
        self._ctx = self._page = None
        self._loc_key = None
        self._svc = None            # cached serviceability for current location
        self._addr = ""

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    async def _ensure_browser(self):
        if self._browser is not None:
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

    async def _close_ctx(self):
        try:
            if self._ctx:
                await self._ctx.close()
        except Exception:
            pass
        self._ctx = self._page = None

    async def _reset(self):
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        self._pw = self._browser = self._ctx = self._page = None
        self._loc_key = None
        self._svc = None

    async def _seed_cookies(self, lat, lon, pincode):
        pin = str(pincode or "")
        loc = {"country": "INDIA", "country_iso_code": "IN", "city": "",
               "pincode": pin, "state": ""}
        geo = {"latitude": str(lat), "longitude": str(lon), "polygon_ids": []}
        cookies = [
            {"name": "app_geolocation", "value": urllib.parse.quote(json.dumps(geo)),
             "domain": ".jiomart.com", "path": "/"},
        ]
        if pin:
            cookies.append({"name": "app_location_details",
                            "value": urllib.parse.quote(json.dumps(loc)),
                            "domain": ".jiomart.com", "path": "/"})
        try:
            await self._ctx.add_cookies(cookies)
        except Exception:
            pass

    async def _resolve_location(self):
        """Wait for JioMart to reverse-geocode the spoofed GPS to a pincode."""
        # If a location gate is shown, opt in so the app reads the (spoofed) GPS.
        for pat in ("Enable Location", "Use current location", "current location"):
            try:
                el = self._page.get_by_text(re.compile(pat, re.I)).first
                if await el.is_visible():
                    await el.click(timeout=4000)
                    break
            except Exception:
                continue

        for _ in range(20):
            await self._page.wait_for_timeout(600)
            info = await self._page.evaluate(
                "() => { try { const p = JSON.parse(localStorage.getItem('pin')||'{}');"
                " return (p && p.pincode) ? (p.city + ', ' + p.state + ', ' + p.pincode) : ''; }"
                " catch (e) { return ''; } }")
            if info:
                return info
        return ""

    async def _open_location(self, lat, lon, pincode):
        """Open a fresh context pinned to (lat, lon) and resolve the location."""
        await self._close_ctx()
        ctx_kwargs = dict(
            user_agent=UA, locale="en-US", viewport={"width": 1280, "height": 900},
            geolocation={"latitude": float(lat), "longitude": float(lon)},
            permissions=["geolocation"],
        )
        proxy = config.playwright_proxy()
        if proxy:
            ctx_kwargs["proxy"] = proxy
        self._ctx = await self._browser.new_context(**ctx_kwargs)
        await self._seed_cookies(lat, lon, pincode)
        self._page = await self._ctx.new_page()
        self._svc, self._addr = None, ""

        await self._page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
        await self._page.wait_for_timeout(2000)
        self._addr = await self._resolve_location()
        # Location resolved (or the marketplace default applies) -> serviceable.
        self._svc = True

    async def _search(self, query):
        """Navigate to the results page (the context keeps the location).

        The SPA occasionally renders an empty shell on the first navigation
        after a context is opened, so retry the navigation once when we get
        nothing back without an explicit "no results" state.
        """
        url = SEARCH_URL + urllib.parse.quote(query)
        for _ in range(2):
            await self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
            for _ in range(20):
                await self._page.wait_for_timeout(600)
                items = await self._page.evaluate(SCRAPE_JS)
                if items:
                    return items
                no_results = await self._page.evaluate(
                    "() => /no results found|no products found|couldn.t find any|"
                    "sorry, no results/i.test(document.body.innerText)")
                if no_results:
                    return []
        return []

    async def _query(self, lat, lon, query, pincode):
        await self._ensure_browser()
        key = (round(float(lat), 4), round(float(lon), 4))
        if key != self._loc_key or self._page is None:
            await self._open_location(lat, lon, pincode)
            self._loc_key = key

        if not self._svc:
            return {"serviceable": False, "address": self._addr, "items": []}

        items = await self._search(query)
        return {"serviceable": True, "address": self._addr, "items": items}

    def check(self, lat, lon, query, pincode=None):
        """Return {serviceable, address, items:[{name,variant,brand,price,mrp,inStock,eta}]}."""
        with self._lock:
            try:
                return self._run(self._query(lat, lon, query, pincode))
            except Exception as e:
                try:
                    self._run(self._reset())
                except Exception:
                    pass
                return {"serviceable": None, "address": "", "items": [],
                        "error": str(e)}


client = JioMart()


def match_row(query, result):
    """Normalize a check() result into a row like the other platforms."""
    if result.get("serviceable") is None:
        return {"status": "error", "detail": result.get("error", "")}
    items = result.get("items", [])
    if result.get("serviceable") is False and not items:
        return {"status": "not_serviceable"}
    m = bk.best_match(query, items)
    if not m:
        return {"status": "not_found"}
    row = {
        "status": "available" if m.get("inStock") else "out_of_stock",
        "available": "yes" if m.get("inStock") else "no",
        "name": m.get("name"), "variant": m.get("variant"), "brand": m.get("brand", ""),
        "price": m.get("price"), "mrp": m.get("mrp"), "inventory": "",
        "eta": m.get("eta") or "", "merchant_id": "",
    }
    co = m.get("cardOffer")
    if co and isinstance(co, dict) and (co.get("savings") or co.get("text")):
        from stockly import offers
        row["best_offer"] = offers.make(
            savings_text=co.get("text") if not co.get("savings") else f"₹{co['savings']} OFF",
            final_price=(m.get("price") - co["savings"]) if co.get("savings") and m.get("price") else None,
            base_price=m.get("price"),
            detail=co.get("text"),
            kind="card",
        )
    return row


if __name__ == "__main__":
    for lat, lon, pin, label in [
        (19.0760, 72.8777, "400001", "Mumbai"),
        (12.9716, 77.5946, "560001", "Bangalore"),
        (34.1526, 77.5771, "194101", "Leh (remote)"),
    ]:
        for q in ["amul milk", "maggi noodles", "iphone 15"]:
            r = client.check(lat, lon, q, pin)
            row = match_row(q, r)
            print(f"{label:12} | {q:14} | svc={r.get('serviceable')} "
                  f"items={len(r.get('items', []))} | {row.get('status')} | "
                  f"{(row.get('name') or '')[:45]} {row.get('price') or ''}")
