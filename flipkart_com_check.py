#!/usr/bin/env python3
"""Flipkart.com (marketplace) availability checker.

Distinct from Flipkart Minutes (`flipkart_check.py`), which is Flipkart's
hyperlocal quick-commerce storefront. This module covers the national
marketplace at flipkart.com:

  GET /search?q=<query>
    -> HTML embeds `window.__INITIAL_STATE__` with product widgets carrying
       title, brand, pricing (MRP + selling price), listingId and
       availability.displayState (IN_STOCK / OUT_OF_STOCK / ...).

Availability here means: the best-matching listing is nationally in stock.
Pincode does not change Flipkart's search catalogue the way it does for
Minutes / Instamart — delivery eligibility is resolved on the PDP and is not
reliably exposed over the same unauthenticated HTTP path, so we report the
listing-level stock Flipkart already publishes on search (the same national
signal Croma falls back to when its per-pin TMS call fails).

Exposes a thread-safe singleton `client` with .check(lat, lon, query, pincode).
Matching reuses blinkit_check.best_match (accessory-aware).
"""

from __future__ import annotations

import json
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
BASE = "https://www.flipkart.com"
SEARCH_URL = BASE + "/search"

_HTML_H = {
    "user-agent": UA,
    "accept-language": "en-IN,en;q=0.9",
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "referer": BASE + "/",
}

_STATE_RE = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});\s*</script>", re.S)
_BANK_RE = re.compile(
    r"bank|card|hdfc|icici|sbi|axis|kotak|amex|rupay|visa|master", re.I)


def _walk_products(obj, out: list):
    """Collect productInfo.value dicts that look like real listings."""
    if isinstance(obj, dict):
        pi = obj.get("productInfo")
        if isinstance(pi, dict) and isinstance(pi.get("value"), dict):
            val = pi["value"]
            titles = val.get("titles") or {}
            pricing = val.get("pricing") or {}
            if (titles.get("title") or titles.get("newTitle")) and pricing.get("prices"):
                out.append(val)
        for v in obj.values():
            _walk_products(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_products(v, out)


def _card_offer_from(val: dict):
    """Best-effort bank/card offer from Flipkart widget fields."""
    candidates = []
    for key in ("offerInfo", "offers", "tags", "snippets"):
        blob = val.get(key)
        if blob is None:
            continue
        if isinstance(blob, (list, tuple)):
            candidates.extend(blob)
        else:
            candidates.append(blob)
    # Flatten nested dicts into text snippets.
    texts = []
    for c in candidates:
        if isinstance(c, str):
            texts.append(c)
        elif isinstance(c, dict):
            for k in ("text", "title", "description", "value", "label"):
                if isinstance(c.get(k), str):
                    texts.append(c[k])
            # FormattedRichTextData-style nests
            data = c.get("data")
            if isinstance(data, list):
                for d in data:
                    if isinstance(d, dict):
                        v = ((d.get("value") or {}).get("text")
                             if isinstance(d.get("value"), dict) else d.get("text"))
                        if isinstance(v, str):
                            texts.append(v)
    for text in texts:
        text = (text or "").strip()
        if not text or not _BANK_RE.search(text) or not re.search(r"\d", text):
            continue
        if len(text) > 180:
            continue
        sav = re.search(r"₹\s?([\d,]+)", text)
        pct = re.search(r"(\d+)\s*%", text)
        return {
            "text": text,
            "savings": float(sav.group(1).replace(",", "")) if sav else None,
            "percent": float(pct.group(1)) if pct else None,
        }
    return None


def _items_from_state(data: dict) -> list[dict]:
    found: list[dict] = []
    _walk_products(data, found)
    items: list[dict] = []
    seen: set[str] = set()
    for val in found:
        titles = val.get("titles") or {}
        pricing = val.get("pricing") or {}
        avail = val.get("availability") or {}
        prices = pricing.get("prices") or []
        price = next((p.get("value") for p in prices if not p.get("strikeOff")), None)
        mrp = next((p.get("value") for p in prices if p.get("strikeOff")), None)
        if mrp is not None and price is not None and mrp <= price:
            mrp = None
        state = (avail.get("displayState") or "").upper()
        if state == "OUT_OF_STOCK":
            in_stock = False
        elif state in ("IN_STOCK", "LIMITED_STOCK"):
            in_stock = True
        else:
            # Unknown / empty state: priced + buyable intent wins.
            buy = val.get("buyability") or {}
            in_stock = (
                (buy.get("intent") or "").lower() == "positive"
                or price is not None
            )
        brand = titles.get("superTitle") or ""
        name = titles.get("title") or titles.get("newTitle") or ""
        if not name:
            continue
        specs = val.get("keySpecs") or []
        variant = specs[0] if isinstance(specs, list) and specs else ""
        mid = val.get("listingId") or val.get("id") or ""
        if mid and mid in seen:
            continue
        if mid:
            seen.add(mid)
        items.append({
            "name": name,
            "brand": brand,
            "variant": variant if isinstance(variant, str) else "",
            "price": float(price) if price is not None else None,
            "mrp": float(mrp) if mrp is not None else None,
            "inStock": bool(in_stock),
            "eta": "",
            "merchant_id": mid,
            "cardOffer": _card_offer_from(val),
        })
    return items


class FlipkartCom:
    """curl_cffi client for the Flipkart.com marketplace search page."""

    def __init__(self):
        self._slots = threading.Semaphore(config.platform_slots("flipkart_com"))

    def check(self, lat, lon, query, pincode=None):
        """Return {serviceable, items:[...], error?}."""
        with self._slots:
            session = requests.Session(impersonate=IMPERSONATE)
            url = f"{SEARCH_URL}?q={quote(query)}"
            last_status = 0
            try:
                for attempt in range(max(config.HTTP_SCRAPER_MAX_RETRIES, 1)):
                    r = session.get(
                        url, headers=_HTML_H,
                        timeout=config.HTTP_REQUEST_TIMEOUT_SEC,
                        proxies=config.curl_proxies(),
                    )
                    last_status = r.status_code
                    text = r.text or ""
                    if r.status_code == 200:
                        m = _STATE_RE.search(text)
                        if m:
                            try:
                                data = json.loads(m.group(1))
                            except json.JSONDecodeError:
                                data = None
                            if data is not None:
                                return {
                                    "serviceable": True,
                                    "items": _items_from_state(data),
                                }
                        # Soft block / empty shell — retry.
                        if "captcha" in text.lower() or len(text) < 5000:
                            time.sleep(config.HTTP_SCRAPER_BACKOFF_BASE_SEC * (attempt + 1))
                            continue
                        # Page loaded but no state — treat as empty catalogue.
                        return {"serviceable": True, "items": []}
                    if r.status_code in (429, 500, 502, 503):
                        time.sleep(config.HTTP_SCRAPER_BACKOFF_BASE_SEC * (attempt + 1))
                        continue
                    break
                return {
                    "serviceable": None, "items": [],
                    "error": f"search blocked (status={last_status})",
                }
            except Exception as e:
                return {"serviceable": None, "items": [], "error": str(e)[:200]}


client = FlipkartCom()


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
    for q in ["iphone 15", "amul milk", "sony wh-1000xm5", "nonexistentzzz"]:
        r = client.check(18.5, 73.8, q, "411001")
        row = match_row(q, r)
        print(f"{q:18} | items={len(r.get('items', [])):2} | "
              f"{row.get('status'):14} | {(row.get('name') or '')[:50]} "
              f"{row.get('price') or ''}")
