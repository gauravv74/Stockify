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
import random
import re
import signal
import time

from curl_cffi import requests as cffi_requests

import auth
import blinkit_check as bk
import config
import watches
import whatsapp
from stockly import checks, geo, obs

obs.setup("stockly.watcher")
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


# Monotonic timestamp of the last check per platform, used by _throttle() to
# space out calls to rate-limited APIs (see config.PLATFORM_MIN_INTERVAL_SEC).
_last_check_ts: dict[str, float] = {}


def _interruptible_sleep(seconds):
    """Sleep up to ``seconds`` but wake early on shutdown (2s slices)."""
    end = time.monotonic() + seconds
    while not _STOP:
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(2.0, remaining))


def _throttle(platform):
    """Enforce a minimum, jittered gap between consecutive checks of a
    rate-limited platform.

    Swiggy Instamart's search endpoint is behind a CloudFront JA4 rate-limiter
    that 403s bursts from one IP/TLS-fingerprint; spacing the calls out (with a
    little randomness so the cadence isn't clockwork) keeps us under it. No-op
    for platforms absent from ``config.PLATFORM_MIN_INTERVAL_SEC``.
    """
    min_gap = config.PLATFORM_MIN_INTERVAL_SEC.get(platform, 0.0)
    if min_gap <= 0:
        return
    jitter = config.PLATFORM_JITTER_SEC.get(platform, 0.0)
    target = min_gap + (random.uniform(0, jitter) if jitter > 0 else 0.0)
    last = _last_check_ts.get(platform)
    if last is not None:
        wait = target - (time.monotonic() - last)
        if wait > 0:
            log.debug("throttle %s: sleeping %.1fs before next check", platform, wait)
            _interruptible_sleep(wait)
    _last_check_ts[platform] = time.monotonic()


def _parse_price(value):
    """Coerce a platform price into a float for comparison.

    Handles the various shapes scrapers return: ``"₹499"``, ``"1,299.00"``,
    numbers, or None/"" (unknown -> None, never treated as a drop).
    """
    if value in (None, ""):
        return None
    try:
        cleaned = re.sub(r"[^\d.]", "", str(value))
        return float(cleaned) if cleaned else None
    except (ValueError, TypeError):
        return None


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


def _fmt_drop(prev_price, new_price):
    """Human 'was → now (−x%)' line for a price drop, or '' if not a drop."""
    if prev_price is None or new_price is None or new_price >= prev_price:
        return ""
    pct = (prev_price - new_price) / prev_price * 100 if prev_price else 0
    return f"💸 ₹{prev_price:g} → ₹{new_price:g} (−{pct:.0f}%)"


def _build_message(watch, status, row, first_seen, prev_price=None, new_price=None,
                   threshold=None):
    plat = PLATFORM_LABEL.get(watch["platform"], watch["platform"].title())
    label = STATUS_LABEL.get(status, status.upper())
    name = row.get("name") or watch["product"]
    place = watch.get("place") or watch["pincode"]
    lines = [f"{label} — {plat}", name]
    if threshold is not None:
        lines.append(f"🎯 at/below target ₹{threshold:g}")
    drop = _fmt_drop(prev_price, new_price)
    if drop:
        lines.append(drop)
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


def _should_notify(prev_status, prev_available, new_status, new_available,
                   prev_price=None, new_price=None, mode=None, threshold=None):
    """Decide whether this transition warrants an alert.

    Returns ``(notify, changed, first_seen)``. ``mode`` is the global alert
    mode (defaults to the runtime setting); ``threshold`` is the per-watch
    target price used by "threshold" mode.
    """
    first_seen = prev_status is None
    status_changed = (
        (new_status != prev_status) or (bool(new_available) != bool(prev_available))
    )
    if mode is None:
        mode = watches.get_notify_mode()

    if mode == "threshold":
        # Alert when the item is IN STOCK and its price is at/below the target.
        # Only fire on *entering* that state so it doesn't re-alert every cycle
        # while it stays below the target.
        met = (
            threshold is not None and new_price is not None
            and bool(new_available) and new_price <= threshold
        )
        prev_met = (
            threshold is not None and prev_price is not None
            and bool(prev_available) and prev_price <= threshold
        )
        price_changed = (
            prev_price is not None and new_price is not None and new_price != prev_price
        )
        notify = met and not prev_met
        return notify, (status_changed or price_changed or (met != prev_met)), first_seen

    if mode == "price_drop":
        # Alert when the item is IN STOCK and its price is strictly below the
        # previously recorded price. Missing prices on either side mean
        # "unknown", so we never alert on them.
        price_dropped = (
            prev_price is not None and new_price is not None and new_price < prev_price
        )
        price_changed = (
            prev_price is not None and new_price is not None and new_price != prev_price
        )
        notify = bool(new_available) and price_dropped
        return notify, (status_changed or price_changed), first_seen

    if not status_changed:
        return False, False, first_seen
    if mode == "availability":
        # Only when it (re)enters stock.
        notify = bool(new_available) and not bool(prev_available)
    else:  # "change" — any meaningful status flip
        notify = True
    return notify, status_changed, first_seen


def _process_watch(watch, session, cache):
    wid = watch["id"]
    platform = watch["platform"]
    q = watch["product"]
    pin = watch["pincode"]
    user_id = watch.get("user_id")

    # Token gate: non-admin users must have tokens for the watch to poll.
    if user_id and config.TOKEN_COST_WATCH_POLL > 0:
        user = auth.find_user_by_id(user_id)
        if user and user.get("role") != "admin":
            balance = auth.get_balance(user_id)
            if balance <= 0:
                log.info("watch %s: skipping — user %s has no tokens", wid, user_id)
                return
            consumed, _ = auth.consume_tokens(
                user_id, config.TOKEN_COST_WATCH_POLL,
                reason="watch_poll",
                meta=f"{platform}/{q}/{pin}")
            if consumed < config.TOKEN_COST_WATCH_POLL:
                log.info("watch %s: skipping — token deduction failed for user %s", wid, user_id)
                return

    # 1) geocode (shared, process-safe cache; persisted per-watch)
    lat, lon, place = watch.get("lat"), watch.get("lon"), watch.get("place")
    if not lat or not lon:
        located = geo.resolve(pin, session)
        if located:
            lat, lon, place = located["lat"], located["lon"], located.get("place")
            watches.save_geo(wid, lat, lon, place)
            watch["place"] = place
    if not lat or not lon:
        log.warning("watch %s: geocode failed for %s", wid, pin)
        watches.record_result(wid, "geocode_failed", False, None, False, False)
        return

    # 2) check the platform (throttled for rate-limited APIs like Instamart).
    # Shared with the search path so matching and status semantics can't drift.
    _throttle(platform)
    row = checks.execute_platform_check(
        platform, q, pin, lat=lat, lon=lon, session=session)

    status = row.get("status") or "error"
    available = status == "available"

    # 3) transient statuses: bump error streak, never alert / clobber state
    if watches.is_transient(status):
        watches.record_result(wid, status, False, None, False, False)
        log.info("watch %s (%s/%s @%s): transient=%s", wid, platform, q, pin, status)
        return

    # 4) change detection + notification
    prev_status = watch.get("last_status")
    prev_available = watch.get("last_available")
    prev_price = _parse_price(watch.get("last_price"))
    new_price = _parse_price(row.get("price"))
    threshold = _parse_price(watch.get("price_threshold"))
    mode = watches.get_notify_mode()
    notify, changed, first_seen = _should_notify(
        prev_status, prev_available, status, available, prev_price, new_price,
        mode=mode, threshold=threshold)

    notified = False
    if notify:
        msg = _build_message(watch, status, row, first_seen, prev_price, new_price,
                             threshold=threshold if mode == "threshold" else None)
        to = watch.get("notify_to") or None
        ok, detail = whatsapp.send(msg, to=to)
        notified = ok
        if not ok:
            log.warning("watch %s: alert NOT delivered: %s", wid, detail)

    watches.record_result(wid, status, available, row, changed, notified,
                          price=new_price)
    log.info("watch %s (%s/%s @%s): %s->%s changed=%s notified=%s",
             wid, platform, q, pin, prev_status, status, changed, notified)


def run_cycle(session, cache):
    due = watches.due_watches(watches.get_interval_min(), config.WATCH_BATCH)
    if not due:
        return 0
    log.info("cycle: %d watch(es) due", len(due))
    for w in due:
        if _STOP:
            break
        _process_watch(w, session, cache)
        # Base polite pause + a little jitter so the stream isn't perfectly
        # periodic (harder for cadence-based rate limiters to key on).
        jitter = random.uniform(0, config.WATCH_PAUSE_JITTER_SEC) \
            if config.WATCH_PAUSE_JITTER_SEC > 0 else 0.0
        _interruptible_sleep(config.WATCH_PAUSE_SEC + jitter)
    return len(due)


def main():
    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    auth.init_db()
    watches.init_db()
    geo.init_db()
    log.info("worker up | interval=%dm tick=%ds batch=%d notify_on=%s provider=%s configured=%s",
             watches.get_interval_min(), config.WATCH_TICK_SEC, config.WATCH_BATCH,
             watches.get_notify_mode(), config.WHATSAPP_PROVIDER, whatsapp.is_configured())
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
