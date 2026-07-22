import multiprocessing
import os

bind = f"0.0.0.0:{os.environ.get('STOCKLY_PORT', '5001')}"
workers = int(os.environ.get("STOCKLY_WORKERS", max(2, min(4, multiprocessing.cpu_count()))))
threads = int(os.environ.get("STOCKLY_THREADS", "4"))
worker_class = "gthread"
timeout = int(os.environ.get("STOCKLY_TIMEOUT", "300"))
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("STOCKLY_LOG_LEVEL", "info")
capture_output = True
# Playwright / curl sessions are process-local; avoid premature recycle mid-check
max_requests = int(os.environ.get("STOCKLY_MAX_REQUESTS", "500"))
max_requests_jitter = 50
