"""Gunicorn tuning for an API-only web tier.

Scraping now runs in dedicated worker containers, so this process does nothing
but authenticate, validate, enqueue and read from the database. That changes
the tuning completely: requests are short and I/O-bound, and the dominant
traffic is result polling.
"""

import multiprocessing
import os

bind = f"0.0.0.0:{os.environ.get('STOCKLY_PORT', '5001')}"

# Sized to the host rather than a fixed number, and still overridable.
workers = int(os.environ.get(
    "STOCKLY_WORKERS", max(2, min(8, (multiprocessing.cpu_count() * 2) + 1))))
threads = int(os.environ.get("STOCKLY_THREADS", "8"))
worker_class = "gthread"

# No request waits on a retailer any more; anything near a minute is a bug.
timeout = int(os.environ.get("STOCKLY_TIMEOUT", "60"))
graceful_timeout = 30
keepalive = 5

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("STOCKLY_LOG_LEVEL", "info")
capture_output = True

# Recycling every 500 requests was far too aggressive: with ~50 clients polling
# every couple of seconds a worker churned through that in well under a minute,
# and each recycle used to kill the in-process searches it was running. Nothing
# long-lived lives here now, so recycle rarely and only as leak insurance.
max_requests = int(os.environ.get("STOCKLY_MAX_REQUESTS", "10000"))
max_requests_jitter = int(os.environ.get("STOCKLY_MAX_REQUESTS_JITTER", "1000"))
