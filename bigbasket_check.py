#!/usr/bin/env python3
"""BigBasket (bbnow) product availability checker.

Unlike Swiggy Instamart / Zepto, BigBasket serves clean product JSON to a
curl_cffi session that impersonates Chrome -- no headless browser required.

Location is expressed entirely through cookies. The flow per location is:

  1. Seed a session by loading the home page (base + csrf cookies). Kept per
     thread and reused across checks; see BigBasket for why that is safe.
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

The national marketplace catalog is a *fallback*, not part of this flow: it is
fetched by match_row only when the local express store has no match for the
query. See BigBasket.marketplace_items.

Exposes a thread-safe singleton `client` with .check(lat, lon, query, pincode).
Matching reuses blinkit_check.best_match (accessory-aware).
"""

import base64
import json
import re
import threading
from urllib.parse import quote

from curl_cffi import requests

import blinkit_check as bk
import config
from stockly import offers

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
    """curl_cffi client.

    Concurrency: checks run under a bounded semaphore sized from
    ``config.PLATFORM_CONCURRENCY``, not a mutex. There is no shared mutable
    state to protect — the location-bearing session is thread-local — so the
    only reason to bound this at all is to stay polite to BigBasket.

    Session reuse: the express path reuses one seeded session per thread, which
    saves a full home-page round trip per check. That is only safe because
    :meth:`_set_location` rewrites *every* location-bearing cookie, including
    clearing the serving-area pair; the moment a location cookie is added
    without being reset there, one pincode's results can leak into another's.
    The rarely-taken marketplace and product-detail paths deliberately keep
    using throwaway sessions, since they need a *different* cookie shape and
    are not worth the risk.
    """

    # Every cookie that encodes "where the shopper is". Reused as the reset
    # list, so adding a location cookie without adding it here is the bug this
    # constant exists to prevent.
    LOCATION_COOKIES = ("_bb_lat_long", "_bb_addressinfo", "_bb_pin_code",
                        "_bb_locSrc", "_bb_sa_ids", "_bb_cda_sa_info")

    def __init__(self):
        self._slots = threading.BoundedSemaphore(config.platform_slots("bigbasket"))
        self._local = threading.local()

    @staticmethod
    def _seed():
        s = requests.Session()
        proxies = config.curl_proxies()
        if proxies:
            s.proxies = proxies
        s.get(BASE + "/", headers={"user-agent": UA},
              impersonate=IMPERSONATE, timeout=30)
        return s

    def _session(self):
        """A seeded session owned by this thread.

        Kept per-thread rather than per-client so concurrent checks cannot see
        each other's cookies, and per-thread rather than per-check so we do not
        pay ~0.9s re-seeding for every pincode.
        """
        s = getattr(self._local, "session", None)
        if s is None:
            s = self._local.session = self._seed()
        return s

    def _drop_session(self):
        """Forget this thread's session so the next check seeds a fresh one."""
        self._local.session = None

    @staticmethod
    def _set_cookie(s, name, value):
        s.cookies.set(name, value, domain=".bigbasket.com")

    @staticmethod
    def _clear_cookie(s, name):
        try:
            s.cookies.delete(name, domain=".bigbasket.com")
        except Exception:
            pass

    def _set_location(self, s, lat, lon, pincode):
        pin = str(pincode or "")
        info = f"{lat}|{lon}|Area|{pin}|City|1|false|true|true|Bigbasketeer"
        # Clear first: on a reused session the previous check's serving area is
        # still set, and _resolve_sa only overwrites it when it resolves one. A
        # location BigBasket refuses to serve would otherwise inherit the last
        # location's stores and report its stock.
        for name in self.LOCATION_COOKIES:
            self._clear_cookie(s, name)
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
        # Pass the raw query as the `slug` param; curl_cffi URL-encodes params
        # itself. Pre-encoding with quote() here double-encodes multi-word
        # queries (space -> %20 -> %2520), so BigBasket receives a corrupted
        # search term and returns almost nothing (e.g. "iphone 16" matched only
        # an accessory). Only the referer header needs manual encoding.
        term = query.strip()
        r = s.get(SEARCH_URL, params={"type": "ps", "slug": term, "page": "1"},
                  headers=_headers(BASE + "/ps/?q=" + quote(term)),
                  impersonate=IMPERSONATE, timeout=30)
        try:
            j = r.json()
            prods = j["tabs"][0]["product_info"]["products"]
        except Exception:
            return []
        items = _parse_products(prods)
        for it in items:
            it["source"] = "express"
        return items

    def _search_marketplace(self, query, pincode):
        """Search the full BigBasket marketplace catalog, scoped only by pincode.

        The express (bbNow) serving area resolved from lat/long carries a small
        catalog (~thousands of grocery essentials) that omits electronics/large
        appliances in many cities. Those items live in the standard BigBasket
        catalog, which ships pan-India and is what the mobile app shows for a
        pincode. A session carrying *only* `_bb_pin_code` (no lat/long / address
        / serving-area cookies) returns that full catalog, so device queries
        like "iphone 17" surface here even where the local express store lacks
        them.

        BEWARE: this catalog is national -- every item comes back `avail_status
        001` regardless of pincode, so the search alone CANNOT tell whether a
        marketplace item actually delivers to the given location. Items returned
        here are tagged `source="marketplace"` and must be verified per-pincode
        via `_pd_avail_status()` before being reported as in stock.
        """
        s = self._seed()
        if pincode:
            self._set_cookie(s, "_bb_pin_code", str(pincode))
        items = self._search(s, query)
        for it in items:
            it["source"] = "marketplace"
        return items

    def marketplace_items(self, query, pincode):
        """Marketplace catalog for ``query``, or ``[]`` if it can't be fetched.

        Public because :func:`match_row` calls it *lazily* — see there for why
        this is not part of the normal check.
        """
        try:
            return self._search_marketplace(query, pincode)
        except Exception:
            return []

    def _pd_avail_status(self, product_id, lat, lon, pincode):
        """Resolve the real per-location availability of a marketplace product.

        The listing search is national, but the product-detail page is rendered
        server-side using the delivery-location cookies and embeds the true
        availability for that lat/long/pincode in `__NEXT_DATA__`
        (`props.pageProps.productDetails`). Return values:
            "001"   -> deliverable & in stock
            "000"   -> serviceable but temporarily out of stock ("coming soon")
            "010"   -> not delivered to this location ("we are currently not
                       delivering this")
            "error" -> the PD service refused to serve the location (e.g.
                       PD4007 / SERVE3056 "No SA found for request"); the item
                       is not deliverable to this pincode.
            None    -> could not be determined (network/parse failure). The
                       caller must treat this conservatively, NOT as in stock.
        """
        if not product_id:
            return None
        last = None
        for attempt in range(2):
            try:
                s = self._seed()
                self._set_location(s, lat, lon, pincode)
                # The PD (BB2) serviceability service needs the resolved serving
                # area; without it the page errors with "No SA found".
                self._resolve_sa(s)
                r = s.get(f"{BASE}/pd/{product_id}/x/",
                          headers=_headers(BASE + "/"),
                          impersonate=IMPERSONATE, timeout=30)
                m = re.search(
                    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                    r.text, re.S)
                if not m:
                    last = None
                    continue
                data = json.loads(m.group(1))
                pd = (data.get("props", {}) or {}).get("pageProps", {}) or {}
                details = pd.get("productDetails")
                if isinstance(details, dict) and details.get("errors"):
                    # Serviceability error => not deliverable to this location.
                    return "error"
                found = []

                def walk(o):
                    if isinstance(o, dict):
                        if "avail_status" in o and "button" in o:
                            found.append(o.get("avail_status"))
                        for v in o.values():
                            walk(v)
                    elif isinstance(o, list):
                        for v in o:
                            walk(v)

                walk(data)
                if found:
                    return found[0]
                last = None
            except Exception:
                last = None
        return last

    def _query(self, lat, lon, query, pincode):
        s = self._session()
        self._set_location(s, lat, lon, pincode)
        sa_ids, sa_list = self._resolve_sa(s)
        if not sa_ids:
            return {"serviceable": False, "sa": [], "eta": "", "items": []}
        eta = ""
        for e in sa_list:
            if e.get("eta"):
                eta = e["eta"]
                break
        # Express (location-accurate) catalog only. The wider marketplace
        # catalog is fetched by match_row, and only when express has no match
        # for the query — it costs a second session plus a search, and it is
        # thrown away on every check where the local store stocks the item.
        return {"serviceable": True, "sa": sa_ids, "eta": eta,
                "items": self._search(s, query),
                "lat": lat, "lon": lon, "pincode": pincode}

    def check(self, lat, lon, query, pincode=None):
        with self._slots:
            try:
                return self._query(lat, lon, query, pincode)
            except Exception as e:
                # The thread's session may be half-dead (dropped connection,
                # expired cookies); reseed rather than reuse it for every
                # subsequent check on this thread.
                self._drop_session()
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
        pricing = p.get("pricing", {}) or {}
        disc = pricing.get("discount", {}) or {}
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
            # Usually an empty dict; populated only where a card offer exists.
            "best_offer": offers.from_bigbasket(pricing),
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
    # Match the location-accurate express catalog first; only if the query has
    # no express match (e.g. electronics the local bbNow store doesn't stock) do
    # we consider the wider national marketplace catalog -- whose stock flag is
    # location-agnostic and must be verified per-pincode via the product page.
    express = [it for it in items if it.get("source") != "marketplace"]

    m = bk.best_match(query, express)
    eta = result.get("eta") or ""
    in_stock = bool(m.get("inStock")) if m else False

    if not m:
        # Deferred until it is actually needed: for grocery queries express
        # nearly always matches, and fetching this eagerly was ~40% of every
        # check's latency for a catalog that was then discarded.
        market = [it for it in result.get("items", [])
                  if it.get("source") == "marketplace"]
        if not market:
            market = client.marketplace_items(query, result.get("pincode"))
        m = bk.best_match(query, market)
        if not m:
            return {"status": "not_found",
                    "merchant_id": ",".join(map(str, result.get("sa", [])))}
        st = client._pd_avail_status(
            m.get("product_id"), result.get("lat"), result.get("lon"),
            result.get("pincode"))
        # Only a confirmed "001" from the product page counts as in stock. The
        # search-level flag is national and must never be trusted for a
        # marketplace item, or we falsely report it deliverable everywhere.
        in_stock = (st == "001")
        if st in ("010", "error"):
            eta = "not delivered to this location"
        elif st == "000":
            eta = "temporarily out of stock"
        elif st is None:
            eta = "availability unverified"

    return {
        "status": "available" if in_stock else "out_of_stock",
        "available": "yes" if in_stock else "no",
        "name": m.get("name"), "variant": m.get("variant"), "brand": m.get("brand"),
        "price": m.get("price"), "mrp": m.get("mrp"), "inventory": "",
        "eta": eta, "merchant_id": ",".join(map(str, result.get("sa", []))),
        "best_offer": m.get("best_offer"),
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
