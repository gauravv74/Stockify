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
# When to alert:
#   "change"       -> any meaningful status change (in stock <-> out of stock ...)
#   "availability" -> only when it (re)enters stock
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
