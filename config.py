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
