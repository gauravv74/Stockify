#!/usr/bin/env python3
"""Stockly — multi-platform product availability checker (production-ready)."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from datetime import timedelta

from flask import Flask, jsonify, request, send_from_directory, Response
from curl_cffi import requests as cffi_requests
from werkzeug.middleware.proxy_fix import ProxyFix

import threading

import auth
import blinkit_check as bk
import config
import jobs
import watches
import whatsapp

logging.basicConfig(
    level=logging.INFO if config.IS_PROD else logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("stockly")

ALL_PLATFORMS = auth.ALL_PLATFORMS

app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = auth.ensure_secret_key()
app.permanent_session_lifetime = timedelta(days=config.SESSION_DAYS)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE=config.COOKIE_SAMESITE,
    SESSION_COOKIE_SECURE=config.COOKIE_SECURE,
    SESSION_COOKIE_NAME="stockly_session",
)

if config.TRUST_PROXY:
    # nginx / load balancer terminates TLS
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

_created_default_admin = False
_, _created_default_admin = auth.ensure_users_file()
watches.init_db()
jobs.init_db()
if _created_default_admin:
    log.warning(
        "Default admin created (username=%s). Change password immediately.",
        auth.DEFAULT_ADMIN_USER,
    )


def load_cities():
    with open(config.CITIES_FILE) as f:
        return json.load(f).get("cities", [])


def city_index():
    return {c["id"]: c for c in load_cities()}


def parse_products(raw):
    if isinstance(raw, list):
        items = raw
    else:
        items = re.split(r"[\n,]+", str(raw))
    out = []
    for p in items:
        p = str(p).strip()
        if p:
            out.append(p)
    return out


def parse_products_with_thresholds(raw):
    """Parse product entries, each optionally suffixed with ``@<price>`` to set a
    per-product target price (e.g. ``"oppo k14 6/128 @14300"``).

    Returns a list of ``(product, threshold_or_None)`` tuples.
    """
    out = []
    for p in parse_products(raw):
        m = re.search(r"@\s*([0-9][0-9,]*\.?[0-9]*)\s*$", p)
        if m:
            name = p[:m.start()].strip()
            try:
                thr = float(m.group(1).replace(",", ""))
            except ValueError:
                thr = None
            if name:
                out.append((name, thr))
                continue
        out.append((p, None))
    return out


def _bridge_headers():
    return {"X-Auth-Token": config.WA_BRIDGE_TOKEN} if config.WA_BRIDGE_TOKEN else {}


def _bridge_url():
    """Bridge base URL. A runtime setting overrides the env default so the web
    container can reach the wa-bridge service by name without a recreate."""
    return (watches.get_setting("wa_bridge_url") or config.WA_BRIDGE_URL).rstrip("/")


def parse_raw_pincodes(raw):
    items = raw if isinstance(raw, list) else re.findall(r"\d{6}", str(raw or ""))
    seen, out = set(), []
    for p in items:
        p = str(p).strip()
        if re.fullmatch(r"\d{6}", p) and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def resolve_pincodes(payload):
    cities = payload.get("cities") or []
    if isinstance(cities, str):
        cities = [c.strip() for c in re.split(r"[\n,]+", cities) if c.strip()]

    seen, out, selected = set(), [], []
    if cities:
        index = city_index()
        for cid in cities:
            key = str(cid).strip().lower().replace(" ", "-")
            city = index.get(key)
            if not city:
                city = next((c for c in index.values()
                             if c["name"].lower() == str(cid).strip().lower()), None)
            if not city:
                continue
            selected.append({"id": city["id"], "name": city["name"], "count": city["count"]})
            for pin in city["pincodes"]:
                if pin not in seen:
                    seen.add(pin)
                    out.append(pin)

    for pin in parse_raw_pincodes(payload.get("pincodes", [])):
        if pin not in seen:
            seen.add(pin)
            out.append(pin)

    return out, selected


def resolve_platforms(platform, allowed):
    platform = (platform or "blinkit").lower()
    allowed = list(allowed or [])
    if not allowed:
        return []
    if platform == "all":
        return allowed
    if platform in allowed:
        return [platform]
    return []


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "service": "stockly",
        "env": config.ENV,
        "db": str(config.DB_PATH),
    })


@app.route("/api/login", methods=["POST"])
def api_login():
    payload = request.get_json(force=True, silent=True) or {}
    user = auth.authenticate(payload.get("username"), payload.get("password"))
    if not user:
        return jsonify({"error": "Invalid username or password"}), 401
    auth.login_user(user)
    return jsonify({
        "user": user,
        "platforms": auth.allowed_platforms(user),
        "must_change_password": bool(user.get("must_change_password")),
    })


@app.route("/api/logout", methods=["POST"])
def api_logout():
    auth.logout_user()
    return jsonify({"ok": True})


@app.route("/api/me")
def api_me():
    user = auth.current_user()
    if not user:
        return jsonify({"user": None}), 401
    return jsonify({
        "user": user,
        "platforms": auth.allowed_platforms(user),
        "must_change_password": bool(user.get("must_change_password")),
    })


@app.route("/api/change-password", methods=["POST"])
@auth.login_required
def api_change_password():
    payload = request.get_json(force=True, silent=True) or {}
    me = auth.current_user()
    user, err = auth.change_password(
        me["id"],
        payload.get("current_password"),
        payload.get("new_password"),
    )
    if err:
        return jsonify({"error": err}), 400
    auth.login_user(user)  # refresh session claims
    return jsonify({
        "user": user,
        "platforms": auth.allowed_platforms(user),
        "must_change_password": False,
    })


@app.route("/api/admin/users", methods=["GET"])
@auth.admin_required
def admin_list_users():
    return jsonify({"users": auth.list_users()})


@app.route("/api/admin/users", methods=["POST"])
@auth.admin_required
def admin_create_user():
    payload = request.get_json(force=True, silent=True) or {}
    user, err = auth.create_user(
        payload.get("username"),
        payload.get("password"),
        platforms=payload.get("platforms"),
        role=payload.get("role") or "user",
        cities=payload.get("cities"),
        allow_pincodes=payload.get("allow_pincodes", True),
    )
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"user": user}), 201


@app.route("/api/admin/users/<user_id>", methods=["PATCH"])
@auth.admin_required
def admin_update_user(user_id):
    payload = request.get_json(force=True, silent=True) or {}
    user, err = auth.update_user(
        user_id,
        platforms=payload.get("platforms"),
        active=payload.get("active"),
        password=payload.get("password"),
        role=payload.get("role"),
        cities=payload.get("cities"),
        allow_pincodes=payload.get("allow_pincodes"),
    )
    if err:
        code = 404 if err == "User not found." else 400
        return jsonify({"error": err}), code
    return jsonify({"user": user})


@app.route("/api/admin/users/<user_id>", methods=["DELETE"])
@auth.admin_required
def admin_delete_user(user_id):
    me = auth.current_user()
    if me and me["id"] == user_id:
        return jsonify({"error": "Cannot delete your own account while logged in."}), 400
    ok, err = auth.delete_user(user_id)
    if not ok:
        code = 404 if err == "User not found." else 400
        return jsonify({"error": err}), code
    return jsonify({"ok": True})


@app.route("/api/admin/searches")
@auth.admin_required
def admin_list_searches():
    try:
        limit = int(request.args.get("limit", 200))
    except (TypeError, ValueError):
        limit = 200
    return jsonify({"searches": auth.list_searches(limit)})


@app.route("/api/cities")
@auth.login_required
def api_cities():
    allowed = auth.allowed_cities(auth.current_user())  # None == all cities
    cities = [
        {"id": c["id"], "name": c["name"], "state": c.get("state", ""), "count": c["count"]}
        for c in load_cities()
        if allowed is None or c["id"] in allowed
    ]
    return jsonify({"cities": cities})


@app.route("/api/geocode")
@auth.login_required
def api_geocode():
    """Resolve a pincode to lat/lon/place so the UI can use it as a distance
    reference ("order pincodes nearest to <this location>"). Reuses the shared
    geocode cache, so repeated lookups are free."""
    pin = (request.args.get("pincode") or "").strip()
    if not re.fullmatch(r"\d{6}", pin):
        return jsonify({"error": "Enter a valid 6-digit pincode."}), 400
    session = cffi_requests.Session()
    cache = bk.load_cache()
    geo = bk.geocode_pincode(pin, cache, session)
    if not geo.get("lat"):
        return jsonify({"error": f"Couldn't locate pincode {pin}."}), 404
    return jsonify({
        "pincode": pin,
        "lat": geo.get("lat"),
        "lon": geo.get("lon"),
        "place": geo.get("place"),
    })


def _blank_row(idx, pin, place, lat, lon, product, platform):
    return {
        "type": "result", "index": idx, "pincode": pin, "platform": platform,
        "location": place or "", "lat": lat, "lon": lon, "product": product,
        "status": "", "available": "", "name": "", "variant": "", "brand": "",
        "price": "", "mrp": "", "inventory": "", "eta": "", "merchant_id": "",
    }


def _check_blinkit(session, q, lat, lon):
    serviceable, prods, code = bk.blinkit_search(session, q, lat, lon)
    time.sleep(bk.REQUEST_PAUSE)
    if serviceable is False:
        return {"status": "not_serviceable"}
    if serviceable is None:
        return {"status": f"error_{code}"}
    m = bk.best_match(q, prods)
    if not m:
        return {"status": "not_found"}
    return {
        "status": "available" if m["available"] else "out_of_stock",
        "available": "yes" if m["available"] else "no",
        "name": m["name"], "variant": m["variant"], "brand": m["brand"],
        "price": m["price"], "mrp": m["mrp"], "inventory": m["inventory"],
        "eta": m["eta"], "merchant_id": m["merchant_id"],
    }


def _check_instamart(q, lat, lon):
    import swiggy_check as sw
    res = sw.client.check(float(lat), float(lon), q)
    return sw.match_row(q, res)


def _check_zepto(q, lat, lon):
    import zepto_check as zp
    res = zp.client.check(float(lat), float(lon), q)
    return zp.match_row(q, res)


def _check_bigbasket(q, lat, lon, pin):
    import bigbasket_check as bb
    res = bb.client.check(str(lat), str(lon), q, pin)
    return bb.match_row(q, res)


def _check_flipkart(q, lat, lon):
    import flipkart_check as fk
    res = fk.client.check(float(lat), float(lon), q)
    return fk.match_row(q, res)


def _check_jiomart(q, lat, lon, pin):
    import jiomart_check as jm
    res = jm.client.check(float(lat), float(lon), q, pin)
    return jm.match_row(q, res)


def _check_apple(q, lat, lon, pin):
    import apple_check as ap
    res = ap.client.check(float(lat), float(lon), q, pin)
    return ap.match_row(q, res)


def _check_croma(q, lat, lon, pin):
    import croma_check as cr
    res = cr.client.check(float(lat), float(lon), q, pin)
    return cr.match_row(q, res)


def _check_one(platform, session, q, lat, lon, pin):
    if platform == "instamart":
        return _check_instamart(q, lat, lon)
    if platform == "zepto":
        return _check_zepto(q, lat, lon)
    if platform == "bigbasket":
        return _check_bigbasket(q, lat, lon, pin)
    if platform == "flipkart":
        return _check_flipkart(q, lat, lon)
    if platform == "jiomart":
        return _check_jiomart(q, lat, lon, pin)
    if platform == "apple":
        return _check_apple(q, lat, lon, pin)
    if platform == "croma":
        return _check_croma(q, lat, lon, pin)
    return _check_blinkit(session, q, lat, lon)


# ---------------------------------------------------------------------------
# Product picker — return the raw list of products a platform surfaces for a
# query at one reference location, so the user can pick the exact SKU to track
# instead of relying purely on free-text fuzzy matching.
# ---------------------------------------------------------------------------
def _platform_raw_check(platform, q, lat, lon, pin):
    """Call a browser-backed platform client and return its raw check() dict."""
    if platform == "instamart":
        import swiggy_check as sw
        return sw.client.check(float(lat), float(lon), q)
    if platform == "zepto":
        import zepto_check as zp
        return zp.client.check(float(lat), float(lon), q)
    if platform == "bigbasket":
        import bigbasket_check as bb
        return bb.client.check(str(lat), str(lon), q, pin)
    if platform == "flipkart":
        import flipkart_check as fk
        return fk.client.check(float(lat), float(lon), q)
    if platform == "jiomart":
        import jiomart_check as jm
        return jm.client.check(float(lat), float(lon), q, pin)
    if platform == "apple":
        import apple_check as ap
        return ap.client.check(float(lat), float(lon), q, pin)
    if platform == "croma":
        import croma_check as cr
        return cr.client.check(float(lat), float(lon), q, pin)
    return {"serviceable": None, "items": []}


def _norm_option(p):
    """Normalize a platform product card into a compact, UI-friendly option."""
    in_stock = p.get("inStock")
    if in_stock is None:
        in_stock = p.get("available")
    return {
        "name": (p.get("name") or "").strip(),
        "variant": (p.get("variant") or "").strip(),
        "brand": (p.get("brand") or "").strip(),
        "price": p.get("price"),
        "mrp": p.get("mrp"),
        "inStock": bool(in_stock),
        "eta": (p.get("eta") or "").strip(),
    }


def _platform_items(platform, session, q, lat, lon, pin, limit=40):
    """Return (serviceable, options) for a query on one platform at a location.

    `serviceable` is True/False/None (None == transient/couldn't reach). Options
    are de-duplicated by name+variant and capped at `limit`.
    """
    if platform == "blinkit":
        serviceable, prods, _code = bk.blinkit_search(session, q, lat, lon)
        time.sleep(bk.REQUEST_PAUSE)
        raw = prods if serviceable else []
    else:
        res = _platform_raw_check(platform, q, lat, lon, pin)
        serviceable = res.get("serviceable")
        raw = res.get("items") or []

    seen, options = set(), []
    for p in raw:
        opt = _norm_option(p)
        if not opt["name"]:
            continue
        key = (opt["name"].lower(), opt["variant"].lower())
        if key in seen:
            continue
        seen.add(key)
        options.append(opt)
        if len(options) >= limit:
            break
    return serviceable, options


def _haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km between two lat/lon points."""
    r = 6371.0
    p = math.pi / 180.0
    dlat = (lat2 - lat1) * p
    dlon = (lon2 - lon1) * p
    a = (math.sin(dlat / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin(dlon / 2) ** 2)
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _order_pincodes_by_distance(pincodes, cache, ref_lat, ref_lon):
    """Return pincodes ordered nearest-first from (ref_lat, ref_lon).

    Uses only already-cached geocodes (no blocking network calls), so the run
    starts immediately. Pincodes without a cached coordinate keep their original
    relative order and go last; they're geocoded normally when processed, and
    become correctly ordered on the next run once the cache is warm.
    """
    def sort_key(i_pin):
        i, pin = i_pin
        g = cache.get(pin) or {}
        la, lo = g.get("lat"), g.get("lon")
        try:
            if la not in (None, "") and lo not in (None, ""):
                return (0, _haversine_km(ref_lat, ref_lon, float(la), float(lo)), i)
        except (TypeError, ValueError):
            pass
        return (1, 0.0, i)

    return [pin for _, pin in sorted(enumerate(pincodes), key=sort_key)]


def _run_check_job(job_id, pincodes, products, platforms,
                   ref_lat=None, ref_lon=None, order_by=None):
    """Execute a search run in the background, persisting each result row.

    Runs in a daemon thread decoupled from any HTTP request, so the run keeps
    going even if the user backgrounds the tab, locks the phone, or reloads —
    the browser just re-polls ``/api/check/poll`` from its cursor. Cancellation
    is cooperative: we check the job's cancel flag between checks.

    When ``order_by == 'distance'`` and a reference point is given, pincodes are
    processed nearest-first so the most relevant results stream in first.
    """
    try:
        cache = bk.load_cache()
        session = cffi_requests.Session()
        if order_by == "distance" and ref_lat is not None and ref_lon is not None:
            pincodes = _order_pincodes_by_distance(pincodes, cache, ref_lat, ref_lon)
        idx = 0
        canceled = False
        for pin in pincodes:
            if jobs.is_canceled(job_id):
                canceled = True
                break
            geo = bk.geocode_pincode(pin, cache, session)
            lat, lon, place = geo.get("lat"), geo.get("lon"), geo.get("place")

            if not lat:
                for q in products:
                    for plat in platforms:
                        idx += 1
                        row = _blank_row(idx, pin, "", None, None, q, plat)
                        row["status"] = "geocode_failed"
                        jobs.add_event(job_id, row)
                continue

            for q in products:
                if jobs.is_canceled(job_id):
                    canceled = True
                    break
                for plat in platforms:
                    # Cooperative cancel between platforms too, so Stop reacts
                    # after the single in-flight check instead of finishing the
                    # whole platform set for this product.
                    if jobs.is_canceled(job_id):
                        canceled = True
                        break
                    idx += 1
                    row = _blank_row(idx, pin, place, lat, lon, q, plat)
                    try:
                        row.update(_check_one(plat, session, q, lat, lon, pin))
                    except Exception as e:
                        log.exception("check failed pin=%s plat=%s q=%s", pin, plat, q)
                        row["status"] = "error"
                        row["detail"] = str(e)[:200]
                    jobs.add_event(job_id, row)
            if canceled:
                break

        jobs.set_status(job_id, "canceled" if (canceled or jobs.is_canceled(job_id)) else "done")
    except Exception as e:
        log.exception("search job %s crashed", job_id)
        jobs.set_status(job_id, "error", detail=str(e)[:200])


@app.route("/api/check/start", methods=["POST"])
@auth.login_required
def check_start():
    """Kick off a background search run and return its job id + meta. The
    browser then polls ``/api/check/poll`` for results, so the run survives the
    tab being backgrounded / suspended / reloaded."""
    user = auth.current_user()
    allowed = auth.allowed_platforms(user)
    payload = request.get_json(force=True, silent=True) or {}

    # Enforce per-user location access: drop cities the user isn't granted, and
    # ignore any custom pincodes if the user isn't allowed to enter them.
    allowed_city_ids = auth.allowed_cities(user)  # None == unrestricted
    if allowed_city_ids is not None:
        req_cities = payload.get("cities") or []
        if isinstance(req_cities, str):
            req_cities = [c.strip() for c in re.split(r"[\n,]+", req_cities) if c.strip()]
        payload["cities"] = [
            c for c in req_cities
            if str(c).strip().lower().replace(" ", "-") in allowed_city_ids
        ]
    if not auth.can_use_pincodes(user):
        payload["pincodes"] = []

    pincodes, selected_cities = resolve_pincodes(payload)
    products = parse_products(payload.get("products", [])) or ["iphone 17"]
    platforms = resolve_platforms(payload.get("platform"), allowed)
    multi = len(platforms) > 1

    # Optional: check nearest pincodes first, relative to a reference point
    # (the user's current location or a chosen pincode).
    order_by = (str(payload.get("order_by") or "").strip().lower()) or None
    try:
        ref_lat = float(payload["ref_lat"]) if payload.get("ref_lat") not in (None, "") else None
        ref_lon = float(payload["ref_lon"]) if payload.get("ref_lon") not in (None, "") else None
    except (TypeError, ValueError):
        ref_lat = ref_lon = None

    if not platforms:
        return jsonify({
            "error": "No platform access. Ask an admin to enable Blinkit / Instamart / Zepto / BigBasket / Flipkart Minutes / JioMart / Apple / Croma for your account."
        }), 403
    if not pincodes:
        return jsonify({"error": "Select a city and/or enter at least one pincode."}), 400

    total = len(pincodes) * len(products) * len(platforms)

    # Audit the request so admins can see what users searched.
    auth.log_search(
        user,
        "all" if multi else platforms[0],
        products,
        [c["name"] for c in selected_cities],
        len(pincodes),
        total,
    )

    meta = {
        "total": total,
        "platform": "all" if multi else platforms[0],
        "platforms": platforms,
        "pincodes": len(pincodes),
        "products": products,
        "cities": selected_cities,
    }
    job_id = jobs.create_job(user.get("id"), meta, total)
    t = threading.Thread(
        target=_run_check_job,
        args=(job_id, pincodes, products, platforms),
        kwargs={"ref_lat": ref_lat, "ref_lon": ref_lon, "order_by": order_by},
        daemon=True)
    t.start()

    return jsonify({"job_id": job_id, "meta": meta})


@app.route("/api/check/poll")
@auth.login_required
def check_poll():
    """Return any result rows past ``cursor`` plus the run's current status, so
    the browser can pick up exactly where it left off after being away."""
    user = auth.current_user()
    job_id = (request.args.get("job_id") or "").strip()
    try:
        cursor = int(request.args.get("cursor", 0))
    except (TypeError, ValueError):
        cursor = 0

    job = jobs.get_job(job_id, user_id=user.get("id"))
    if not job:
        return jsonify({"error": "Job not found."}), 404

    events = jobs.get_events(job_id, cursor)
    next_cursor = events[-1]["seq"] if events else cursor
    return jsonify({
        "status": job["status"],           # running | done | canceled | error
        "total": job["total"],
        "meta": job["meta"],
        "detail": job.get("detail"),
        "events": events,
        "cursor": next_cursor,
    })


@app.route("/api/check/cancel", methods=["POST"])
@auth.login_required
def check_cancel():
    user = auth.current_user()
    payload = request.get_json(force=True, silent=True) or {}
    job_id = (payload.get("job_id") or "").strip()
    ok = jobs.request_cancel(job_id, user_id=user.get("id"))
    if not ok:
        return jsonify({"error": "Job not found."}), 404
    return jsonify({"ok": True})


@app.route("/api/product-options", methods=["POST"])
@auth.login_required
def api_product_options():
    """Browse the products a single platform surfaces for a free-text query at
    one reference location, so the user can pick the exact SKU to check/track."""
    user = auth.current_user()
    allowed = auth.allowed_platforms(user)
    payload = request.get_json(force=True, silent=True) or {}

    query = (payload.get("query") or "").strip()
    if not query:
        # Fall back to the first free-text product if a bare query wasn't sent.
        query = (parse_products(payload.get("products", [])) or [""])[0]
    if not query:
        return jsonify({"error": "Type a product to search."}), 400

    # Same platform + location access enforcement as /api/check.
    allowed_city_ids = auth.allowed_cities(user)
    if allowed_city_ids is not None:
        req_cities = payload.get("cities") or []
        if isinstance(req_cities, str):
            req_cities = [c.strip() for c in re.split(r"[\n,]+", req_cities) if c.strip()]
        payload["cities"] = [
            c for c in req_cities
            if str(c).strip().lower().replace(" ", "-") in allowed_city_ids
        ]
    if not auth.can_use_pincodes(user):
        payload["pincodes"] = []

    platforms = resolve_platforms(payload.get("platform"), allowed)
    if not platforms:
        return jsonify({"error": "No access to the requested platform."}), 403
    if len(platforms) != 1:
        return jsonify({"error": "Pick a single platform to browse products."}), 400
    platform = platforms[0]

    pincodes, _selected = resolve_pincodes(payload)
    if not pincodes:
        return jsonify({"error": "Select a city or enter a pincode first."}), 400
    pin = pincodes[0]

    session = cffi_requests.Session()
    cache = bk.load_cache()
    geo = bk.geocode_pincode(pin, cache, session)
    lat, lon, place = geo.get("lat"), geo.get("lon"), geo.get("place")
    if not lat or not lon:
        return jsonify({"error": f"Couldn't locate pincode {pin}."}), 400

    try:
        serviceable, options = _platform_items(platform, session, query, lat, lon, pin)
    except Exception as e:
        log.exception("product-options failed plat=%s q=%s pin=%s", platform, query, pin)
        return jsonify({"error": str(e)[:200]}), 502

    if serviceable is False:
        return jsonify({
            "options": [], "serviceable": False, "platform": platform,
            "location": {"pincode": pin, "place": place},
        })
    if serviceable is None:
        return jsonify({
            "error": "Couldn't reach the platform right now — please retry.",
            "platform": platform, "location": {"pincode": pin, "place": place},
        }), 502
    return jsonify({
        "options": options, "serviceable": True, "platform": platform,
        "query": query, "location": {"pincode": pin, "place": place},
    })


# ---------------------------------------------------------------------------
# Stock watches — a user registers products to monitor; worker.py polls them
# every STOCKLY_WATCH_INTERVAL_MIN minutes and WhatsApps on any change.
# ---------------------------------------------------------------------------
@app.route("/api/watches", methods=["GET"])
@auth.login_required
def api_list_watches():
    user = auth.current_user()
    # Admins see every watch; regular users see only their own.
    uid = None if user.get("role") == "admin" else user["id"]
    return jsonify({
        "watches": watches.list_watches(user_id=uid),
        "interval_min": watches.get_interval_min(),
        "whatsapp": {
            "provider": config.WHATSAPP_PROVIDER,
            "configured": whatsapp.is_configured(),
            "notify_on": watches.get_notify_mode(),
            "modes": list(watches.NOTIFY_MODES),
        },
    })


@app.route("/api/watches", methods=["POST"])
@auth.login_required
def api_create_watches():
    """Register watches for the cartesian product of products x pincodes x
    platforms, honouring the caller's platform + location access."""
    user = auth.current_user()
    allowed = auth.allowed_platforms(user)
    payload = request.get_json(force=True, silent=True) or {}

    # Same access enforcement as /api/check.
    allowed_city_ids = auth.allowed_cities(user)
    if allowed_city_ids is not None:
        req_cities = payload.get("cities") or []
        if isinstance(req_cities, str):
            req_cities = [c.strip() for c in re.split(r"[\n,]+", req_cities) if c.strip()]
        payload["cities"] = [
            c for c in req_cities
            if str(c).strip().lower().replace(" ", "-") in allowed_city_ids
        ]
    if not auth.can_use_pincodes(user):
        payload["pincodes"] = []

    pincodes, _selected = resolve_pincodes(payload)
    product_specs = parse_products_with_thresholds(payload.get("products", []))
    platforms = resolve_platforms(payload.get("platform"), allowed)
    notify_to = (payload.get("notify_to") or "").strip() or None
    # A single "target price" field applies to products that don't carry their
    # own inline "@price"; inline thresholds always win.
    try:
        default_threshold = float(payload["price_threshold"]) if str(
            payload.get("price_threshold", "")).strip() != "" else None
    except (TypeError, ValueError):
        default_threshold = None

    if not platforms:
        return jsonify({"error": "No platform access for the requested platform."}), 403
    if not product_specs:
        return jsonify({"error": "Enter at least one product."}), 400
    if not pincodes:
        return jsonify({"error": "Select a city and/or enter at least one pincode."}), 400

    created, errors = [], []
    for pin in pincodes:
        for q, thr in product_specs:
            threshold = thr if thr is not None else default_threshold
            for plat in platforms:
                w, err = watches.add_watch(user, plat, q, pin, notify_to=notify_to,
                                           price_threshold=threshold)
                if w:
                    created.append(w)
                elif err:
                    errors.append({"product": q, "pincode": pin, "platform": plat, "error": err})
    return jsonify({"created": len(created), "watches": created, "errors": errors}), 201


@app.route("/api/watches/<int:watch_id>", methods=["PATCH"])
@auth.login_required
def api_update_watch(watch_id):
    user = auth.current_user()
    uid = None if user.get("role") == "admin" else user["id"]
    payload = request.get_json(force=True, silent=True) or {}
    if "active" not in payload:
        return jsonify({"error": "Nothing to update."}), 400
    ok = watches.set_active(watch_id, bool(payload.get("active")), user_id=uid)
    if not ok:
        return jsonify({"error": "Watch not found."}), 404
    return jsonify({"watch": watches.get_watch(watch_id)})


@app.route("/api/watches/<int:watch_id>", methods=["DELETE"])
@auth.login_required
def api_delete_watch(watch_id):
    user = auth.current_user()
    uid = None if user.get("role") == "admin" else user["id"]
    if not watches.delete_watch(watch_id, user_id=uid):
        return jsonify({"error": "Watch not found."}), 404
    return jsonify({"ok": True})


@app.route("/api/watches/test-whatsapp", methods=["POST"])
@auth.login_required
def api_test_whatsapp():
    payload = request.get_json(force=True, silent=True) or {}
    to = (payload.get("to") or "").strip() or None
    ok, detail = whatsapp.send(
        "Stockly ✅ test alert — WhatsApp notifications are wired up.", to=to)
    return jsonify({"ok": ok, "detail": detail, "provider": config.WHATSAPP_PROVIDER}), (
        200 if ok else 502)


# ---------------------------------------------------------------------------
# Admin: global alert mode + WhatsApp Web (sending account) management.
# ---------------------------------------------------------------------------
@app.route("/api/admin/settings", methods=["GET"])
@auth.admin_required
def admin_get_settings():
    return jsonify({
        "notify_on": watches.get_notify_mode(),
        "modes": list(watches.NOTIFY_MODES),
        "interval_min": watches.get_interval_min(),
    })


@app.route("/api/admin/settings", methods=["POST"])
@auth.admin_required
def admin_set_settings():
    payload = request.get_json(force=True, silent=True) or {}
    mode = (payload.get("notify_on") or "").strip().lower()
    if mode and mode not in watches.NOTIFY_MODES:
        return jsonify({"error": f"Invalid mode. Use one of {list(watches.NOTIFY_MODES)}."}), 400
    if mode:
        watches.set_setting("notify_on", mode)
    if str(payload.get("interval_min", "")).strip() != "":
        try:
            interval = max(1, int(payload["interval_min"]))
        except (TypeError, ValueError):
            return jsonify({"error": "interval_min must be a positive integer."}), 400
        watches.set_setting("interval_min", interval)
    return jsonify({"ok": True, "notify_on": watches.get_notify_mode(),
                    "interval_min": watches.get_interval_min()})


@app.route("/api/admin/whatsapp/status")
@auth.admin_required
def admin_whatsapp_status():
    if config.WHATSAPP_PROVIDER != "webjs":
        return jsonify({"ok": False, "provider": config.WHATSAPP_PROVIDER,
                        "error": "WhatsApp Web bridge is only used when provider=webjs."}), 200
    try:
        r = cffi_requests.get(_bridge_url() + "/status",
                              headers=_bridge_headers(), timeout=10)
        data = r.json()
    except Exception as e:
        return jsonify({"ok": False, "error": f"bridge unreachable: {e}",
                        "bridge_url": config.WA_BRIDGE_URL}), 502
    data["ok"] = True
    data["provider"] = config.WHATSAPP_PROVIDER
    return jsonify(data)


@app.route("/api/admin/whatsapp/qr")
@auth.admin_required
def admin_whatsapp_qr():
    try:
        r = cffi_requests.get(_bridge_url() + "/qr",
                              headers=_bridge_headers(), timeout=10)
    except Exception as e:
        return jsonify({"error": f"bridge unreachable: {e}"}), 502
    if r.status_code == 204:
        return ("", 204)
    return Response(r.content, mimetype="image/png",
                    headers={"Cache-Control": "no-store"})


@app.route("/api/admin/whatsapp/logout", methods=["POST"])
@auth.admin_required
def admin_whatsapp_logout():
    try:
        r = cffi_requests.post(_bridge_url() + "/logout",
                               headers=_bridge_headers(), timeout=20)
        try:
            body = r.json()
        except Exception:
            body = {"ok": r.status_code == 200}
        return jsonify(body), r.status_code
    except Exception as e:
        return jsonify({"ok": False, "error": f"bridge unreachable: {e}"}), 502


if __name__ == "__main__":
    # Dev only — production uses gunicorn (see wsgi.py / Docker)
    print(f"Stockly ({config.ENV}) -> http://{config.HOST}:{config.PORT}")
    if _created_default_admin:
        print(f"Default admin → {auth.DEFAULT_ADMIN_USER} / {auth.DEFAULT_ADMIN_PASS} (change on first login)")
    app.run(host=config.HOST, port=config.PORT, debug=not config.IS_PROD, threaded=True)
