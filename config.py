#!/usr/bin/env python3
"""Stockly runtime configuration (env-driven)."""

from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
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
