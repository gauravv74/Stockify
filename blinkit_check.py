#!/usr/bin/env python3
"""
Blinkit product availability checker.

For each pincode:
  1. Geocode the pincode -> lat/lon (OpenStreetMap Nominatim, cached).
  2. For each product query, hit Blinkit's search endpoint at that location.
  3. Detect serviceability, best-matching product, price, stock and ETA.

Output: blinkit_availability.csv  (one row per product x pincode)

Blinkit sits behind Cloudflare with TLS/JA3 fingerprinting, so we use
curl_cffi impersonating Chrome. No API key needed for unauthenticated search.
"""

import csv
import json
import os
import re
import sys
import time
import uuid

from curl_cffi import requests

import config

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
PRODUCTS_FILE = os.path.join(HERE, "products.txt")
PINCODES_FILE = os.path.join(HERE, "pincodes.txt")
OUTPUT_CSV = os.path.join(HERE, "blinkit_availability.csv")
GEO_CACHE = os.path.join(HERE, "pincode_geocache.json")

IMPERSONATE = "chrome124"
REQUEST_PAUSE = 0.6        # seconds between Blinkit calls (avoid 429)
GEO_PAUSE = 1.1            # Nominatim asks for <=1 req/sec
# In-request retries. Backoff is 3s * attempt, so 4 attempts is 30s of sleeping
# inside a single check — longer than a queued task's whole budget, which meant
# a rate-limited check was killed mid-backoff and redelivered, adding yet another
# request. The queue is now the outer retry layer (with jitter, and without
# holding a worker thread), so this one stays short enough to fit the budget.
MAX_RETRIES = config.HTTP_SCRAPER_MAX_RETRIES
# Shared with the queue's time-limit maths; see config.http_scraper_worst_case_sec.
REQUEST_TIMEOUT = config.HTTP_REQUEST_TIMEOUT_SEC
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

SEARCH_URL = "https://blinkit.com/v1/layout/search"


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def read_lines(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out


def load_cache():
    if os.path.exists(GEO_CACHE):
        try:
            return json.load(open(GEO_CACHE))
        except Exception:
            return {}
    return {}


def save_cache(cache):
    json.dump(cache, open(GEO_CACHE, "w"), indent=2)


def _nominatim(session, params):
    r = session.get(
        "https://nominatim.openstreetmap.org/search",
        params=params,
        headers={"user-agent": "blinkit-availability-checker/1.0"},
        impersonate=IMPERSONATE, timeout=REQUEST_TIMEOUT,
    )
    js = r.json()
    if js:
        return {"lat": js[0]["lat"], "lon": js[0]["lon"],
                "place": js[0].get("display_name", "")}
    return None


def _india_post_place(session, pin):
    """Fallback: resolve a pincode to a 'Block, District, State' string via the
    free India Post API, which covers pincodes OSM lacks as postal nodes."""
    try:
        r = session.get(f"https://api.postalpincode.in/pincode/{pin}",
                        impersonate=IMPERSONATE, timeout=REQUEST_TIMEOUT)
        js = r.json()
        if js and js[0].get("Status") == "Success" and js[0].get("PostOffice"):
            po = js[0]["PostOffice"][0]
            parts = [po.get("Block") or po.get("Name"), po.get("District"), po.get("State")]
            return ", ".join([p for p in parts if p])
    except Exception:
        pass
    return None


def geocode_pincode(pin, cache, session, persist=True):
    if pin in cache and cache[pin].get("lat"):
        return cache[pin]
    result = {"lat": None, "lon": None, "place": None}
    attempts = [
        {"postalcode": pin, "country": "India", "format": "json", "limit": 1},
        {"q": f"{pin}, Maharashtra, India", "format": "json", "limit": 1},
        {"q": f"{pin}, India", "format": "json", "limit": 1},
    ]
    for params in attempts:
        try:
            hit = _nominatim(session, params)
            if hit:
                result = hit
                break
        except Exception as e:
            print(f"    ! geocode error for {pin}: {e}", file=sys.stderr)
        time.sleep(GEO_PAUSE)

    # Last resort: India Post -> place name -> Nominatim
    if not result["lat"]:
        place = _india_post_place(session, pin)
        if place:
            try:
                hit = _nominatim(session, {"q": place, "format": "json", "limit": 1})
                if hit:
                    hit["place"] = f"{place} (via India Post)"
                    result = hit
            except Exception as e:
                print(f"    ! fallback geocode error for {pin}: {e}", file=sys.stderr)
            time.sleep(GEO_PAUSE)

    cache[pin] = result
    # Callers that own a shared/multi-process cache (see stockly/geo.py) persist
    # the result themselves; rewriting the whole JSON file from several worker
    # processes loses updates and can truncate it.
    if persist:
        save_cache(cache)
    time.sleep(GEO_PAUSE)
    return result


def blinkit_headers(lat, lon):
    return {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "app_client": "consumer_web",
        "app_version": "1010101010",
        "web_app_version": "1008010016",
        "lat": str(lat),
        "lon": str(lon),
        "device_id": str(uuid.uuid4()),
        "session_uuid": str(uuid.uuid4()),
        "access_token": "",
        "content-type": "application/json",
        "origin": "https://blinkit.com",
        "referer": "https://blinkit.com/",
        "user-agent": UA,
    }


def blinkit_search(session, query, lat, lon):
    """Returns (serviceable: bool|None, products: list[dict], raw_status)."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.post(
                SEARCH_URL, params={"q": query},
                headers=blinkit_headers(lat, lon),
                impersonate=IMPERSONATE, timeout=REQUEST_TIMEOUT,
                proxies=config.curl_proxies(),
            )
        except Exception as e:
            print(f"    ! request error ({attempt}/{MAX_RETRIES}): {e}", file=sys.stderr)
            time.sleep(1.5 * attempt)
            continue

        if r.status_code == 429 or r.status_code >= 500:
            wait = 3 * attempt
            print(f"    ! HTTP {r.status_code}, backing off {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue

        try:
            js = r.json()
        except Exception:
            return None, [], r.status_code

        if r.status_code == 400 and isinstance(js, dict) and \
                "not serviceable" in str(js.get("error", "")).lower():
            return False, [], r.status_code

        if r.status_code == 200 and isinstance(js, dict) and js.get("is_success"):
            snippets = js.get("response", {}).get("snippets", [])
            return True, parse_products(snippets), r.status_code

        # unexpected -> retry a couple of times
        print(f"    ! unexpected status {r.status_code}: {str(js)[:120]}", file=sys.stderr)
        time.sleep(1.5 * attempt)
    return None, [], -1


def _txt(node):
    if isinstance(node, dict):
        return node.get("text")
    return None


def parse_products(snippets):
    products = []
    for s in snippets:
        if not s.get("widget_type", "").startswith("product_card"):
            continue
        d = s.get("data", {})
        cart = (d.get("atc_action", {}) or {}).get("add_to_cart", {}).get("cart_item", {}) or {}
        price_txt = _txt(d.get("normal_price")) or ""
        price = cart.get("price")
        if price is None and price_txt:
            m = re.search(r"\d+", price_txt.replace(",", ""))
            price = int(m.group()) if m else None
        inventory = d.get("inventory", cart.get("inventory"))
        sold_out = bool(d.get("is_sold_out"))
        available = (not sold_out) and (inventory is None or inventory > 0)
        # Look for bank/card offer in the product snippet
        card_offer = _extract_blinkit_card_offer(d)
        products.append({
            "name": _txt(d.get("name")) or cart.get("product_name") or "",
            "variant": _txt(d.get("variant")) or cart.get("unit") or "",
            "brand": cart.get("brand") or "",
            "price": price,
            "mrp": cart.get("mrp"),
            "inventory": inventory,
            "sold_out": sold_out,
            "available": available,
            "eta": _txt((d.get("eta_tag") or {}).get("title")) or d.get("eta_identifier") or "",
            "merchant_id": (d.get("meta") or {}).get("merchant_id"),
            "product_id": (d.get("meta") or {}).get("product_id"),
            "cardOffer": card_offer,
        })
    return products


_BANK_PATTERN = re.compile(
    r"bank|card|hdfc|icici|sbi|axis|kotak|amex|rupay|visa|master", re.I)


def _extract_blinkit_card_offer(data):
    """Extract credit card offer from Blinkit product snippet data if present."""
    for key in ("offers", "bank_offers", "bankOffers", "applicable_offers",
                "payment_offers", "coupon_text"):
        val = data.get(key)
        if not val:
            continue
        entries = val if isinstance(val, list) else [val]
        for entry in entries:
            if isinstance(entry, str):
                desc = entry.strip()
            elif isinstance(entry, dict):
                desc = ""
                for field in ("description", "title", "offer_text", "text", "message"):
                    desc = (entry.get(field) or "").strip()
                    if desc:
                        break
            else:
                continue
            if desc and _BANK_PATTERN.search(desc):
                sav_m = re.search(r"₹\s?([\d,]+)", desc)
                pct_m = re.search(r"(\d+)\s*%", desc)
                return {
                    "text": desc,
                    "savings": int(sav_m.group(1).replace(",", "")) if sav_m else None,
                    "percent": int(pct_m.group(1)) if pct_m else None,
                }
    return None


def _norm(s):
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    # Split glued number+unit so a query like "500g" matches a catalogue
    # variant rendered as "500 g" (and "1ltr" == "1 ltr", "128gb" == "128 gb").
    # Without this, best_match's "every query token must appear" rule drops the
    # right product because "500g" != the separate tokens "500" and "g".
    s = re.sub(r"(\d)\s*([a-z])", r"\1 \2", s)
    return s


def _capacity_gb(product):
    """Storage capacity of a product in GB (TB -> *1024).

    Used to prefer the base (smallest-storage) variant when a query doesn't pin a
    capacity, e.g. "iphone 17" should surface the 128GB model, not 512GB/1TB.
    Returns the largest capacity found (so a laptop's SSD wins over its RAM) and
    +inf when none is present, so capacity-less items never outrank real ones.
    """
    text = _norm(product.get("name", "") + " " + product.get("variant", ""))
    vals = []
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(tb|gb)\b", text):
        try:
            vals.append(float(num) * (1024.0 if unit == "tb" else 1.0))
        except ValueError:
            pass
    return max(vals) if vals else float("inf")


# Words that indicate an accessory or an add-on service rather than the actual
# device. When the query itself is not about one of these, candidates containing
# them are dropped so "iphone 17" matches the phone, not an "iphone 17 cover" or
# an "AppleCare+ for iPhone 17" protection plan.
ACCESSORY_WORDS = {
    "cover", "case", "guard", "protector", "tempered", "glass", "screen",
    "charger", "cable", "adapter", "skin", "pouch", "holder", "stand",
    "mount", "ring", "strap", "lens", "grip", "sleeve", "wallet",
    # add-on services / protection plans (e.g. Apple's "AppleCare+ for iPhone 17")
    "applecare", "care", "warranty", "protection", "plan", "insurance",
    "coverage", "subscription",
}


def best_match(query, products):
    """Pick the product card whose name best matches the query tokens.

    Matching rewards covering all query tokens and penalizes extra tokens in the
    candidate name (so the bare device beats accessories/bundles). Accessory
    products are demoted unless the query is itself about an accessory. Among
    otherwise-equal matches we prefer the base (smallest-storage) variant so a
    query like "iphone 17" surfaces the 128GB model rather than 512GB/1TB, then
    an in-stock variant, then the cheaper one. If the query pins a capacity
    (e.g. "iphone 17 256gb") the token filter already keeps only that capacity,
    so the base-variant preference is a no-op there.
    """
    q_tokens = [t for t in _norm(query).split() if t]
    if not q_tokens:
        return products[0] if products else None
    # Every query token must appear in the candidate. This keeps "iphone 17"
    # from matching "iPhone 16" (missing 17) or a "17.4 g" weight (missing
    # iphone). Numbers matter as much as words for specific SKUs.
    query_is_accessory = any(t in ACCESSORY_WORDS for t in q_tokens)

    best, best_key = None, None
    for p in products:
        name_tokens = _norm(p.get("name", "")).split()
        hay = set(_norm(p.get("name", "") + " " + p.get("variant", "") + " "
                        + p.get("brand", "")).split())
        if not all(t in hay for t in q_tokens):
            continue
        # For a device query, ignore accessories so "iphone 17" only matches the
        # phone, never a cover / screen protector / charger.
        if not query_is_accessory and (hay & ACCESSORY_WORDS):
            continue
        # fewer extra words in the candidate name -> closer match.
        precision = len(q_tokens) / max(len(set(name_tokens)), 1)
        in_stock = bool(p.get("available") or p.get("inStock"))
        price = p.get("price")
        price_key = price if isinstance(price, (int, float)) else float("inf")
        # A real product/SKU (flagged is_device by callers that separate the
        # actual device from add-ons) always beats a bundle/plan whose short
        # name would otherwise score a higher precision -- e.g. so "iphone air"
        # picks the phone, not an "iPhone Air MagSafe Battery". Absent the flag
        # (most platforms) this term is constant and has no effect.
        device_rank = 0 if p.get("is_device") else 1
        # Lowest tuple wins: real device first, then closest match, then the base
        # (smallest storage) variant, then in stock, then cheapest.
        key = (device_rank, -precision, _capacity_gb(p),
               0 if in_stock else 1, price_key)
        if best_key is None or key < best_key:
            best, best_key = p, key
    return best


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    products = read_lines(PRODUCTS_FILE)
    pincodes = read_lines(PINCODES_FILE)
    if not products or not pincodes:
        print("ERROR: fill products.txt and pincodes.txt (one item per line).")
        sys.exit(1)

    print(f"Checking {len(products)} product(s) across {len(pincodes)} pincode(s)...\n")
    cache = load_cache()
    session = requests.Session()

    rows = []
    for pin in pincodes:
        geo = geocode_pincode(pin, cache, session)
        lat, lon = geo["lat"], geo["lon"]
        if not lat:
            print(f"[{pin}] geocode failed -> skipping")
            for q in products:
                rows.append(base_row(pin, None, None, q, status="geocode_failed"))
            continue

        print(f"[{pin}] {lat},{lon}  ({(geo.get('place') or '')[:50]})")
        for q in products:
            serviceable, prods, code = blinkit_search(session, q, lat, lon)
            time.sleep(REQUEST_PAUSE)
            if serviceable is False:
                rows.append(base_row(pin, lat, lon, q, status="not_serviceable"))
                print(f"    - {q!r}: location not serviceable")
                continue
            if serviceable is None:
                rows.append(base_row(pin, lat, lon, q, status=f"error_{code}"))
                print(f"    - {q!r}: request error ({code})")
                continue
            match = best_match(q, prods)
            if not match:
                rows.append(base_row(pin, lat, lon, q, status="not_found"))
                print(f"    - {q!r}: not found in results ({len(prods)} cards)")
                continue
            r = base_row(pin, lat, lon, q,
                         status=("available" if match["available"] else "out_of_stock"))
            r.update({
                "matched_name": match["name"],
                "variant": match["variant"],
                "brand": match["brand"],
                "available": "yes" if match["available"] else "no",
                "price_rs": match["price"],
                "mrp_rs": match["mrp"],
                "inventory": match["inventory"],
                "eta": match["eta"],
                "merchant_id": match["merchant_id"],
                "product_id": match["product_id"],
            })
            rows.append(r)
            flag = "OK " if match["available"] else "OOS"
            print(f"    - {q!r}: {flag} {match['name']} {match['variant']} "
                  f"Rs.{match['price']} (inv {match['inventory']}, {match['eta']})")

    write_csv(rows)
    print(f"\nDone. Wrote {len(rows)} rows to {OUTPUT_CSV}")


def base_row(pin, lat, lon, query, status=""):
    return {
        "pincode": pin, "lat": lat, "lon": lon, "product_query": query,
        "status": status, "available": "", "matched_name": "", "variant": "",
        "brand": "", "price_rs": "", "mrp_rs": "", "inventory": "", "eta": "",
        "merchant_id": "", "product_id": "",
    }


def write_csv(rows):
    cols = ["pincode", "product_query", "status", "available", "matched_name",
            "variant", "brand", "price_rs", "mrp_rs", "inventory", "eta",
            "merchant_id", "product_id", "lat", "lon"]
    with open(OUTPUT_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


if __name__ == "__main__":
    main()
