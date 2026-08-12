#!/usr/bin/env python3
"""Stockly runtime configuration (env-driven)."""

from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no dependency). Existing env vars win, so Docker's
    env_file / real shell exports are never overridden."""
    if not path.exists():
        return
    try:
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            # strip an inline comment and surrounding quotes
            val = val.split(" #", 1)[0].strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except OSError:
        pass


_load_dotenv(HERE / ".env")
DATA_DIR = Path(os.environ.get("STOCKLY_DATA_DIR", HERE / "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

ENV = os.environ.get("STOCKLY_ENV", "development").strip().lower()
IS_PROD = ENV in ("production", "prod")

HOST = os.environ.get("STOCKLY_HOST", "0.0.0.0" if IS_PROD else "127.0.0.1")
PORT = int(os.environ.get("STOCKLY_PORT", "5001"))
WORKERS = int(os.environ.get("STOCKLY_WORKERS", "2"))

# Prefer env secret in production; fall back to file under data/
SECRET_KEY = os.environ.get("STOCKLY_SECRET_KEY", "").strip() or None
SECRET_FILE = DATA_DIR / ".stockly_secret"
DB_PATH = Path(os.environ.get("STOCKLY_DB", DATA_DIR / "stockly.db")).resolve()
LEGACY_USERS_JSON = HERE / "users.json"

SESSION_DAYS = int(os.environ.get("STOCKLY_SESSION_DAYS", "14"))
# Secure cookies: on in production unless explicitly disabled (e.g. local http docker)
_cookie_secure = os.environ.get("STOCKLY_COOKIE_SECURE")
if _cookie_secure is None:
    COOKIE_SECURE = IS_PROD
else:
    COOKIE_SECURE = _cookie_secure.strip().lower() in ("1", "true", "yes", "on")

COOKIE_SAMESITE = os.environ.get("STOCKLY_COOKIE_SAMESITE", "Lax")
TRUST_PROXY = os.environ.get("STOCKLY_TRUST_PROXY", "1" if IS_PROD else "0").lower() in (
    "1", "true", "yes", "on",
)

DEFAULT_ADMIN_USER = os.environ.get("STOCKLY_ADMIN_USER", "admin")
DEFAULT_ADMIN_PASS = os.environ.get("STOCKLY_ADMIN_PASS", "admin123")
CITIES_FILE = HERE / "cities.json"

# ---------------------------------------------------------------------------
# Outbound proxy (residential/mobile) for scrapers.
#
# Grocery platforms (Swiggy Instamart, Zepto) block requests from datacenter
# IPs (AWS/GCP/etc.), so from a cloud host their search returns 403 / login
# walls. Route scraper traffic through a residential/mobile proxy to look like
# a normal consumer connection. Leave unset to make direct connections.
#
#   STOCKLY_PROXY_SERVER   e.g. http://gate.provider.com:7000  (or socks5://...)
#   STOCKLY_PROXY_USERNAME
#   STOCKLY_PROXY_PASSWORD
# ---------------------------------------------------------------------------
PROXY_SERVER = os.environ.get("STOCKLY_PROXY_SERVER", "").strip()
PROXY_USERNAME = os.environ.get("STOCKLY_PROXY_USERNAME", "").strip()
PROXY_PASSWORD = os.environ.get("STOCKLY_PROXY_PASSWORD", "").strip()


def playwright_proxy():
    """Proxy dict for Playwright's browser/context, or None if unconfigured."""
    if not PROXY_SERVER:
        return None
    proxy = {"server": PROXY_SERVER}
    if PROXY_USERNAME:
        proxy["username"] = PROXY_USERNAME
    if PROXY_PASSWORD:
        proxy["password"] = PROXY_PASSWORD
    return proxy


def curl_proxies():
    """Proxy mapping for curl_cffi (`proxies=`), or None if unconfigured.

    Embeds credentials into the URL: scheme://user:pass@host:port.
    """
    if not PROXY_SERVER:
        return None
    url = PROXY_SERVER
    if PROXY_USERNAME and "@" not in PROXY_SERVER.split("://", 1)[-1]:
        scheme, _, rest = PROXY_SERVER.partition("://")
        if rest:
            creds = PROXY_USERNAME
            if PROXY_PASSWORD:
                creds += f":{PROXY_PASSWORD}"
            url = f"{scheme}://{creds}@{rest}"
    return {"http": url, "https": url}


def _env_flag(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Stock watcher (worker.py)
#
# The worker re-checks every active watch on a fixed cadence and fires a
# WhatsApp alert whenever a product's availability changes (e.g. it comes back
# in stock on Swiggy Instamart). Everything below is env-driven so no cost is
# incurred and no secrets live in the repo.
# ---------------------------------------------------------------------------
WATCH_ENABLED = _env_flag("STOCKLY_WATCH_ENABLED", True)
# How often each watch is re-checked, in minutes.
WATCH_INTERVAL_MIN = int(os.environ.get("STOCKLY_WATCH_INTERVAL_MIN", "20"))
# Worker wakes up this often (seconds) to pick up watches whose interval elapsed.
WATCH_TICK_SEC = int(os.environ.get("STOCKLY_WATCH_TICK_SEC", "60"))
# Cap watches processed per wake-up so a huge list can't stampede the platforms.
WATCH_BATCH = int(os.environ.get("STOCKLY_WATCH_BATCH", "40"))
# Polite pause (seconds) between individual checks inside one cycle.
WATCH_PAUSE_SEC = float(os.environ.get("STOCKLY_WATCH_PAUSE_SEC", "1.5"))
# Random extra jitter (0..N seconds) added on top of WATCH_PAUSE_SEC between
# checks, so the request stream isn't perfectly periodic. Cadence-based rate
# limiters (e.g. Swiggy Instamart's CloudFront JA4 limiter) are easier to trip
# with clockwork-regular traffic, so a little randomness helps.
WATCH_PAUSE_JITTER_SEC = float(os.environ.get("STOCKLY_WATCH_PAUSE_JITTER_SEC", "1.5"))
# Hard ceiling (seconds) on a single browser-backed availability check
# (Instamart / Zepto). A stalled in-page fetch through a metered proxy can
# otherwise hang Playwright's promise forever, freezing the whole watch loop.
# On timeout we cancel the operation and reset the browser so the next check
# starts clean. Generous enough to cover a worst-case WAF re-prime.
# Lowered from 120s now that checks run on a bounded pool of queue workers
# rather than one sequential watcher process: there, a slow check only delayed
# the next watch; here it holds a scarce worker hostage while other users wait.
# The queue retries what this gives up on, so a re-prime still gets its chance —
# just not while occupying a worker for two minutes.
SCRAPER_CHECK_TIMEOUT_SEC = float(os.environ.get("STOCKLY_SCRAPER_CHECK_TIMEOUT_SEC", "45"))

# ───────────────────────────────────────────────────────────────────────────
# Execution model — queued checks (Redis + Dramatiq)
#
# Scraping must not run inside the web tier: Playwright/Chromium, gunicorn
# threads and SQLite writes otherwise compete for the same CPU/RAM, and a
# worker recycle kills in-flight searches. The API now only enqueues one task
# per (platform × product × pincode); dedicated worker processes execute them.
# ───────────────────────────────────────────────────────────────────────────
REDIS_URL = os.environ.get("STOCKLY_REDIS_URL", "redis://127.0.0.1:6379/0").strip()

# Master switch. When off (or Redis is unreachable at start-up) the API falls
# back to the legacy in-process daemon thread, so this migration can be rolled
# back without redeploying old code. Set to 0 for the pre-queue behaviour.
QUEUE_ENABLED = _env_flag("STOCKLY_QUEUE_ENABLED", True)

# Queue names. Platforms are grouped by cost profile so a cheap HTTP platform
# can never be starved by an expensive browser one, and so each group can be
# scaled (and concurrency-capped) independently.
QUEUE_HTTP = "http_checks"            # pure HTTP/TLS-impersonation scrapers
QUEUE_BROWSER = "browser_checks"      # headless Chromium scrapers
QUEUE_PROTECTED = "protected_checks"  # real-Chrome / WAF-heavy scrapers
QUEUE_CONTROL = "control"             # dispatch, finalise, maintenance

PLATFORM_QUEUE = {
    "blinkit": QUEUE_HTTP,
    "bigbasket": QUEUE_HTTP,
    "amazon": QUEUE_HTTP,
    "flipkart_com": QUEUE_HTTP,
    "zepto": QUEUE_BROWSER,
    "flipkart": QUEUE_BROWSER,
    "jiomart": QUEUE_BROWSER,
    "instamart": QUEUE_PROTECTED,
    "croma": QUEUE_PROTECTED,
    "apple": QUEUE_PROTECTED,
}


def _conc(platform, default):
    return int(os.environ.get(f"STOCKLY_CONCURRENCY_{platform.upper()}", str(default)))


# Per-platform ceiling on simultaneous checks, enforced *per worker process* by
# a semaphore in the client. The fleet-wide ceiling is therefore this times the
# number of worker processes serving the platform's queue, bounded in turn by
# that worker's thread count — so changing these and the compose commands are
# two halves of one decision.
#
# Deliberately conservative: 50 concurrent *users* must not mean 50 concurrent
# browsers. Retailer rate limits, not CPU, are the binding constraint for the
# protected platforms.
PLATFORM_CONCURRENCY = {
    "blinkit": _conc("blinkit", 6),
    # 2 here x 2 worker-http processes = 4 concurrent BigBasket checks, up from
    # the 1-per-process its old mutex allowed.
    "bigbasket": _conc("bigbasket", 2),
    # Amazon / Flipkart.com are pure HTTP like Blinkit; keep them below Blinkit's
    # ceiling — both are more CAPTCHA-prone under burst traffic.
    "amazon": _conc("amazon", 3),
    "flipkart_com": _conc("flipkart_com", 3),
    "zepto": _conc("zepto", 2),
    "flipkart": _conc("flipkart", 2),
    "jiomart": _conc("jiomart", 2),
    # Instamart's warm path is an HTTP call carrying the browser's cookies, not
    # a browser action, so it parallelises without a second Chromium.
    "instamart": _conc("instamart", 3),
    "croma": _conc("croma", 1),
    "apple": _conc("apple", 1),
}


def platform_slots(platform):
    """Simultaneous checks this *process* may run for ``platform``."""
    return max(1, PLATFORM_CONCURRENCY.get(platform, 1))

# How long to trust "this store serves that location" (see stockly/stores.py).
# A retailer's footprint changes on the timescale of new stores opening, not of
# stock moving, so a day is conservative. Nothing about availability is cached.
STORE_CACHE_TTL_SEC = int(os.environ.get("STOCKLY_STORE_CACHE_TTL_SEC", str(24 * 3600)))

# Fairness: cap how many checks of a single job may be in flight at once, so a
# 5,000-check search cannot monopolise the workers ahead of a 10-check one.
MAX_INFLIGHT_CHECKS_PER_JOB = int(
    os.environ.get("STOCKLY_MAX_INFLIGHT_CHECKS_PER_JOB", "8"))

# Safety limits — exceeded requests get a clear 4xx, never a crash.
MAX_ACTIVE_JOBS_PER_USER = int(os.environ.get("STOCKLY_MAX_ACTIVE_JOBS_PER_USER", "2"))
MAX_SEARCH_CHECKS = int(os.environ.get("STOCKLY_MAX_SEARCH_CHECKS", "5000"))
MAX_QUEUED_CHECKS_PER_USER = int(
    os.environ.get("STOCKLY_MAX_QUEUED_CHECKS_PER_USER", "2000"))
MAX_TOTAL_QUEUED_CHECKS = int(os.environ.get("STOCKLY_MAX_TOTAL_QUEUED_CHECKS", "20000"))

# Timeouts. Every retailer interaction is bounded so one hung request can never
# permanently occupy a worker.
HTTP_CHECK_TIMEOUT_SEC = float(os.environ.get("STOCKLY_HTTP_CHECK_TIMEOUT_SEC", "15"))
BROWSER_CHECK_TIMEOUT_SEC = float(
    os.environ.get("STOCKLY_BROWSER_CHECK_TIMEOUT_SEC", "40"))
# Absolute ceiling for one task, whatever the platform.
TASK_MAX_TIMEOUT_SEC = float(os.environ.get("STOCKLY_TASK_MAX_TIMEOUT_SEC", "60"))

# Per-request ceiling for the direct-HTTP scrapers. A quick-commerce API either
# answers in a second or is tarpitting us: when we are blocked the socket opens
# and then stays silent, so a generous value buys nothing and just pins a worker
# thread. Every curl call in the HTTP path must use this, otherwise the retry
# loop below can outlast the queue's time limit.
HTTP_REQUEST_TIMEOUT_SEC = float(
    os.environ.get("STOCKLY_HTTP_REQUEST_TIMEOUT_SEC", "12"))
# Attempts the HTTP scraper makes internally, before our own queue-level retry.
HTTP_SCRAPER_MAX_RETRIES = int(os.environ.get("STOCKLY_BLINKIT_MAX_RETRIES", "2"))
# Backoff it sleeps between those attempts: 3s, 6s, ... (see blinkit_search).
HTTP_SCRAPER_BACKOFF_BASE_SEC = 3.0


def http_scraper_worst_case_sec():
    """Longest ``blinkit_search`` can run before returning on its own.

    Each attempt can burn a full request timeout and is followed by a backoff,
    so the queue limit has to clear the sum of both — counting only the sleeps
    (the mistake this function exists to prevent) understates it several-fold.
    """
    attempts = max(HTTP_SCRAPER_MAX_RETRIES, 1)
    requests_sec = attempts * HTTP_REQUEST_TIMEOUT_SEC
    backoff_sec = HTTP_SCRAPER_BACKOFF_BASE_SEC * attempts * (attempts + 1) / 2
    return requests_sec + backoff_sec

# Retries apply only to *infrastructure* failures (timeout, reset, 5xx, 429,
# WAF, browser crash) — never to legitimate business results.
CHECK_MAX_RETRIES = int(os.environ.get("STOCKLY_CHECK_MAX_RETRIES", "2"))
CHECK_RETRY_BASE_SEC = float(os.environ.get("STOCKLY_CHECK_RETRY_BASE_SEC", "5"))

# A job whose workers died stops making progress; the reaper fails it rather
# than leaving the client polling a corpse forever.
JOB_STALE_TIMEOUT_SEC = int(os.environ.get("STOCKLY_JOB_STALE_TIMEOUT_SEC", "180"))
RECOVERY_TICK_SEC = int(os.environ.get("STOCKLY_RECOVERY_TICK_SEC", "60"))

# Structured (JSON) logs — on by default in production, off locally for
# human-readable output.
LOG_JSON = _env_flag("STOCKLY_LOG_JSON", IS_PROD)
LOG_LEVEL = os.environ.get("STOCKLY_LOG_LEVEL", "info").upper()


def platform_timeout(platform):
    """Hard wall-clock budget for one check on ``platform``."""
    queue = PLATFORM_QUEUE.get(platform, QUEUE_BROWSER)
    if queue == QUEUE_HTTP:
        # Nothing enforces a wall clock on the HTTP path; its real ceiling is
        # however long the scraper's own retry loop can take.
        base = max(HTTP_CHECK_TIMEOUT_SEC, http_scraper_worst_case_sec())
    else:
        base = BROWSER_CHECK_TIMEOUT_SEC
    return min(base, TASK_MAX_TIMEOUT_SEC)


# Grace added on top of a check's budget before the queue kills the task.
_TASK_GRACE_SEC = 15


def task_time_limit_ms(queue):
    """Outer (queue) time limit for a task on ``queue``, in milliseconds.

    Always strictly greater than any timeout the scraper enforces internally.
    If the outer limit fires first the task is killed mid-flight and
    redelivered — which sends *another* request to a retailer that is usually
    already rate-limiting us. Letting the inner timeout win instead yields a
    normal error result that we can classify and retry on our own terms.
    """
    if queue == QUEUE_HTTP:
        # The HTTP scraper has no wall clock of its own — it just retries — so
        # the floor here is its full retry loop, not the nominal check budget.
        budget = max(HTTP_CHECK_TIMEOUT_SEC, http_scraper_worst_case_sec())
    elif queue == QUEUE_BROWSER:
        budget = BROWSER_CHECK_TIMEOUT_SEC
    else:
        budget = TASK_MAX_TIMEOUT_SEC
    # Browser platforms police themselves with SCRAPER_CHECK_TIMEOUT_SEC; the
    # queue must sit outside whichever of the two is larger.
    if queue in (QUEUE_BROWSER, QUEUE_PROTECTED):
        budget = max(budget, SCRAPER_CHECK_TIMEOUT_SEC)
    return int((budget + _TASK_GRACE_SEC) * 1000)


# ── Token / credit system (monetisation) ────────────────────────────────────
# A non-admin user spends tokens per *billable* availability result (one per
# pincode × platform × product that returns a real in-stock / out-of-stock
# answer). Not-listed / unserviceable / error / geocode-failed are free, so a
# user is never charged for a check that didn't actually resolve stock. Admins
# are never charged. Costs are configurable via env for easy repricing.
TOKEN_COST_IN_STOCK = int(os.environ.get("STOCKLY_TOKEN_COST_IN_STOCK", "2"))
TOKEN_COST_OUT_OF_STOCK = int(os.environ.get("STOCKLY_TOKEN_COST_OUT_OF_STOCK", "1"))
# Map result status -> token cost. Only these statuses are billable.
TOKEN_COST = {
    "available": TOKEN_COST_IN_STOCK,
    "out_of_stock": TOKEN_COST_OUT_OF_STOCK,
}
# Cost per watch creation (each product × pincode × platform combination).
TOKEN_COST_WATCH = int(os.environ.get("STOCKLY_TOKEN_COST_WATCH", "1"))
# Cost per watch poll cycle (charged when the worker checks a watch).
TOKEN_COST_WATCH_POLL = int(os.environ.get("STOCKLY_TOKEN_COST_WATCH_POLL", "1"))

# Per-platform minimum spacing (seconds) between two *consecutive* checks of
# that platform, to dodge fingerprint/cadence rate limits. Swiggy Instamart's
# search endpoint sits behind a CloudFront "JA4-ratelimit-instamart" limiter
# that 403s bursts from a single IP/TLS-fingerprint, so we space those out far
# more than the cheaper platforms. A random 0..jitter is added on top. Any
# platform not listed here is not throttled (min spacing 0).
INSTAMART_MIN_INTERVAL_SEC = float(
    os.environ.get("STOCKLY_INSTAMART_MIN_INTERVAL_SEC", "12"))
INSTAMART_JITTER_SEC = float(os.environ.get("STOCKLY_INSTAMART_JITTER_SEC", "6"))
PLATFORM_MIN_INTERVAL_SEC = {"instamart": INSTAMART_MIN_INTERVAL_SEC}
PLATFORM_JITTER_SEC = {"instamart": INSTAMART_JITTER_SEC}
# When to alert:
#   "change"       -> any meaningful status change (in stock <-> out of stock ...)
#   "availability" -> only when it (re)enters stock
#   "price_drop"   -> when the item is in stock AND its price is lower than the
#                     previously recorded price. The first check has no
#                     baseline, so it never alerts.
WATCH_NOTIFY_ON = os.environ.get("STOCKLY_WATCH_NOTIFY_ON", "change").strip().lower()
# Transient errors (WAF/geocode/network) never overwrite a good state or alert;
# after this many consecutive errors the worker sends one "can't check" heads-up.
WATCH_ERROR_ALERT_AFTER = int(os.environ.get("STOCKLY_WATCH_ERROR_ALERT_AFTER", "0"))

# ---------------------------------------------------------------------------
# WhatsApp delivery (whatsapp.py) — free by default.
#
#   provider=webjs      (self-hosted whatsapp-web.js bridge; 100% free, sends
#                        from your own number to anyone. Scan a QR once.)
#   provider=callmebot  (100% free for personal alerts to your own number; get
#                        an api key from https://www.callmebot.com/ — the free
#                        bot is sometimes at capacity and stops issuing keys.)
#   provider=meta       (WhatsApp Cloud API free tier; needs a Meta app + an
#                        approved template for un-prompted messages)
#   provider=none       (log only — useful for local testing)
# ---------------------------------------------------------------------------
WHATSAPP_PROVIDER = os.environ.get("STOCKLY_WHATSAPP_PROVIDER", "callmebot").strip().lower()
# Default recipient in international format (e.g. 919876543210). Per-watch
# recipients override this.
WHATSAPP_TO = os.environ.get("STOCKLY_WHATSAPP_TO", "").strip()

# CallMeBot
CALLMEBOT_PHONE = os.environ.get("STOCKLY_CALLMEBOT_PHONE", "").strip() or WHATSAPP_TO
CALLMEBOT_APIKEY = os.environ.get("STOCKLY_CALLMEBOT_APIKEY", "").strip()

# Meta WhatsApp Cloud API
META_TOKEN = os.environ.get("STOCKLY_META_TOKEN", "").strip()
META_PHONE_ID = os.environ.get("STOCKLY_META_PHONE_ID", "").strip()
META_API_VERSION = os.environ.get("STOCKLY_META_API_VERSION", "v21.0").strip()
# Optional approved template name (required to message outside the 24h window).
META_TEMPLATE = os.environ.get("STOCKLY_META_TEMPLATE", "").strip()
META_TEMPLATE_LANG = os.environ.get("STOCKLY_META_TEMPLATE_LANG", "en").strip()

# Self-hosted whatsapp-web.js bridge (wa-bridge/). The Python side just POSTs
# {to, message} to this local HTTP service, which owns the WhatsApp Web session.
WA_BRIDGE_URL = os.environ.get("STOCKLY_WA_BRIDGE_URL", "http://127.0.0.1:3001").strip()
WA_BRIDGE_TOKEN = os.environ.get("STOCKLY_WA_BRIDGE_TOKEN", "").strip()
