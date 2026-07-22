#!/usr/bin/env python3
"""Stockly — multi-platform product availability checker (production-ready)."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import timedelta

from flask import Flask, Response, jsonify, request, send_from_directory
from curl_cffi import requests as cffi_requests
from werkzeug.middleware.proxy_fix import ProxyFix

import auth
import blinkit_check as bk
import config

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


@app.route("/api/cities")
@auth.login_required
def api_cities():
    cities = [
        {"id": c["id"], "name": c["name"], "state": c.get("state", ""), "count": c["count"]}
        for c in load_cities()
    ]
    return jsonify({"cities": cities})


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


def _check_one(platform, session, q, lat, lon, pin):
    if platform == "instamart":
        return _check_instamart(q, lat, lon)
    if platform == "zepto":
        return _check_zepto(q, lat, lon)
    if platform == "bigbasket":
        return _check_bigbasket(q, lat, lon, pin)
    return _check_blinkit(session, q, lat, lon)


@app.route("/api/check", methods=["POST"])
@auth.login_required
def check():
    user = auth.current_user()
    allowed = auth.allowed_platforms(user)
    payload = request.get_json(force=True, silent=True) or {}
    pincodes, selected_cities = resolve_pincodes(payload)
    products = parse_products(payload.get("products", [])) or ["iphone 17"]
    platforms = resolve_platforms(payload.get("platform"), allowed)
    multi = len(platforms) > 1

    if not platforms:
        return jsonify({
            "error": "No platform access. Ask an admin to enable Blinkit / Instamart / Zepto / BigBasket for your account."
        }), 403

    def generate():
        if not pincodes:
            yield json.dumps({
                "type": "error",
                "message": "Select a city and/or enter at least one pincode.",
            }) + "\n"
            yield json.dumps({"type": "done", "total": 0}) + "\n"
            return

        cache = bk.load_cache()
        session = cffi_requests.Session()
        total = len(pincodes) * len(products) * len(platforms)
        yield json.dumps({
            "type": "meta",
            "total": total,
            "platform": "all" if multi else platforms[0],
            "platforms": platforms,
            "pincodes": len(pincodes),
            "products": products,
            "cities": selected_cities,
        }) + "\n"

        idx = 0
        for pin in pincodes:
            geo = bk.geocode_pincode(pin, cache, session)
            lat, lon, place = geo.get("lat"), geo.get("lon"), geo.get("place")

            if not lat:
                for q in products:
                    for plat in platforms:
                        idx += 1
                        row = _blank_row(idx, pin, "", None, None, q, plat)
                        row["status"] = "geocode_failed"
                        yield json.dumps(row) + "\n"
                continue

            for q in products:
                for plat in platforms:
                    idx += 1
                    row = _blank_row(idx, pin, place, lat, lon, q, plat)
                    try:
                        row.update(_check_one(plat, session, q, lat, lon, pin))
                    except Exception as e:
                        log.exception("check failed pin=%s plat=%s q=%s", pin, plat, q)
                        row["status"] = "error"
                        row["detail"] = str(e)[:200]
                    yield json.dumps(row) + "\n"

        yield json.dumps({"type": "done", "total": total}) + "\n"

    return Response(generate(), mimetype="application/x-ndjson")


if __name__ == "__main__":
    # Dev only — production uses gunicorn (see wsgi.py / Docker)
    print(f"Stockly ({config.ENV}) -> http://{config.HOST}:{config.PORT}")
    if _created_default_admin:
        print(f"Default admin → {auth.DEFAULT_ADMIN_USER} / {auth.DEFAULT_ADMIN_PASS} (change on first login)")
    app.run(host=config.HOST, port=config.PORT, debug=not config.IS_PROD, threaded=True)
