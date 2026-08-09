#!/usr/bin/env python3
"""Manual end-to-end smoke test against a real Redis + real dramatiq workers.

Not part of the pytest suite (it needs live infrastructure). Run it after a
deploy to prove the whole path works:

    redis-server --port 6399 --daemonize yes
    STOCKLY_REDIS_URL=redis://127.0.0.1:6399/0 python tests/smoke_queue.py

Retailer calls are stubbed, so this exercises the plumbing — enqueue, fan-out,
execution, idempotency, progress, finalisation — not the scrapers.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

os.environ.setdefault("STOCKLY_DATA_DIR", tempfile.mkdtemp(prefix="stockly_smoke_"))
os.environ.setdefault("STOCKLY_SECRET_KEY", "smoke-test")
os.environ["STOCKLY_QUEUE_ENABLED"] = "1"
os.environ.setdefault("STOCKLY_REDIS_URL", "redis://127.0.0.1:6399/0")
os.environ["STOCKLY_LOG_JSON"] = "0"

import auth  # noqa: E402
import config  # noqa: E402
import jobs  # noqa: E402
from stockly import broker, dispatcher, geo  # noqa: E402

PINCODES = ["411001", "411002", "411003", "411004"]
PLATFORMS = ["blinkit", "zepto", "croma"]


def fail(msg):
    print(f"  FAIL: {msg}")
    sys.exit(1)


def main():
    ok, detail = broker.ping()
    if not ok:
        fail(f"Redis not reachable at {config.REDIS_URL}: {detail}")
    print(f"Redis OK ({config.REDIS_URL})")

    auth.init_db()
    jobs.init_db()
    geo.init_db()
    for pin in PINCODES:
        geo.store(pin, {"lat": "18.5204", "lon": "73.8567", "place": f"Pune {pin}"})

    user, err = auth.create_user(
        "smokeuser", "password123",
        platforms={p: True for p in auth.ALL_PLATFORMS})
    if err:
        user = auth.find_user_by_username("smokeuser")
    auth.grant_tokens(user["id"], 100_000, actor="smoke")

    env = {**os.environ, "STOCKLY_WORKER": "1", "PYTHONPATH": str(REPO)}
    queues = f"{config.QUEUE_CONTROL},{config.QUEUE_HTTP}," \
             f"{config.QUEUE_BROWSER},{config.QUEUE_PROTECTED}"
    worker = subprocess.Popen(
        [sys.executable, "-m", "dramatiq", "tests.smoke_actors",
         "--queues", *queues.split(","), "--processes", "2", "--threads", "4"],
        cwd=str(REPO), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    print("Workers starting...")
    time.sleep(4)
    if worker.poll() is not None:
        print(worker.stdout.read())
        fail("workers exited immediately")

    try:
        expected = len(PINCODES) * len(PLATFORMS)
        job_id, total, queued = dispatcher.start_search(
            auth._public_user(user), {"total": expected},
            PINCODES, ["iphone 17"], PLATFORMS)
        print(f"Enqueued job {job_id}: total={total} queued={queued}")
        if not queued or total != expected:
            fail(f"expected {expected} checks, got total={total} queued={queued}")

        deadline = time.time() + 90
        job = None
        while time.time() < deadline:
            job = jobs.get_job(job_id)
            if job["status"] in jobs.TERMINAL_STATUSES:
                break
            time.sleep(0.5)

        events = [e for e in jobs.get_events(job_id) if e.get("type") == "result"]
        print(f"status={job['status']} completed={job['completed_checks']}/{total} "
              f"rows={len(events)}")

        if job["status"] != jobs.DONE:
            fail(f"job did not complete: {job['status']} ({job.get('detail')})")
        if len(events) != expected:
            fail(f"expected {expected} result rows, got {len(events)}")

        seqs = [e["seq"] for e in events]
        if seqs != sorted(seqs):
            fail("cursor is not monotonic")
        if len(set(seqs)) != len(seqs):
            fail("duplicate cursor values")

        indexes = sorted(e["index"] for e in events)
        if indexes != list(range(1, expected + 1)):
            fail(f"result indexes are not dense 1..{expected}")

        print("\nAll smoke assertions passed.")
    finally:
        worker.terminate()
        try:
            worker.wait(timeout=30)
            print(f"Workers shut down cleanly (exit {worker.returncode})")
        except subprocess.TimeoutExpired:
            worker.kill()
            fail("workers did not honour SIGTERM within 30s")


if __name__ == "__main__":
    main()
