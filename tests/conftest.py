"""Shared test fixtures.

Every test runs against a throwaway SQLite database in a temp directory, so the
suite never touches ``data/stockly.db``. Environment is set *before* ``config``
is imported, because ``config`` resolves and creates DATA_DIR at import time.
"""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Must happen before `import config` anywhere in the process.
_TMP_DATA = Path(tempfile.mkdtemp(prefix="stockly_test_"))
os.environ.setdefault("STOCKLY_DATA_DIR", str(_TMP_DATA))
os.environ.setdefault("STOCKLY_ENV", "development")
os.environ.setdefault("STOCKLY_SECRET_KEY", "test-secret-not-used-in-prod")
os.environ.setdefault("STOCKLY_LOG_JSON", "0")
# Keep the queue off by default so unit tests never need a live Redis; the
# tests that exercise enqueueing turn it on explicitly with a stub broker.
os.environ.setdefault("STOCKLY_QUEUE_ENABLED", "0")

import config  # noqa: E402


@pytest.fixture
def db(monkeypatch, tmp_path):
    """Point every module's connection helper at an isolated database file."""
    db_path = tmp_path / f"test_{uuid.uuid4().hex}.db"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)

    import auth
    import jobs
    import watches

    auth.init_db()
    jobs.init_db()
    watches.init_db()
    return db_path


@pytest.fixture
def admin_user(db):
    import auth
    user = auth.find_user_by_username(config.DEFAULT_ADMIN_USER)
    assert user, "init_db should seed a default admin"
    return auth._public_user(user)


@pytest.fixture
def make_user(db):
    """Factory for regular (non-admin) users with a token balance."""
    import auth

    def _make(username=None, password="password123", tokens=100,
              platforms=None, **kwargs):
        username = username or f"u{uuid.uuid4().hex[:8]}"
        # create_user wants a {platform: bool} map; accept a plain list here
        # because that reads better at the call site.
        if isinstance(platforms, (list, tuple, set)):
            platforms = {p: True for p in platforms}
        if platforms is None:
            platforms = {p: True for p in auth.ALL_PLATFORMS}
        user, err = auth.create_user(username, password, platforms=platforms, **kwargs)
        assert err is None, err
        if tokens:
            auth.grant_tokens(user["id"], tokens, actor="test")
            user = auth._public_user(auth.find_user_by_id(user["id"]))
        return user

    return _make


@pytest.fixture
def job(db, make_user):
    """A search job owned by a regular user."""
    import jobs
    user = make_user()
    job_id = jobs.create_job(user["id"], {"total": 3}, 3)
    return {"job_id": job_id, "user": user}


@pytest.fixture
def stub_check(monkeypatch):
    """Replace real scraping with a deterministic result.

    Returns a recorder so tests can assert exactly which checks ran, without
    ever touching a retailer or launching a browser.
    """
    from stockly import checks

    calls = []

    def _fake(platform, product, pincode, lat=None, lon=None, place=None, **kw):
        calls.append({"platform": platform, "product": product, "pincode": pincode})
        return {
            "status": "available",
            "available": "yes",
            "name": f"{product} ({platform})",
            "variant": "",
            "brand": "",
            "price": 100,
            "mrp": 120,
            "inventory": "",
            "eta": "10 mins",
            "merchant_id": "",
        }

    monkeypatch.setattr(checks, "_run_platform_check", _fake)
    return calls
