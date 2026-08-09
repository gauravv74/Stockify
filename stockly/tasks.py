#!/usr/bin/env python3
"""Dramatiq actors — the only place scraping executes.

Two kinds of task:

``plan_job``
    Control task. Expands a job's stored plan into one check task per
    (platform × product × pincode). Keeping this off the request path is what
    makes ``POST /api/check/start`` O(1) even for a 5,000-check search.

``check_http`` / ``check_browser`` / ``check_protected``
    One retailer check each. Three actors rather than one because Dramatiq
    binds a queue per actor, and separating them by cost profile is what stops
    a slow browser platform from starving cheap HTTP ones.

Token charging under parallelism
--------------------------------
Charging happens per billable result, exactly as before. With several checks
in flight a job can overshoot the wallet by at most the in-flight count before
it notices it is empty. ``auth.consume_tokens`` is atomic and clamps at zero,
so a user is never overcharged and the balance never goes negative — only the
stop point is approximate.
"""

from __future__ import annotations

import logging
import os
import sys

import dramatiq
from dramatiq.middleware import CurrentMessage
from dramatiq.middleware.time_limit import TimeLimitExceeded

import auth
import config
import jobs
from stockly import checks, geo, obs
from stockly.broker import get_broker

log = logging.getLogger("stockly.tasks")


def _is_worker_process() -> bool:
    """True when this module was loaded by the ``dramatiq`` CLI."""
    if os.environ.get("STOCKLY_WORKER") == "1":
        return True
    return os.path.basename(sys.argv[0] or "").startswith("dramatiq")


# Bind the broker before any actor is declared. A worker with no real broker is
# worse than a dead one — it would idle silently — so let it fail loudly.
obs.setup("stockly.worker" if _is_worker_process() else "stockly.tasks")
get_broker(strict=_is_worker_process())
jobs.init_db()
geo.init_db()


def _attempt() -> int:
    """How many times this message has already been retried."""
    message = CurrentMessage.get_current_message()
    if not message:
        return 0
    return int(message.options.get("retries", 0) or 0)


def _emit_notice(job_id, kind, **fields):
    """Notices are deduped by a deterministic check_id so parallel workers
    can't emit the same banner several times."""
    jobs.add_event(job_id, {"type": "notice", "kind": kind, **fields},
                   check_id=f"notice:{kind}")


def _charge_tokens(row, user_id, platform, pincode, product):
    """Charge for a billable result. Returns True when the wallet ran dry."""
    cost = config.TOKEN_COST.get(row.get("status"), 0)
    if not cost:
        return False
    consumed, balance = auth.consume_tokens(
        user_id, cost, reason="search",
        meta=f"{platform}:{pincode}:{product}:{row.get('status')}")
    row["token_cost"] = consumed
    row["balance"] = balance
    return consumed < cost or balance <= 0


def _run_check(job_id, check_id, index, platform, product, pincode,
               user_id=None, charge=False):
    """Execute one check and persist exactly one result row."""
    with obs.scope(job_id=job_id, check_id=check_id, platform=platform,
                   pincode=pincode, product=product, user_id=user_id):
        job = jobs.get_job(job_id)
        if not job:
            log.warning("orphan_task_dropped")
            return
        # Cooperative cancellation: queued work simply never runs.
        if job["cancel"] or job["status"] in jobs.TERMINAL_STATUSES:
            obs.metrics.incr("checks_skipped", reason="canceled")
            jobs.finalize_if_complete(job_id)
            return

        jobs.mark_running(job_id)
        row = checks.blank_row(index, pincode, "", None, None, product, platform)

        location = geo.resolve(pincode)
        if not location:
            row["status"] = "geocode_failed"
            jobs.record_result(job_id, check_id, row, failed=True)
            jobs.finalize_if_complete(job_id)
            return

        row["lat"] = location["lat"]
        row["lon"] = location["lon"]
        row["location"] = location.get("place") or ""

        attempt = _attempt()
        can_retry = attempt < config.CHECK_MAX_RETRIES
        try:
            row.update(checks.execute_platform_check(
                platform, product, pincode,
                lat=location["lat"], lon=location["lon"],
                raise_transient=can_retry,
            ))
        except checks.TransientCheckError:
            # Let Dramatiq redeliver with backoff. No row is written, so the
            # retry re-uses the same check_id and stays idempotent.
            obs.metrics.incr("checks_retried", platform=platform)
            log.warning("check_retry_scheduled", extra={"attempt": attempt + 1})
            raise
        except TimeLimitExceeded:
            # Interrupt subclasses BaseException, so it bypasses the handler in
            # execute_platform_check. On the final attempt nothing else would
            # ever write a row for this check and the job would hang at
            # completed < total until the reaper failed it, so record the
            # timeout as the infrastructure error it is.
            obs.metrics.incr("checks_timed_out", platform=platform)
            log.warning("check_timed_out", extra={"attempt": attempt + 1})
            if can_retry:
                raise
            row["status"] = "error"
            row["detail"] = f"Timed out after {config.platform_timeout(platform):.0f}s"
            jobs.record_result(job_id, check_id, row, failed=True)
            jobs.finalize_if_complete(job_id)
            return

        exhausted = False
        if charge and user_id:
            exhausted = _charge_tokens(row, user_id, platform, pincode, product)

        failed = checks.is_infrastructure_status(row.get("status"))
        jobs.record_result(job_id, check_id, row, failed=failed)

        if exhausted:
            _emit_notice(job_id, "tokens_exhausted", balance=row.get("balance", 0))
            jobs.set_status(job_id, jobs.EXHAUSTED)
            return

        jobs.finalize_if_complete(job_id)


# ── Actors ─────────────────────────────────────────────────────────────────
# Retry policy: only transient failures raise, so Dramatiq's retry is never
# triggered by a legitimate business result. Backoff is exponential with the
# jitter Dramatiq applies by default.
_RETRY_KW = dict(
    max_retries=config.CHECK_MAX_RETRIES,
    min_backoff=int(config.CHECK_RETRY_BASE_SEC * 1000),
    max_backoff=int(config.CHECK_RETRY_BASE_SEC * 1000 * 8),
)


@dramatiq.actor(queue_name=config.QUEUE_HTTP,
                time_limit=config.task_time_limit_ms(config.QUEUE_HTTP),
                **_RETRY_KW)
def check_http(job_id, check_id, index, platform, product, pincode,
               user_id=None, charge=False):
    _run_check(job_id, check_id, index, platform, product, pincode, user_id, charge)


@dramatiq.actor(queue_name=config.QUEUE_BROWSER,
                time_limit=config.task_time_limit_ms(config.QUEUE_BROWSER),
                **_RETRY_KW)
def check_browser(job_id, check_id, index, platform, product, pincode,
                  user_id=None, charge=False):
    _run_check(job_id, check_id, index, platform, product, pincode, user_id, charge)


@dramatiq.actor(queue_name=config.QUEUE_PROTECTED,
                time_limit=config.task_time_limit_ms(config.QUEUE_PROTECTED),
                **_RETRY_KW)
def check_protected(job_id, check_id, index, platform, product, pincode,
                    user_id=None, charge=False):
    _run_check(job_id, check_id, index, platform, product, pincode, user_id, charge)


_ACTORS = {
    config.QUEUE_HTTP: check_http,
    config.QUEUE_BROWSER: check_browser,
    config.QUEUE_PROTECTED: check_protected,
}


def actor_for(platform):
    """The actor whose queue matches this platform's cost profile."""
    return _ACTORS[config.PLATFORM_QUEUE.get(platform, config.QUEUE_BROWSER)]


@dramatiq.actor(queue_name=config.QUEUE_CONTROL, max_retries=1, time_limit=120_000)
def plan_job(job_id):
    """Expand a job's plan into individual check tasks."""
    from stockly import dispatcher

    with obs.scope(job_id=job_id):
        dispatcher.enqueue_checks(job_id)
