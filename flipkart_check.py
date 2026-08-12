#!/usr/bin/env python3
"""Flipkart Minutes availability checker.

Flipkart Minutes is Flipkart's quick-commerce storefront, reachable on web at
`/flipkart-minutes-store?marketplace=HYPERLOCAL`. The storefront is a client
rendered SPA behind Akamai; it reads the delivery location from the browser
session (set from GPS coordinates) and only renders products once a serviceable
location is chosen.

Flipkart does not block a real headless browser, so we drive a persistent
headless Chromium (Playwright):

Flow per location:
  spoof geolocation(lat,lon) -> open Minutes store -> "Use my current location"
  -> read serviceability (address + ETA, or "not serviceable")
  -> for each query: search via the in-page search box (a full navigation to
     /search drops the Minutes location) -> scrape product tiles from the DOM.

Because a context's geolocation is fixed at creation, we open one context per
location and reuse it across all queries for that location (the app groups
queries by pincode), recreating it only when the location changes.

Exposes a thread-safe singleton `client` with .check(lat, lon, query).
Matching reuses blinkit_check.best_match (accessory-aware).
"""

import asyncio
import re
import threading

from playwright.async_api import async_playwright

import blinkit_check as bk
import config

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")

MINUTES_URL = "https://www.flipkart.com/flipkart-minutes-store?marketplace=HYPERLOCAL"

# Scrape Minutes product tiles from the rendered search DOM. A product card is
# the innermost element whose text carries a rupee price plus an "Add" control
# (or an out-of-stock marker) and a real product name. Picking the innermost
# such node avoids grabbing the bare price chip or a whole grid wrapper.
SCRAPE_JS = r"""
() => {
  const nameOf = (t) => t
    .replace(/best seller/gi, '').replace(/\bAD\b/g, '').replace(/\d+%\s*Off/gi, '')
    .replace(/₹\s?[\d,]+/g, '').replace(/\bAdd\b/gi, '').replace(/\d+\s*mins?/gi, '')
    .replace(/\s+/g, ' ').trim();
  const isCard = (t) => {
    if (!t || t.length > 240) return false;
    if (!/₹\s?\d/.test(t)) return false;
    if (!/\bAdd\b/i.test(t) && !/sold out|out of stock|notify/i.test(t)) return false;
    return nameOf(t).replace(/[^a-zA-Z]/g, '').length >= 5;
  };
  const bankRe = /bank|card|hdfc|icici|sbi|axis|kotak|amex|rupay|visa|master/i;
  const all = Array.from(document.querySelectorAll('div,a,li'));
  const cards = all.filter(el => isCard(el.innerText || ''));
  // keep only innermost cards (drop any card that contains another card)
  const kept = cards.filter(el => !cards.some(o => o !== el && el.contains(o)));
  const items = []; const seen = new Set();
  for (const el of kept) {
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    const amounts = []; let m; const re = /₹\s?([\d,]+)/g;
    while ((m = re.exec(t)) !== null) amounts.push(Number(m[1].replace(/,/g, '')));
    if (!amounts.length) continue;
    const price = Math.min(...amounts);
    const mrp = amounts.length > 1 ? Math.max(...amounts) : null;
    const inStock = /\bAdd\b/i.test(t) && !/sold out|out of stock|notify/i.test(t);
    const etaM = t.match(/(\d+)\s*mins?/i);
    const vM = t.match(/(\d+(\.\d+)?\s?(ml|l|g|kg|pcs?|pack|units?)\b)/i);
    const name = nameOf(t);
    const key = name.slice(0, 50) + '|' + (vM ? vM[1] : '');
    if (seen.has(key)) continue; seen.add(key);
    // Look for bank/card offer text in the tile
    let cardOffer = null;
    const lines = t.split(/\s{2,}|\n/);
    for (const line of lines) {
      if (bankRe.test(line) && /\d/.test(line)) {
        const savM = line.match(/₹\s?([\d,]+)/);
        const pctM = line.match(/(\d+)\s*%/);
        cardOffer = { text: line.trim(),
          savings: savM ? Number(savM[1].replace(/,/g, '')) : null,
          percent: pctM ? Number(pctM[1]) : null };
        break;
      }
    }
    items.push({ name, variant: vM ? vM[1] : '', brand: '', price, mrp,
                 inStock, eta: etaM ? etaM[0] : '', cardOffer });
  }
  return items.slice(0, 20);
}
"""


class FlipkartMinutes:
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
        self._store_eta = ""

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

    async def _open_location(self, lat, lon):
        """Open a fresh context pinned to (lat, lon) and resolve serviceability."""
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
        self._page = await self._ctx.new_page()
        self._svc, self._addr, self._store_eta = None, "", ""

        await self._page.goto(MINUTES_URL, wait_until="networkidle", timeout=60000)
        await self._page.wait_for_timeout(2000)

        # Use the spoofed GPS: "Use my current location".
        for pat in ("Use my current location", "current location"):
            try:
                el = self._page.get_by_text(re.compile(pat, re.I)).first
                if await el.is_visible():
                    await el.click(timeout=5000)
                    break
            except Exception:
                continue

        # Resolve to: serviceable (header address + ETA) or not serviceable.
        for _ in range(20):
            await self._page.wait_for_timeout(600)
            body = await self._page.evaluate("() => (document.body.innerText || '').slice(0, 200)")
            if re.search(r"not serviceable", body, re.I):
                self._svc = False
                return
            hdr = body.replace("\n", " ")
            m = re.search(r"(\d+)\s*min", hdr)
            if m and "Select delivery address" not in hdr:
                self._svc = True
                self._store_eta = m.group(0)
                # header up to the ETA is the resolved address
                self._addr = hdr.split(m.group(0))[0].strip()[:120]
                return
        # Couldn't confirm either way -> treat as unknown/not serviceable.
        self._svc = False

    async def _search(self, query):
        """Search via the in-page box (soft nav keeps the Minutes location)."""
        try:
            sb = self._page.locator(
                'input[title*="Search"], input[name="q"], '
                'input[placeholder*="Search"], input[type="text"]'
            ).first
            await sb.click(timeout=8000)
            await sb.fill("")
            await sb.type(query, delay=15)
            await self._page.wait_for_timeout(400)
            await self._page.keyboard.press("Enter")
        except Exception:
            return []

        items = []
        for _ in range(20):
            await self._page.wait_for_timeout(600)
            items = await self._page.evaluate(SCRAPE_JS)
            if items:
                break
            no_results = await self._page.evaluate(
                "() => /no results|couldn.t find|not available|no products/i"
                ".test(document.body.innerText)")
            if no_results:
                break
        return items or []

    async def _query(self, lat, lon, query):
        await self._ensure_browser()
        key = (round(float(lat), 4), round(float(lon), 4))
        if key != self._loc_key or self._page is None:
            await self._open_location(lat, lon)
            self._loc_key = key

        if not self._svc:
            return {"serviceable": False, "address": self._addr,
                    "eta": "", "items": []}

        items = await self._search(query)
        return {"serviceable": True, "address": self._addr,
                "eta": self._store_eta, "items": items}

    def check(self, lat, lon, query):
        """Return {serviceable, address, eta, items:[{name,variant,brand,price,mrp,inStock,eta}]}."""
        with self._lock:
            try:
                return self._run(self._query(lat, lon, query))
            except Exception as e:
                try:
                    self._run(self._reset())
                except Exception:
                    pass
                return {"serviceable": None, "address": "", "eta": "",
                        "items": [], "error": str(e)}


client = FlipkartMinutes()


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
        "eta": m.get("eta") or result.get("eta", ""), "merchant_id": "",
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
    for lat, lon, label in [
        (12.9352, 77.6245, "Koramangala BLR"),
        (34.1526, 77.5771, "Leh (remote)"),
    ]:
        for q in ["amul milk", "maggi noodles", "iphone 15"]:
            r = client.check(lat, lon, q)
            row = match_row(q, r)
            print(f"{label:16} | {q:14} | svc={r.get('serviceable')} "
                  f"items={len(r.get('items', []))} | {row.get('status')} | "
                  f"{(row.get('name') or '')[:45]} {row.get('price') or ''}")
