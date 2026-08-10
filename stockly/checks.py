#!/usr/bin/env python3
"""The single place a retailer availability check is executed.

Both searches and watches funnel through :func:`execute_platform_check`, so the
two can never drift in matching, status semantics or error handling — before
this module they were two near-identical dispatch chains (``app._check_one``
and ``worker._check_platform``) that had already diverged on how Blinkit
reported HTTP failures.

Status contract (unchanged, and load-bearing):

    available | out_of_stock | not_found | not_serviceable   -> business results
    error, error_<code>, geocode_failed                       -> infrastructure

An infrastructure failure must never be reported as ``out_of_stock``: watches
alert on transitions, so conflating "we couldn't check" with "it's gone" would
fire false alerts on every recovery.
"""

from __future__ import annotations

import logging
import time

import blinkit_check as bk
from stockly import obs, offers

log = logging.getLogger("stockly.checks")

# Results that represent a real answer from the retailer. Everything else is an
# infrastructure outcome and is never billable, never alertable, and (for the
# retryable subset) safe to run again.
BUSINESS_STATUSES = frozenset(
    ("available", "out_of_stock", "not_found", "not_serviceable"))


def is_infrastructure_status(status) -> bool:
    """True when ``status`` means "we failed to get an answer".

    Prefix-aware on purpose: Blinkit reports ``error_403``/``error_429`` while
    the browser platforms report a bare ``error``. Matching only the exact
    string (as ``watches.TRANSIENT`` historically did) let coded errors leak
    through as if they were real state changes.
    """
    if not status:
        return True
    return status == "geocode_failed" or str(status).startswith("error")


class TransientCheckError(Exception):
    """Raised for failures worth retrying (network, WAF, browser crash)."""


# Substrings that mark a failure as environmental rather than a real answer.
_TRANSIENT_MARKERS = (
    "timeout", "timed out", "econnreset", "connection reset", "connection aborted",
    "connection refused", "temporarily unavailable", "read timeout", "eof occurred",
    "429", "500", "502", "503", "504", "waf", "challenge", "captcha",
    "browser", "chromium", "target closed", "session closed", "page crashed",
    "proxy", "tunnel", "ssl", "handshake",
)


def classify_exception(exc) -> bool:
    """True when ``exc`` looks transient and the check is worth retrying."""
    if isinstance(exc, TransientCheckError):
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def _blinkit_row(session, product, lat, lon):
    serviceable, products, code = bk.blinkit_search(session, product, lat, lon)
    if serviceable is False:
        return {"status": "not_serviceable"}
    if serviceable is None:
        # Coded so operators can see *why* it failed; still infrastructure.
        return {"status": f"error_{code}", "detail": f"http {code}"}
    match = bk.best_match(product, products)
    if not match:
        return {"status": "not_found"}
    return {
        "status": "available" if match["available"] else "out_of_stock",
        "available": "yes" if match["available"] else "no",
        "name": match["name"], "variant": match["variant"], "brand": match["brand"],
        "price": match["price"], "mrp": match["mrp"],
        "inventory": match["inventory"], "eta": match["eta"],
        "merchant_id": match["merchant_id"],
    }


def _run_platform_check(platform, product, pincode, lat=None, lon=None, session=None):
    """Dispatch to one platform client and return its normalised row.

    Imports are deferred so an API process (which never scrapes) does not pay
    the cost of importing Playwright, and so a broken scraper module cannot
    prevent the web tier from starting.
    """
    if platform == "instamart":
        import swiggy_check as sw
        return sw.match_row(product, sw.client.check(float(lat), float(lon), product))
    if platform == "zepto":
        import zepto_check as zp
        return zp.match_row(product, zp.client.check(float(lat), float(lon), product))
    if platform == "bigbasket":
        import bigbasket_check as bb
        return bb.match_row(product, bb.client.check(str(lat), str(lon), product, pincode))
    if platform == "flipkart":
        import flipkart_check as fk
        return fk.match_row(product, fk.client.check(float(lat), float(lon), product))
    if platform == "jiomart":
        import jiomart_check as jm
        return jm.match_row(product, jm.client.check(float(lat), float(lon), product, pincode))
    if platform == "apple":
        import apple_check as ap
        return ap.match_row(product, ap.client.check(float(lat), float(lon), product, pincode))
    if platform == "croma":
        import croma_check as cr
        return cr.match_row(product, cr.client.check(float(lat), float(lon), product, pincode))

    if session is None:
        from curl_cffi import requests as cffi_requests
        session = cffi_requests.Session()
    row = _blinkit_row(session, product, lat, lon)
    # Blinkit's public API is rate-limited by cadence; keep the historical pause.
    time.sleep(bk.REQUEST_PAUSE)
    return row


def execute_platform_check(platform, product, pincode, lat=None, lon=None,
                           place=None, session=None, raise_transient=False):
    """Run exactly one (platform × product × pincode) check.

    Always returns a normalised row. When ``raise_transient`` is set, an
    infrastructure failure raises :class:`TransientCheckError` instead so the
    task layer can decide whether to retry — business results never raise.
    """
    started = time.monotonic()
    row = {}
    try:
        row = _run_platform_check(platform, product, pincode, lat, lon, session) or {}
        status = row.get("status") or "error"
    except Exception as exc:  # noqa: BLE001 - deliberately broad; classified below
        transient = classify_exception(exc)
        log.warning(
            "check failed", exc_info=not transient,
            extra={"platform": platform, "pincode": pincode, "product": product,
                   "transient": transient, "error": str(exc)[:200]},
        )
        if transient and raise_transient:
            raise TransientCheckError(str(exc)[:200]) from exc
        row = {"status": "error", "detail": str(exc)[:200]}
        status = "error"

    duration_ms = (time.monotonic() - started) * 1000.0
    obs.metrics.observe("check_duration_ms", duration_ms, platform=platform)
    obs.metrics.incr("checks_total", platform=platform, status=status)

    if is_infrastructure_status(status):
        obs.metrics.incr("check_failures_total", platform=platform)
        if raise_transient and status != "geocode_failed":
            raise TransientCheckError(row.get("detail") or status)

    log.info(
        "check_completed",
        extra={"event": "check_completed", "platform": platform, "pincode": pincode,
               "product": product, "status": status,
               "duration_ms": round(duration_ms, 1)},
    )
    return _with_offer(row)


def _with_offer(row):
    """Fill in the shelf discount when the platform found no card offer.

    Done here rather than in each scraper because MRP and price are already
    normalised onto the row by this point, so one implementation covers all
    eight platforms and stays consistent between them. A card offer a scraper
    did find is never overwritten.
    """
    if not isinstance(row, dict) or row.get("best_offer"):
        return row
    discount = offers.from_price(row.get("price"), row.get("mrp"))
    if discount:
        row["best_offer"] = discount
    return row


def blank_row(index, pincode, place, lat, lon, product, platform):
    """The result envelope the API and clients expect. Shape is frozen."""
    return {
        "type": "result", "index": index, "pincode": pincode, "platform": platform,
        "location": place or "", "location_full": "", "lat": lat, "lon": lon,
        "product": product,
        "status": "", "available": "", "name": "", "variant": "", "brand": "",
        "price": "", "mrp": "", "inventory": "", "eta": "", "merchant_id": "",
        # None means "no offer found", which is a real answer — see stockly/offers.py.
        "best_offer": None,
    }
