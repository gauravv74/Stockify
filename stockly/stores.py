#!/usr/bin/env python3
"""Cache of "which of the retailer's stores serves this location".

Several platforms answer a check in two steps: resolve the location to one of
their own stores or serving areas, then search that store's catalogue. The
first step is the expensive one — for Instamart it is a browser round trip that
costs more than the search itself — and its answer barely ever changes, because
it is a fact about the retailer's footprint rather than about stock.

Deliberately *not* a result cache. Two pincodes served by the same store still
run their own search, because stock is what the caller asked about and stock
moves by the minute. Only the store identity is remembered, so nothing here can
make a check report availability it did not observe.

Entries expire (``STOCKLY_STORE_CACHE_TTL_SEC``, default 24h) so a new store
eventually gets picked up, and callers can :func:`forget` a key immediately
when a search fails in a way that suggests the store is wrong.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import config

log = logging.getLogger("stockly.stores")

_lock = threading.Lock()
_ready = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def _conn():
    conn = sqlite3.connect(str(config.DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    global _ready
    # Checked before the lock: every read and write calls this, and concurrent
    # checks should not queue on a mutex to learn the table already exists.
    if _ready:
        return
    with _lock:
        if _ready:
            return
        with _conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS store_cache (
                    platform TEXT NOT NULL,
                    key TEXT NOT NULL,
                    store_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (platform, key)
                )
                """
            )
        _ready = True


def key_for(lat, lon) -> str:
    """Cache key for a coordinate pair.

    Rounded to ~11m so the same pincode always hits the same row: callers pass
    coordinates straight from the geocache, but a float that has been through
    JSON or a string conversion can differ in its last digits.
    """
    try:
        return f"{float(lat):.4f},{float(lon):.4f}"
    except (TypeError, ValueError):
        return f"{lat},{lon}"


def get(platform, key):
    """The cached store id, or None when absent or expired."""
    init_db()
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT store_id, updated_at FROM store_cache "
                "WHERE platform = ? AND key = ?", (platform, str(key))).fetchone()
    except sqlite3.Error:
        log.warning("store_cache_read_failed", exc_info=True)
        return None
    if not row:
        return None
    try:
        age = _now() - datetime.fromisoformat(row["updated_at"])
    except (TypeError, ValueError):
        return None
    if age > timedelta(seconds=config.STORE_CACHE_TTL_SEC):
        return None
    return row["store_id"]


def put(platform, key, store_id) -> None:
    if not store_id:
        return
    init_db()
    try:
        with _conn() as conn:
            conn.execute(
                "INSERT INTO store_cache (platform, key, store_id, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(platform, key) DO UPDATE SET "
                "store_id = excluded.store_id, updated_at = excluded.updated_at",
                (platform, str(key), str(store_id), _now().isoformat()))
    except sqlite3.Error:
        # A cache that cannot be written is a slow check, not a failed one.
        log.warning("store_cache_write_failed", exc_info=True)


def forget(platform, key) -> None:
    """Drop an entry that led to a bad search, so the next check re-resolves."""
    init_db()
    try:
        with _conn() as conn:
            conn.execute("DELETE FROM store_cache WHERE platform = ? AND key = ?",
                         (platform, str(key)))
    except sqlite3.Error:
        log.warning("store_cache_delete_failed", exc_info=True)
