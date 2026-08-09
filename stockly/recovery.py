#!/usr/bin/env python3
"""Stale job recovery.

A job is a database row plus tasks in flight. If the workers holding those
tasks die — OOM, container restart, deploy — nothing else notices: the row
stays ``running`` and the browser polls a corpse forever. That is the exact
failure the old daemon-thread model had whenever gunicorn recycled a worker.

Any active job whose heartbeat has gone quiet for longer than
``JOB_STALE_TIMEOUT_SEC`` is moved to a terminal state, so every client
eventually stops polling.
"""

from __future__ import annotations

import logging
import signal
import time

import config
import jobs
from stockly import obs

log = logging.getLogger("stockly.recovery")

_stop = False


def _handle_signal(signum, _frame):
    global _stop
    _stop = True
    log.info("shutdown_signal", extra={"signal": signum})


def sweep(timeout_sec=None):
    """Fail every job that has stopped making progress. Returns how many."""
    timeout_sec = timeout_sec or config.JOB_STALE_TIMEOUT_SEC
    stale = jobs.stale_jobs(timeout_sec)
    for job in stale:
        completed = int(job.get("completed_checks") or 0)
        total = int(job.get("total") or 0)

        # It may simply have finished while the last worker was shutting down.
        if total and completed >= total:
            jobs.set_status(job["id"], jobs.DONE)
            log.info("stale_job_completed", extra={"job_id": job["id"]})
            continue

        status = jobs.CANCELED if job.get("status") == jobs.CANCELING else jobs.ERROR
        detail = (f"Stopped responding after {completed}/{total} checks. "
                  "The worker handling it was interrupted.")
        jobs.set_status(job["id"], status, detail=detail)
        obs.metrics.incr("jobs_reaped", status=status)
        log.warning("stale_job_reaped",
                    extra={"event": "stale_job_reaped", "job_id": job["id"],
                           "completed": completed, "total": total, "status": status})
    return len(stale)


def main():
    obs.setup("stockly.recovery")
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    jobs.init_db()
    log.info("recovery_started",
             extra={"tick_sec": config.RECOVERY_TICK_SEC,
                    "stale_timeout_sec": config.JOB_STALE_TIMEOUT_SEC})

    while not _stop:
        try:
            reaped = sweep()
            if reaped:
                log.info("sweep_done", extra={"reaped": reaped})
        except Exception:  # noqa: BLE001 - a bad sweep must not kill the loop
            log.exception("sweep_failed")

        slept = 0
        while slept < config.RECOVERY_TICK_SEC and not _stop:
            time.sleep(min(2, config.RECOVERY_TICK_SEC - slept))
            slept += 2

    log.info("recovery_stopped")


if __name__ == "__main__":
    main()
