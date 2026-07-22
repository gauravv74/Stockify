#!/usr/bin/env python3
"""Zepto availability checker.

Zepto is a Next.js app behind AWS WAF whose search page is server-rendered and
reads the delivery location from cookies (latitude / longitude / user_position).
It writes a `serviceability` cookie holding the resolved storeId + serviceable
flag + ETA + city. So we drive a persistent headless Chromium (Playwright):
set the location cookies, open /search?query=..., then scrape the product tiles
straight from the DOM.

Flow per location:
  set lat/lng cookies -> GET /search?query=Q -> read serviceability cookie
                      -> scrape product cards from the DOM.

Exposes a thread-safe singleton `client` with .check(lat, lon, query).
Matching reuses blinkit_check.best_match (accessory-aware).
"""

import asyncio
import json
import threading
import urllib.parse

from playwright.async_api import async_playwright

import blinkit_check as bk

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")

# Scrape product tiles from the search DOM into structured items.
SCRAPE_JS = r"""
() => {
  const items = [];
  document.querySelectorAll('a[href*="/pn/"]').forEach(a => {
    const walker = document.createTreeWalker(a, NodeFilter.SHOW_TEXT);
    const parts = []; let n;
    while (n = walker.nextNode()) { const t = n.textContent.trim(); if (t) parts.push(t); }
    if (!parts.length) return;

    const joined = parts.join(' ');
    const hasAdd = parts.some(p => /^add$/i.test(p));
    const oos = /out of stock|sold out|notify me/i.test(joined);

    // rupee amounts, in DOM order: [sellingPrice, mrp, discount?]
    const amounts = [];
    const re = /₹\s?([\d,]+)/g; let m;
    while ((m = re.exec(joined)) !== null) amounts.push(Number(m[1].replace(/,/g, '')));
    const price = amounts.length ? amounts[0] : null;
    const mrp = (/\bOFF\b/i.test(joined) && amounts.length > 1) ? amounts[1] : null;

    // pack / variant, e.g. "1 pc", "1 pack (280 g)", "500 ml"
    let variant = '';
    for (const p of parts) {
      if (/^\d+\s?(pc|pcs|pack|unit|combo)\b/i.test(p) || /\b\d+(\.\d+)?\s?(g|kg|ml|l)\b/i.test(p)) { variant = p; break; }
    }

    // product name: the longest text part that is not price/ADD/OFF/pack/rating
    let name = '';
    for (const p of parts) {
      if (/[a-zA-Z]{3,}/.test(p) && !/^(add|off)$/i.test(p)
          && !/^\d+\s?(pc|pcs|pack|unit|combo)\b/i.test(p) && !/^\(?\d/.test(p)) {
        if (p.length > name.length) name = p;
      }
    }
    const slug = ((a.getAttribute('href') || '').match(/\/pn\/([^/]+)/) || [])[1] || '';
    if (!name) name = slug.replace(/-/g, ' ');

    items.push({name: name.replace(/\s+/g, ' ').trim(), variant, brand: '',
                price, mrp, inStock: hasAdd && !oos});
  });
  // de-dupe by name + variant
  const seen = new Set(); const out = [];
  for (const it of items) { const k = it.name + '|' + it.variant; if (!seen.has(k)) { seen.add(k); out.push(it); } }
  return out;
}
"""


class Zepto:
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
            user_agent=UA, locale="en-US", viewport={"width": 1280, "height": 900})
        self._page = await self._ctx.new_page()
        # Solve the AWS WAF challenge once.
        await self._page.goto("https://www.zepto.com/", wait_until="domcontentloaded", timeout=45000)
        await self._page.wait_for_timeout(3000)

    async def _reset(self):
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        self._pw = self._browser = self._ctx = self._page = None

    async def _set_location(self, lat, lon):
        # Drop any stale store so Zepto recomputes serviceability for this point.
        try:
            await self._ctx.clear_cookies(name="serviceability")
        except Exception:
            pass
        pos = json.dumps({"latitude": float(lat), "longitude": float(lon)})
        await self._ctx.add_cookies([
            {"name": "latitude", "value": str(lat), "domain": ".zepto.com", "path": "/"},
            {"name": "longitude", "value": str(lon), "domain": ".zepto.com", "path": "/"},
            {"name": "user_position", "value": pos, "domain": ".zepto.com", "path": "/"},
        ])

    async def _serviceability(self):
        for c in await self._ctx.cookies():
            if c["name"] == "serviceability":
                try:
                    data = json.loads(urllib.parse.unquote(c["value"]))
                    ps = data.get("primaryStore") or {}
                    info = data.get("storeDetailedInfo") or {}
                    return {
                        "serviceable": bool(ps.get("serviceable")),
                        "store": ps.get("storeId"),
                        "city": info.get("city", ""),
                        "eta": (data.get("etaInformation") or {}).get("secondaryText", ""),
                    }
                except Exception:
                    return None
        return None

    async def _query(self, lat, lon, query):
        await self._ensure()
        await self._set_location(lat, lon)
        # The /search page only READS the serviceability cookie; the home page
        # RESOLVES it (store id + serviceable) for the current lat/lng. So load
        # home first and wait for the store to be recomputed for this location.
        await self._page.goto("https://www.zepto.com/", wait_until="domcontentloaded", timeout=45000)
        svc = None
        for _ in range(12):
            await self._page.wait_for_timeout(500)
            svc = await self._serviceability()
            if svc is not None:
                break

        if svc is not None and not svc.get("serviceable") and not svc.get("store"):
            return {"serviceable": False, "store": None, "city": svc.get("city", ""),
                    "eta": "", "items": []}

        url = "https://www.zepto.com/search?query=" + urllib.parse.quote(query)
        await self._page.goto(url, wait_until="domcontentloaded", timeout=45000)
        # Poll until product tiles hydrate (or a clear "no results" state), so a
        # slow render never masquerades as "not found".
        items = []
        for _ in range(16):
            await self._page.wait_for_timeout(500)
            items = await self._page.evaluate(SCRAPE_JS)
            if items:
                break
            no_results = await self._page.evaluate(
                "() => /no results|couldn.t find|not available|no products/i.test(document.body.innerText)")
            if no_results:
                break
        svc = await self._serviceability() or svc
        serviceable = True if svc is None else svc.get("serviceable", True)
        return {
            "serviceable": serviceable,
            "store": (svc or {}).get("store"),
            "city": (svc or {}).get("city", ""),
            "eta": (svc or {}).get("eta", ""),
            "items": items or [],
        }

    def check(self, lat, lon, query):
        """Return {serviceable, store, city, eta, items:[{name,variant,price,mrp,inStock}]}."""
        with self._lock:
            try:
                return self._run(self._query(lat, lon, query))
            except Exception as e:
                try:
                    self._run(self._reset())
                except Exception:
                    pass
                return {"serviceable": None, "store": None, "items": [], "error": str(e)}


client = Zepto()


def match_row(query, result):
    """Normalize a check() result into a row like the other platforms."""
    if result.get("serviceable") is None:
        return {"status": "error", "detail": result.get("error", "")}
    items = result.get("items", [])
    # No store and nothing rendered -> location not served by Zepto.
    if result.get("serviceable") is False and not items:
        return {"status": "not_serviceable"}
    m = bk.best_match(query, items)
    if not m:
        if not items and result.get("serviceable") is False:
            return {"status": "not_serviceable"}
        return {"status": "not_found", "store": result.get("store")}
    return {
        "status": "available" if m.get("inStock") else "out_of_stock",
        "available": "yes" if m.get("inStock") else "no",
        "name": m.get("name"), "variant": m.get("variant"), "brand": m.get("brand", ""),
        "price": m.get("price"), "mrp": m.get("mrp"), "inventory": "",
        "eta": result.get("eta", ""), "merchant_id": result.get("store"),
    }


if __name__ == "__main__":
    for lat, lon, label in [(18.5362, 73.8940, "Pune KP"), (28.6139, 77.209, "Delhi CP")]:
        for q in ["maggi", "iphone 17", "amul milk"]:
            r = client.check(lat, lon, q)
            row = match_row(q, r)
            print(f"{label:9} | {q:10} | svc={r.get('serviceable')} store={str(r.get('store'))[:8]} "
                  f"items={len(r.get('items', []))} | {row.get('status')} | {(row.get('name') or '')[:45]}")
