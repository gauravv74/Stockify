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
MAX_RETRIES = 4
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
        impersonate=IMPERSONATE, timeout=30,
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
                        impersonate=IMPERSONATE, timeout=30)
        js = r.json()
        if js and js[0].get("Status") == "Success" and js[0].get("PostOffice"):
            po = js[0]["PostOffice"][0]
            parts = [po.get("Block") or po.get("Name"), po.get("District"), po.get("State")]
            return ", ".join([p for p in parts if p])
    except Exception:
        pass
    return None


def geocode_pincode(pin, cache, session):
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
                impersonate=IMPERSONATE, timeout=30,
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
        })
    return products


def _norm(s):
    return re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())


# Words that indicate an accessory rather than the actual device. When the
# query itself is not about an accessory, candidates containing these are
# demoted so "iphone 17" matches the phone, not a "iphone 17 cover".
ACCESSORY_WORDS = {
    "cover", "case", "guard", "protector", "tempered", "glass", "screen",
    "charger", "cable", "adapter", "skin", "pouch", "holder", "stand",
    "mount", "ring", "strap", "lens", "grip", "sleeve", "wallet",
}


def best_match(query, products):
    """Pick the product card whose name best matches the query tokens.

    Scoring rewards covering all query tokens and penalizes extra tokens in the
    candidate name (so the bare device beats accessories/bundles). Accessory
    products are demoted unless the query is itself about an accessory.
    """
    q_tokens = [t for t in _norm(query).split() if t]
    if not q_tokens:
        return products[0] if products else None
    # Every query token must appear in the candidate. This keeps "iphone 17"
    # from matching "iPhone 16" (missing 17) or a "17.4 g" weight (missing
    # iphone). Numbers matter as much as words for specific SKUs.
    query_is_accessory = any(t in ACCESSORY_WORDS for t in q_tokens)

    best, best_score = None, -1.0
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
        # fewer extra words in the candidate name -> closer match. Prefer an
        # in-stock variant when scores are otherwise equal.
        precision = len(q_tokens) / max(len(set(name_tokens)), 1)
        score = precision + (0.15 if p.get("available") or p.get("inStock") else 0)
        if score > best_score:
            best, best_score = p, score
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
