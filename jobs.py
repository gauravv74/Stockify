#!/usr/bin/env python3
"""Background search jobs — decouple a "check availability" run from the client.

A normal ``/api/check`` streamed results straight down the HTTP response, so the
whole run died the moment the browser tab was backgrounded / suspended (very
common on phones when you switch apps) or the connection blipped.

Instead we now run each search as a *server-side job*: the checks execute in a
daemon thread and every result row is persisted here (same SQLite/WAL DB as
``watches.py``/``auth.py``). The browser just polls for new rows from a cursor,
so it can go away and come back — reload the page, lock the phone, switch tabs —
without losing the run. Because state lives in the DB (not process memory), any
gunicorn worker can serve the poll, and a crash mid-run is safe.

Tables
------
* ``search_jobs``   — one row per run (status, total, meta, cancel flag).
* ``search_events`` — one row per result; the autoincrement ``id`` doubles as a
                      global cursor the client advances through.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import config

_lock = threading.Lock()
_initialized = False

# Jobs (and their events) older than this are purged on the next create so the
# tables don't grow without bound. A search run is ephemeral by nature.
JOB_TTL_HOURS = 12


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _conn():
    conn = sqlite3.connect(str(config.DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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
    global _initialized
    with _lock:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with _conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS search_jobs (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    status TEXT NOT NULL DEFAULT 'running',
                    total INTEGER NOT NULL DEFAULT 0,
                    meta TEXT,
                    detail TEXT,
                    cancel INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS search_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_job "
                "ON search_events(job_id, id)"
            )
        _initialized = True


def _purge_old(conn) -> None:
    cutoff = f"datetime('now', '-{int(JOB_TTL_HOURS)} hours')"
    old = conn.execute(
        f"SELECT id FROM search_jobs WHERE updated_at <= {cutoff}"
    ).fetchall()
    for row in old:
        conn.execute("DELETE FROM search_events WHERE job_id = ?", (row["id"],))
        conn.execute("DELETE FROM search_jobs WHERE id = ?", (row["id"],))


def create_job(user_id, meta, total) -> str:
    job_id = uuid.uuid4().hex
    now = _now()
    with _conn() as conn:
        _purge_old(conn)
        conn.execute(
            """INSERT INTO search_jobs
                 (id, user_id, status, total, meta, cancel, created_at, updated_at)
               VALUES (?, ?, 'running', ?, ?, 0, ?, ?)""",
            (job_id, user_id, int(total), json.dumps(meta or {}), now, now),
        )
    return job_id


def add_event(job_id, payload) -> None:
    now = _now()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO search_events (job_id, payload, created_at) VALUES (?, ?, ?)",
            (job_id, json.dumps(payload), now),
        )
        conn.execute(
            "UPDATE search_jobs SET updated_at = ? WHERE id = ?", (now, job_id)
        )


def set_status(job_id, status, detail=None) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE search_jobs SET status = ?, detail = ?, updated_at = ? WHERE id = ?",
            (status, detail, _now(), job_id),
        )


def request_cancel(job_id, user_id=None) -> bool:
    with _conn() as conn:
        if user_id is not None:
            cur = conn.execute(
                "UPDATE search_jobs SET cancel = 1, updated_at = ? "
                "WHERE id = ? AND user_id IS ?",
                (_now(), job_id, user_id),
            )
        else:
            cur = conn.execute(
                "UPDATE search_jobs SET cancel = 1, updated_at = ? WHERE id = ?",
                (_now(), job_id),
            )
        return cur.rowcount > 0


def is_canceled(job_id) -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT cancel FROM search_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    return bool(row and row["cancel"])


def get_job(job_id, user_id=None):
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM search_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    if not row:
        return None
    if user_id is not None and row["user_id"] != user_id:
        return None
    d = dict(row)
    try:
        d["meta"] = json.loads(d.get("meta") or "null")
    except Exception:
        d["meta"] = None
    d["cancel"] = bool(d.get("cancel"))
    return d


def get_events(job_id, after_seq=0):
    """Return result rows with cursor id > ``after_seq`` (oldest first)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, payload FROM search_events "
            "WHERE job_id = ? AND id > ? ORDER BY id ASC",
            (job_id, int(after_seq or 0)),
        ).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload"])
        except Exception:
            payload = {}
        payload["seq"] = r["id"]
        out.append(payload)
    return out


if __name__ == "__main__":
    init_db()
    print("search_jobs / search_events ready in", config.DB_PATH)
