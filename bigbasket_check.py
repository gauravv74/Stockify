#!/usr/bin/env python3
"""BigBasket (bbnow) product availability checker.

Unlike Swiggy Instamart / Zepto, BigBasket serves clean product JSON to a
curl_cffi session that impersonates Chrome -- no headless browser required.

Location is expressed entirely through cookies. The flow per location is:

  1. Seed a session by loading the home page (base + csrf cookies).
  2. Set the delivery location by writing the same cookies the web app writes
     after you pick a place in the "Select Location" modal:
        _bb_lat_long     = base64("<lat>|<lon>")
        _bb_addressinfo  = base64("<lat>|<lon>|<area>|<pin>|<city>|1|...")
        _bb_pin_code     = <pin>
  3. Call /ui-svc/v2/header?send_door_info=true which resolves the serving
     areas (sa_list) for those coordinates. An empty sa_list => not serviceable.
  4. Write the resolved areas back as _bb_sa_ids / _bb_cda_sa_info cookies.
  5. Query /listing-svc/v2/products?type=ps&slug=<query> -> location-specific
     products, pricing and availability.

Exposes a thread-safe singleton `client` with .check(lat, lon, query, pincode).
Matching reuses blinkit_check.best_match (accessory-aware).
"""

import base64
import threading
from urllib.parse import quote

from curl_cffi import requests

import blinkit_check as bk

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")
IMPERSONATE = "chrome124"
BASE = "https://www.bigbasket.com"
HEADER_URL = BASE + "/ui-svc/v2/header/?send_door_info=true&send_address_set_by_user=true"
SEARCH_URL = BASE + "/listing-svc/v2/products"


def _b64(s):
    return base64.b64encode(s.encode()).decode()


def _headers(referer=BASE + "/"):
    return {"user-agent": UA, "accept": "application/json", "referer": referer}


class BigBasket:
    """curl_cffi client. A fresh, isolated session is used per check so that
    one location's serving-area/city cookies never leak into another's search
    results. Calls are serialized behind a lock to keep request rate polite."""

    def __init__(self):
        self._lock = threading.Lock()

    @staticmethod
    def _seed():
        s = requests.Session()
        s.get(BASE + "/", headers={"user-agent": UA},
              impersonate=IMPERSONATE, timeout=30)
        return s

    @staticmethod
    def _set_cookie(s, name, value):
        s.cookies.set(name, value, domain=".bigbasket.com")

    def _set_location(self, s, lat, lon, pincode):
        pin = str(pincode or "")
        info = f"{lat}|{lon}|Area|{pin}|City|1|false|true|true|Bigbasketeer"
        self._set_cookie(s, "_bb_lat_long", _b64(f"{lat}|{lon}"))
        self._set_cookie(s, "_bb_addressinfo", _b64(info))
        if pin:
            self._set_cookie(s, "_bb_pin_code", pin)
        self._set_cookie(s, "_bb_locSrc", "gps")

    def _resolve_sa(self, s):
        """Return (sa_ids:list[int], sa_list:list[dict]) for the current cookies."""
        r = s.get(HEADER_URL, headers=_headers(), impersonate=IMPERSONATE, timeout=30)
        try:
            j = r.json()
        except Exception:
            return [], []
        sa_list = j.get("sa_list", []) or []
        sa_ids = [x.get("sa_id") for x in sa_list if x.get("sa_id") is not None]
        if sa_ids:
            ss = ",".join(str(x) for x in sa_ids)
            self._set_cookie(s, "_bb_sa_ids", ss)
            self._set_cookie(s, "_bb_cda_sa_info", _b64("v2.cda_sa.10." + ss))
        return sa_ids, sa_list

    def _search(self, s, query):
        slug = quote(query.strip())
        r = s.get(SEARCH_URL, params={"type": "ps", "slug": slug, "page": "1"},
                  headers=_headers(BASE + "/ps/?q=" + slug),
                  impersonate=IMPERSONATE, timeout=30)
        try:
            j = r.json()
            prods = j["tabs"][0]["product_info"]["products"]
        except Exception:
            return []
        return _parse_products(prods)

    def _query(self, lat, lon, query, pincode):
        s = self._seed()
        self._set_location(s, lat, lon, pincode)
        sa_ids, sa_list = self._resolve_sa(s)
        if not sa_ids:
            return {"serviceable": False, "sa": [], "eta": "", "items": []}
        eta = ""
        for e in sa_list:
            if e.get("eta"):
                eta = e["eta"]
                break
        items = self._search(s, query)
        return {"serviceable": True, "sa": sa_ids, "eta": eta, "items": items}

    def check(self, lat, lon, query, pincode=None):
        with self._lock:
            try:
                return self._query(lat, lon, query, pincode)
            except Exception as e:
                return {"serviceable": None, "sa": [], "eta": "",
                        "items": [], "error": str(e)}


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _parse_products(prods):
    out = []
    for p in prods:
        brand = p.get("brand")
        if isinstance(brand, dict):
            brand = brand.get("name") or ""
        avail = p.get("availability", {}) or {}
        disc = (p.get("pricing", {}) or {}).get("discount", {}) or {}
        prim = disc.get("prim_price", {}) or {}
        in_stock = (avail.get("avail_status") == "001") and not avail.get("not_for_sale")
        out.append({
            "name": p.get("desc") or "",
            "variant": p.get("w") or p.get("pack_desc") or "",
            "brand": brand or "",
            "price": _num(prim.get("sp")),
            "mrp": _num(disc.get("mrp")),
            "inStock": in_stock,
            "eta": "",
            "product_id": p.get("id"),
        })
    return out


client = BigBasket()


def match_row(query, result):
    """Turn a raw check() result into a normalized row like the Blinkit checker."""
    if result.get("serviceable") is None:
        return {"status": "error", "detail": result.get("error", "")}
    if result.get("serviceable") is False:
        return {"status": "not_serviceable"}
    items = result.get("items", [])
    m = bk.best_match(query, items)
    if not m:
        return {"status": "not_found", "merchant_id": ",".join(map(str, result.get("sa", [])))}
    return {
        "status": "available" if m.get("inStock") else "out_of_stock",
        "available": "yes" if m.get("inStock") else "no",
        "name": m.get("name"), "variant": m.get("variant"), "brand": m.get("brand"),
        "price": m.get("price"), "mrp": m.get("mrp"), "inventory": "",
        "eta": result.get("eta") or "", "merchant_id": ",".join(map(str, result.get("sa", []))),
    }


if __name__ == "__main__":
    for lat, lon, pin, label in [
        ("19.1364016", "72.8296252", "400053", "Mumbai Andheri"),
        ("12.9716", "77.5946", "560001", "Bangalore"),
        ("34.152588", "77.577049", "194101", "Leh (remote)"),
    ]:
        r = client.check(lat, lon, "amul gold milk", pin)
        print(label, "sa:", r.get("sa"), "items:", len(r.get("items", [])),
              "->", match_row("amul gold milk", r))
