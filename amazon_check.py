#!/usr/bin/env python3
"""Amazon India availability checker.

Amazon.in is a national marketplace: catalogue and prices are shared across
India, and the location-specific signal is whether Amazon will deliver the
matched ASIN to the shopper's pincode. Both signals are reachable over plain
HTTP with Chrome TLS impersonation (curl_cffi) — no headless browser:

  1. POST /gp/delivery/ajax/address-change.html  (Glow "Deliver to" pin)
     -> sets the session's delivery zip so search results reflect that pin.
  2. GET  /s?k=<query>
     -> HTML search results with ASIN, brand, title, price, MRP and an
        "Currently unavailable" marker when the listing is OOS.

Availability here means: the best-matching listing is buyable (has a price and
is not marked unavailable). Amazon delivers to essentially every valid Indian
pincode; an invalid pin fails step 1 and is reported as not_serviceable.

Exposes a thread-safe singleton `client` with .check(lat, lon, query, pincode).
Matching reuses blinkit_check.best_match (accessory-aware).
"""

from __future__ import annotations

import html as htmlmod
import re
import threading
import time
from urllib.parse import quote

from curl_cffi import requests

import blinkit_check as bk
import config

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
IMPERSONATE = "chrome124"
BASE = "https://www.amazon.in"
PIN_URL = BASE + "/gp/delivery/ajax/address-change.html"
SEARCH_URL = BASE + "/s"

_HTML_H = {
    "user-agent": UA,
    "accept-language": "en-IN,en;q=0.9",
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
_PIN_H = {
    **_HTML_H,
    "content-type": "application/x-www-form-urlencoded",
    "x-requested-with": "XMLHttpRequest",
    "origin": BASE,
    "referer": BASE + "/",
    "accept": "application/json, text/javascript, */*; q=0.01",
}


def _parse_search(html: str) -> list[dict]:
    """Pull product cards out of an Amazon search results page."""
    parts = re.split(r'data-component-type="s-search-result"', html)
    items: list[dict] = []
    seen: set[str] = set()
    for part in parts[1:40]:
        chunk = part[:16000]
        asin_m = re.search(r"/dp/([A-Z0-9]{10})", chunk)
        if not asin_m:
            continue
        asin = asin_m.group(1)
        if asin in seen:
            continue

        # Newer Amazon layouts put brand and title in consecutive spans under
        # title-recipe. Sponsored-ad chrome is filtered out.
        tr = re.search(
            r'data-cy="title-recipe"([\s\S]*?)'
            r'data-cy="(?:price-recipe|reviews-block|asin-faceout)',
            chunk,
        )
        region = tr.group(1) if tr else ""
        spans = [
            htmlmod.unescape(x).strip()
            for x in re.findall(r"<span[^>]*>([^<]{2,300})</span>", region)
        ]
        spans = [
            x for x in spans
            if x and not re.search(
                r"sponsored|let us know|you are seeing|leave a rating|"
                r"overall pick|best seller|amazon.?s choice|^\d+(\.\d+)?$",
                x, re.I,
            )
        ]
        if not spans:
            continue
        brand = spans[0] if len(spans) > 1 else ""
        title = spans[1] if len(spans) > 1 else spans[0]
        if brand and title and not title.lower().startswith(brand.lower()):
            name = f"{brand} {title}".strip()
        else:
            name = title or brand
        if not name:
            continue

        price_m = re.search(r'class="a-price-whole"[^>]*>([\d,]+)', chunk)
        price = float(price_m.group(1).replace(",", "")) if price_m else None
        mrp_m = re.search(
            r'class="a-price a-text-price"[^>]*>[\s\S]*?'
            r'a-offscreen[^>]*>\s*₹\s*([\d,]+)',
            chunk,
        )
        mrp = float(mrp_m.group(1).replace(",", "")) if mrp_m else None
        # Amazon sometimes surfaces a tiny "₹7" style chip in the struck-price
        # slot; only keep MRP when it is actually above the selling price.
        if mrp is not None and (price is None or mrp <= price):
            mrp = None

        plain = re.sub(r"<[^>]+>", " ", chunk[:6000])
        oos = bool(re.search(r"currently unavailable|out of stock", plain, re.I))
        eta = ""
        dm = re.search(
            r"(FREE delivery[^<\n]{0,50}|Get it by[^<\n]{0,40}|"
            r"Delivery by[^<\n]{0,40})",
            plain, re.I,
        )
        if dm:
            eta = re.sub(r"\s+", " ", dm.group(1)).strip()

        # Bank/card offer text occasionally appears on the tile.
        card_offer = None
        bank_re = re.compile(
            r"bank|card|hdfc|icici|sbi|axis|kotak|amex|rupay|visa|master", re.I)
        for line in re.split(r"\s{2,}|\n", plain):
            line = line.strip()
            if bank_re.search(line) and re.search(r"\d", line) and len(line) < 160:
                sav = re.search(r"₹\s?([\d,]+)", line)
                pct = re.search(r"(\d+)\s*%", line)
                card_offer = {
                    "text": line,
                    "savings": float(sav.group(1).replace(",", "")) if sav else None,
                    "percent": float(pct.group(1)) if pct else None,
                }
                break

        seen.add(asin)
        items.append({
            "name": name,
            "brand": brand,
            "variant": "",
            "price": price,
            "mrp": mrp,
            # A priced listing without an OOS marker is buyable. Listings with
            # no price (e.g. "See options") are treated as unavailable rather
            # than inventing stock.
            "inStock": (not oos) and price is not None,
            "eta": eta,
            "merchant_id": asin,
            "cardOffer": card_offer,
        })
    return items


class Amazon:
    """curl_cffi client. One session per check; location is a request param."""

    def __init__(self):
        # Bound concurrent outbound Amazon calls the same way Blinkit/BigBasket
        # do — retailer rate limits, not CPU, are the constraint.
        self._slots = threading.Semaphore(config.platform_slots("amazon"))

    def _session(self):
        return requests.Session(impersonate=IMPERSONATE)

    def _set_pin(self, session, pincode) -> bool | None:
        """Bind the Glow delivery zip. True/False = valid/invalid; None = error."""
        try:
            r = session.post(
                PIN_URL,
                data={
                    "locationType": "LOCATION_INPUT",
                    "zipCode": str(pincode),
                    "storeContext": "generic",
                    "deviceType": "web",
                    "pageType": "Gateway",
                    "actionSource": "glow",
                },
                headers=_PIN_H,
                timeout=config.HTTP_REQUEST_TIMEOUT_SEC,
                proxies=config.curl_proxies(),
            )
        except Exception:
            return None
        if r.status_code != 200:
            return None
        try:
            data = r.json()
        except Exception:
            return None
        if data.get("successful") or data.get("isValidAddress"):
            return True
        if data.get("isValidAddress") == 0:
            return False
        return None

    def check(self, lat, lon, query, pincode=None):
        """Return {serviceable, items:[...], error?}."""
        with self._slots:
            session = self._session()
            try:
                # Warm cookies; Amazon tolerates a soft-fail here.
                try:
                    session.get(
                        BASE + "/", headers=_HTML_H,
                        timeout=config.HTTP_REQUEST_TIMEOUT_SEC,
                        proxies=config.curl_proxies(),
                    )
                except Exception:
                    pass

                if pincode:
                    pin_ok = self._set_pin(session, pincode)
                    if pin_ok is False:
                        return {"serviceable": False, "items": []}
                    if pin_ok is None:
                        # Pin endpoint flaked; still try the search — Amazon's
                        # catalogue is national, so a missing pin just means we
                        # can't claim pin-specific delivery ETA.
                        pass

                url = f"{SEARCH_URL}?k={quote(query)}"
                last_status = 0
                for attempt in range(max(config.HTTP_SCRAPER_MAX_RETRIES, 1)):
                    r = session.get(
                        url, headers={**_HTML_H, "referer": BASE + "/"},
                        timeout=config.HTTP_REQUEST_TIMEOUT_SEC,
                        proxies=config.curl_proxies(),
                    )
                    last_status = r.status_code
                    if r.status_code == 200 and "Type the characters" not in (r.text or ""):
                        items = _parse_search(r.text or "")
                        return {"serviceable": True, "items": items}
                    if r.status_code in (429, 503, 502, 500) or "Type the characters" in (r.text or ""):
                        time.sleep(config.HTTP_SCRAPER_BACKOFF_BASE_SEC * (attempt + 1))
                        continue
                    break
                return {
                    "serviceable": None, "items": [],
                    "error": f"search blocked (status={last_status})",
                }
            except Exception as e:
                return {"serviceable": None, "items": [], "error": str(e)[:200]}


client = Amazon()


def match_row(query, result):
    """Normalize a check() result into a row like the other platforms."""
    if result.get("serviceable") is None:
        return {"status": "error", "detail": result.get("error", "")}
    if result.get("serviceable") is False:
        return {"status": "not_serviceable"}
    items = result.get("items") or []
    m = bk.best_match(query, items)
    if not m:
        return {"status": "not_found"}
    row = {
        "status": "available" if m.get("inStock") else "out_of_stock",
        "available": "yes" if m.get("inStock") else "no",
        "name": m.get("name"), "variant": m.get("variant", ""),
        "brand": m.get("brand", ""),
        "price": m.get("price"), "mrp": m.get("mrp"), "inventory": "",
        "eta": m.get("eta") or "", "merchant_id": m.get("merchant_id", ""),
    }
    co = m.get("cardOffer")
    if co and isinstance(co, dict) and (co.get("savings") or co.get("text")):
        from stockly import offers
        row["best_offer"] = offers.make(
            savings_text=co.get("text") if not co.get("savings") else f"₹{co['savings']} OFF",
            final_price=(m.get("price") - co["savings"])
            if co.get("savings") and m.get("price") else None,
            base_price=m.get("price"),
            detail=co.get("text"),
            kind="card",
        )
    return row


if __name__ == "__main__":
    for pin, label in [("411001", "Pune"), ("560001", "Bengaluru")]:
        for q in ["iphone 15", "amul milk", "sony wh-1000xm5"]:
            r = client.check(18.5, 73.8, q, pin)
            row = match_row(q, r)
            print(f"{label:10} | {q:18} | items={len(r.get('items', [])):2} | "
                  f"{row.get('status'):14} | {(row.get('name') or '')[:50]} "
                  f"{row.get('price') or ''}")
