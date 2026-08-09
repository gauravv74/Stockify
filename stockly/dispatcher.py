#!/usr/bin/env python3
"""Turning an authorized search request into queued work.

The API calls :func:`start_search` and returns immediately. All expansion —
distance ordering, building one task per (platform × product × pincode) —
happens in the ``plan_job`` control task, so a 5,000-check search costs the
request the same as a 1-check one.

Everything reaching this module is already authorized: the route has intersected
the request with the caller's platform/city/pincode grants. Workers never
re-derive permissions from user input.
"""

from __future__ import annotations

import logging
import math

import config
import jobs
from stockly import geo, obs

log = logging.getLogger("stockly.dispatcher")


class LimitExceeded(Exception):
    """A safety limit was hit. Carries the payload the API should return."""

    def __init__(self, code, message, status=429, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra

    def to_response(self):
        return {"error": self.code, "message": self.message, **self.extra}, self.status


def check_id_for(index, platform, product, pincode) -> str:
    """Stable logical identity for one check.

    Used as the idempotency key, so a redelivered or duplicated task collapses
    onto the same ``search_events`` row instead of rendering twice. ``index``
    is included because it fixes the row's position in the results table.
    """
    return f"{index}:{platform}:{pincode}:{product}"


def enforce_limits(user_id, total_checks, is_admin=False):
    """Raise :class:`LimitExceeded` when a request would overload the system.

    Admins bypass the per-user caps but not the global one — the global cap
    protects the workers, and nobody should be able to bury them.
    """
    if total_checks <= 0:
        raise LimitExceeded(
            "empty_search", "Nothing to check for that selection.", status=400)

    if total_checks > config.MAX_SEARCH_CHECKS:
        raise LimitExceeded(
            "search_too_large",
            f"That search is {total_checks:,} checks; the limit is "
            f"{config.MAX_SEARCH_CHECKS:,}. Narrow the cities, products or platforms.",
            status=409, total=total_checks, limit=config.MAX_SEARCH_CHECKS)

    if jobs.total_queued_checks() + total_checks > config.MAX_TOTAL_QUEUED_CHECKS:
        raise LimitExceeded(
            "system_busy",
            "The system is at capacity right now. Please try again shortly.",
            status=429)

    if is_admin:
        return

    if jobs.count_active_jobs(user_id) >= config.MAX_ACTIVE_JOBS_PER_USER:
        raise LimitExceeded(
            "too_many_active_jobs",
            f"You already have {config.MAX_ACTIVE_JOBS_PER_USER} searches running. "
            "Stop one or wait for it to finish.",
            status=409)

    outstanding = jobs.queued_checks_for_user(user_id)
    if outstanding + total_checks > config.MAX_QUEUED_CHECKS_PER_USER:
        raise LimitExceeded(
            "too_many_queued_checks",
            f"You already have {outstanding:,} checks queued; the limit is "
            f"{config.MAX_QUEUED_CHECKS_PER_USER:,}. Wait for them to finish.",
            status=429, queued=outstanding)


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p = math.pi / 180.0
    dlat = (lat2 - lat1) * p
    dlon = (lon2 - lon1) * p
    a = (math.sin(dlat / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin(dlon / 2) ** 2)
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def order_by_distance(pincodes, ref_lat, ref_lon):
    """Nearest-first ordering using only cached geocodes.

    Uncached pincodes keep their relative order and go last rather than
    blocking the run on a geocode round-trip; they sort correctly on the next
    search once the cache is warm.
    """
    cached = geo.preloaded(pincodes)

    def sort_key(item):
        i, pin = item
        entry = cached.get(str(pin))
        if entry:
            try:
                return (0, _haversine_km(ref_lat, ref_lon,
                                         float(entry["lat"]), float(entry["lon"])), i)
            except (TypeError, ValueError):
                pass
        return (1, 0.0, i)

    return [pin for _, pin in sorted(enumerate(pincodes), key=sort_key)]


def start_search(user, meta, pincodes, products, platforms,
                 ref_lat=None, ref_lon=None, order_by=None):
    """Create the job row and hand expansion to the queue.

    Returns ``(job_id, total, queued)``. ``queued`` is False when the broker was
    unavailable and the caller should fall back to inline execution.
    """
    user_id = user.get("id")
    is_admin = user.get("role") == "admin"
    total = len(pincodes) * len(products) * len(platforms)

    enforce_limits(user_id, total, is_admin=is_admin)

    plan = {
        "pincodes": list(pincodes),
        "products": list(products),
        "platforms": list(platforms),
        "ref_lat": ref_lat,
        "ref_lon": ref_lon,
        "order_by": order_by,
        # Admins are never charged; resolved once here so workers don't have to
        # re-read the user's role on every check.
        "charge": bool(user_id) and not is_admin,
    }
    job_id = jobs.create_job(user_id, meta, total, status=jobs.QUEUED, plan=plan)

    from stockly import broker
    if not broker.is_live():
        log.warning("broker unavailable — caller must run inline",
                    extra={"job_id": job_id})
        return job_id, total, False

    from stockly import tasks
    tasks.plan_job.send(job_id)
    obs.metrics.incr("jobs_started")
    log.info("job_queued", extra={"event": "job_queued", "job_id": job_id,
                                  "total": total, "user_id": user_id})
    return job_id, total, True


def iter_checks(plan):
    """Yield ``(index, platform, product, pincode)`` in execution order.

    Pincode-major so that all platforms for one location resolve together —
    results then fill in location by location, which is what the UI groups by.
    """
    pincodes = plan.get("pincodes") or []
    products = plan.get("products") or []
    platforms = plan.get("platforms") or []

    if plan.get("order_by") == "distance" and plan.get("ref_lat") is not None:
        pincodes = order_by_distance(pincodes, plan["ref_lat"], plan["ref_lon"])

    index = 0
    for pincode in pincodes:
        for product in products:
            for platform in platforms:
                index += 1
                yield index, platform, product, pincode


def enqueue_checks(job_id):
    """Fan a planned job out into one task per check. Runs inside ``plan_job``."""
    job = jobs.get_job(job_id)
    if not job:
        log.warning("plan_missing", extra={"job_id": job_id})
        return 0

    plan = jobs.get_plan(job_id)
    if not plan:
        # Nothing will ever run this job, so terminate it rather than leave the
        # client polling a row that can never progress.
        log.error("plan_missing", extra={"job_id": job_id})
        jobs.set_status(job_id, jobs.ERROR, detail="Search plan was lost.")
        return 0

    if job["status"] in jobs.TERMINAL_STATUSES:
        return 0
    if job["cancel"]:
        # Cancelled between accepting the request and fanning out: no task will
        # ever run, so nothing else would move this job out of `canceling`.
        jobs.set_status(job_id, jobs.CANCELED)
        log.info("fanout_skipped_canceled", extra={"job_id": job_id})
        return 0

    from stockly import tasks

    user_id = job.get("user_id")
    charge = bool(plan.get("charge"))
    sent = 0
    for index, platform, product, pincode in iter_checks(plan):
        tasks.actor_for(platform).send(
            job_id,
            check_id_for(index, platform, product, pincode),
            index, platform, product, pincode, user_id, charge,
        )
        sent += 1
        # A very large fan-out can outlive a cancel issued while we publish.
        # The tasks already sent will see the flag and finalize the job.
        if sent % 250 == 0 and jobs.is_canceled(job_id):
            log.info("fanout_canceled", extra={"job_id": job_id, "sent": sent})
            break

    jobs.heartbeat(job_id)
    obs.metrics.incr("checks_enqueued", value=sent)
    log.info("job_fanned_out", extra={"event": "job_fanned_out",
                                      "job_id": job_id, "tasks": sent})
    return sent
