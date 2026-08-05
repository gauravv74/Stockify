#!/usr/bin/env python3
"""Stockly stock watcher — standalone polling worker.

Runs as its own long-lived process (see docker-compose ``worker`` service). On
every tick it pulls the watches whose re-check interval has elapsed, checks
current availability on the target platform (Swiggy Instamart by default),
compares against the last known state, and fires a WhatsApp alert on any
meaningful change (e.g. a product coming back in stock).

Design notes
------------
* Runs in a *separate* process from gunicorn so the heavy Playwright/Chromium
  session used for Instamart lives in exactly one place and never gets forked
  across web workers.
* State + change detection live in the DB (watches.py), so restarts are safe:
  a crash mid-cycle just re-checks a few watches next tick, it never double-fires
  because an alert is only sent when the *stored* state actually changes.
* Transient failures (WAF challenge, geocode miss, network) never overwrite the
  last good state, so you don't get "out of stock" false alarms from a blip.
"""

from __future__ import annotations

import logging
import signal
import time

from curl_cffi import requests as cffi_requests

import blinkit_check as bk
import config
import watches
import whatsapp

logging.basicConfig(
    level=logging.INFO if config.IS_PROD else logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("stockly.worker")

_STOP = False

# Human-friendly labels + emoji per status for the WhatsApp message.
STATUS_LABEL = {
    "available": "🟢 IN STOCK",
    "out_of_stock": "🔴 OUT OF STOCK",
    "not_found": "⚪ NOT LISTED",
    "not_serviceable": "🚫 NOT SERVICEABLE",
}
PLATFORM_LABEL = {
    "instamart": "Swiggy Instamart", "blinkit": "Blinkit", "zepto": "Zepto",
    "bigbasket": "BigBasket", "flipkart": "Flipkart Minutes", "jiomart": "JioMart",
    "apple": "Apple", "croma": "Croma",
}


def _handle_stop(signum, _frame):
    global _STOP
    log.info("signal %s received -> shutting down after current cycle", signum)
    _STOP = True


# ---------------------------------------------------------------------------
# Platform dispatch (mirrors app._check_one, imported lazily to keep startup
# light and avoid pulling every scraper unless it's actually used).
# ---------------------------------------------------------------------------
def _check_platform(platform, session, q, lat, lon, pin):
    if platform == "instamart":
        import swiggy_check as sw
        return sw.match_row(q, sw.client.check(float(lat), float(lon), q))
    if platform == "zepto":
        import zepto_check as zp
        return zp.match_row(q, zp.client.check(float(lat), float(lon), q))
    if platform == "bigbasket":
        import bigbasket_check as bb
        return bb.match_row(q, bb.client.check(str(lat), str(lon), q, pin))
    if platform == "flipkart":
        import flipkart_check as fk
        return fk.match_row(q, fk.client.check(float(lat), float(lon), q))
    if platform == "jiomart":
        import jiomart_check as jm
        return jm.match_row(q, jm.client.check(float(lat), float(lon), q, pin))
    if platform == "apple":
        import apple_check as ap
        return ap.match_row(q, ap.client.check(float(lat), float(lon), q, pin))
    if platform == "croma":
        import croma_check as cr
        return cr.match_row(q, cr.client.check(float(lat), float(lon), q, pin))
    # default: blinkit
    serviceable, prods, code = bk.blinkit_search(session, q, lat, lon)
    if serviceable is False:
        return {"status": "not_serviceable"}
    if serviceable is None:
        return {"status": "error", "detail": f"http {code}"}
    m = bk.best_match(q, prods)
    if not m:
        return {"status": "not_found"}
    return {
        "status": "available" if m["available"] else "out_of_stock",
        "name": m["name"], "variant": m["variant"], "brand": m["brand"],
        "price": m["price"], "mrp": m["mrp"], "eta": m["eta"],
    }


def _fmt_price(row):
    price, mrp = row.get("price"), row.get("mrp")
    if price in (None, "") and mrp in (None, ""):
        return ""
    out = f"₹{price}" if price not in (None, "") else ""
    if mrp not in (None, "") and mrp != price:
        out += f" (MRP ₹{mrp})"
    if row.get("eta"):
        out += f" · ETA {row['eta']}"
    return out.strip(" ·")


def _build_message(watch, status, row, first_seen):
    plat = PLATFORM_LABEL.get(watch["platform"], watch["platform"].title())
    label = STATUS_LABEL.get(status, status.upper())
    name = row.get("name") or watch["product"]
    place = watch.get("place") or watch["pincode"]
    lines = [f"{label} — {plat}", name]
    price = _fmt_price(row)
    if price:
        lines.append(price)
    lines.append(f"📍 {watch['pincode']} · {place}"[:120])
    # Exact checked coordinate -> Google Maps. Availability is per dark-store, so
    # this is the precise point the stock belongs to (not the whole pincode).
    lat, lon = watch.get("lat"), watch.get("lon")
    if lat and lon:
        lines.append(f"🗺️ https://www.google.com/maps/search/?api=1&query={lat},{lon}")
    if first_seen:
        lines.append("(first check for this watch)")
    return "\n".join(lines)


def _should_notify(prev_status, prev_available, new_status, new_available):
    """Decide whether this transition warrants an alert.

    Returns True on a genuine change. First observation (prev_status is None) is
    reported so you get an immediate baseline, then only real changes after.
    """
    first_seen = prev_status is None
    changed = (new_status != prev_status) or (bool(new_available) != bool(prev_available))
    if not changed:
        return False, False, first_seen
    if config.WATCH_NOTIFY_ON == "availability":
        # Only when it (re)enters stock.
        notify = bool(new_available) and not bool(prev_available)
    else:  # "change" — any meaningful status flip
        notify = True
    return notify, changed, first_seen


def _process_watch(watch, session, cache):
    wid = watch["id"]
    platform = watch["platform"]
    q = watch["product"]
    pin = watch["pincode"]

    # 1) geocode (cached across watches + persisted per-watch)
    lat, lon, place = watch.get("lat"), watch.get("lon"), watch.get("place")
    if not lat or not lon:
        geo = bk.geocode_pincode(pin, cache, session)
        lat, lon, place = geo.get("lat"), geo.get("lon"), geo.get("place")
        if lat and lon:
            watches.save_geo(wid, lat, lon, place)
            watch["place"] = place
    if not lat or not lon:
        log.warning("watch %s: geocode failed for %s", wid, pin)
        watches.record_result(wid, "geocode_failed", False, None, False, False)
        return

    # 2) check the platform
    try:
        row = _check_platform(platform, session, q, lat, lon, pin)
    except Exception as e:
        log.exception("watch %s: check crashed", wid)
        watches.record_result(wid, "error", False, {"detail": str(e)[:200]}, False, False)
        return

    status = row.get("status") or "error"
    available = status == "available"

    # 3) transient statuses: bump error streak, never alert / clobber state
    if status in watches.TRANSIENT:
        watches.record_result(wid, status, False, None, False, False)
        log.info("watch %s (%s/%s @%s): transient=%s", wid, platform, q, pin, status)
        return

    # 4) change detection + notification
    prev_status = watch.get("last_status")
    prev_available = watch.get("last_available")
    notify, changed, first_seen = _should_notify(
        prev_status, prev_available, status, available)

    notified = False
    if notify:
        msg = _build_message(watch, status, row, first_seen)
        to = watch.get("notify_to") or None
        ok, detail = whatsapp.send(msg, to=to)
        notified = ok
        if not ok:
            log.warning("watch %s: alert NOT delivered: %s", wid, detail)

    watches.record_result(wid, status, available, row, changed, notified)
    log.info("watch %s (%s/%s @%s): %s->%s changed=%s notified=%s",
             wid, platform, q, pin, prev_status, status, changed, notified)


def run_cycle(session, cache):
    due = watches.due_watches(config.WATCH_INTERVAL_MIN, config.WATCH_BATCH)
    if not due:
        return 0
    log.info("cycle: %d watch(es) due", len(due))
    for w in due:
        if _STOP:
            break
        _process_watch(w, session, cache)
        time.sleep(config.WATCH_PAUSE_SEC)
    return len(due)


def main():
    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    watches.init_db()
    log.info("worker up | interval=%dm tick=%ds batch=%d notify_on=%s provider=%s configured=%s",
             config.WATCH_INTERVAL_MIN, config.WATCH_TICK_SEC, config.WATCH_BATCH,
             config.WATCH_NOTIFY_ON, config.WHATSAPP_PROVIDER, whatsapp.is_configured())
    if not whatsapp.is_configured():
        log.warning("WhatsApp not fully configured — alerts will be logged/failed. "
                    "Set STOCKLY_WHATSAPP_* env vars.")

    cache = bk.load_cache()
    session = cffi_requests.Session()

    while not _STOP:
        try:
            run_cycle(session, cache)
        except Exception:
            log.exception("cycle failed — continuing")
        # Sleep in small slices so SIGTERM is honoured promptly.
        slept = 0
        while slept < config.WATCH_TICK_SEC and not _STOP:
            time.sleep(min(2, config.WATCH_TICK_SEC - slept))
            slept += 2
    log.info("worker stopped")


if __name__ == "__main__":
    main()
