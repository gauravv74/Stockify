#!/usr/bin/env python3
"""Background search jobs — decouple a "check availability" run from the client.

A search is a *server-side job*. The API creates the job row and enqueues one
task per (platform × product × pincode); dedicated worker processes execute
them and append result rows here. The browser polls for new rows from a cursor,
so it can go away and come back — reload the page, lock the phone, switch tabs —
without losing the run. Because all state lives in the DB, any API worker can
serve the poll and no result depends on a particular process staying alive.

Tables
------
* ``search_jobs``   — one row per run (status, totals, progress, cancel flag,
                      heartbeat).
* ``search_events`` — one row per result; the autoincrement ``id`` doubles as a
                      global cursor the client advances through. ``check_id``
                      makes inserts idempotent so a retried or duplicated task
                      cannot produce a duplicate row.

Statuses
--------
``queued`` → ``running`` → one of ``done`` / ``canceled`` / ``error`` /
``exhausted``. ``canceling`` is the transitional state while in-flight checks
drain. ``done``/``error`` are kept (rather than "completed"/"failed") because
the shipped frontend and the mobile client already branch on those strings.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import config

_lock = threading.Lock()
_initialized = False

# Jobs (and their events) older than this are purged on the next create so the
# tables don't grow without bound. A search run is ephemeral by nature.
JOB_TTL_HOURS = 12

QUEUED = "queued"
RUNNING = "running"
CANCELING = "canceling"
CANCELED = "canceled"
DONE = "done"
ERROR = "error"
EXHAUSTED = "exhausted"

ACTIVE_STATUSES = (QUEUED, RUNNING, CANCELING)
TERMINAL_STATUSES = (DONE, CANCELED, ERROR, EXHAUSTED)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ago(seconds) -> str:
    """ISO timestamp ``seconds`` in the past.

    Cutoffs are computed in Python, never with SQLite's ``datetime('now', ...)``:
    that returns ``YYYY-MM-DD HH:MM:SS`` while our columns hold ISO-8601 with a
    ``T`` separator, and comparing the two as strings is wrong (``'T' > ' '``,
    so a same-day row always sorts newer than the cutoff).
    """
    return (datetime.now(timezone.utc) - timedelta(seconds=int(seconds))).isoformat()


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
                    status TEXT NOT NULL DEFAULT 'queued',
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

            # Additive migrations. Stage 5 replaces this with Alembic once the
            # store moves to PostgreSQL; until then new columns are guarded.
            job_cols = {r["name"] for r in conn.execute("PRAGMA table_info(search_jobs)")}
            for name, ddl in (
                ("completed_checks", "INTEGER NOT NULL DEFAULT 0"),
                ("failed_checks", "INTEGER NOT NULL DEFAULT 0"),
                ("started_at", "TEXT"),
                ("completed_at", "TEXT"),
                ("last_heartbeat_at", "TEXT"),
                # Execution plan (pincodes/products/platforms). Kept out of
                # `meta` because meta is echoed to the client on every poll and
                # a nationwide run carries hundreds of pincodes.
                ("plan", "TEXT"),
            ):
                if name not in job_cols:
                    conn.execute(f"ALTER TABLE search_jobs ADD COLUMN {name} {ddl}")

            event_cols = {r["name"] for r in conn.execute("PRAGMA table_info(search_events)")}
            if "check_id" not in event_cols:
                conn.execute("ALTER TABLE search_events ADD COLUMN check_id TEXT")

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_job "
                "ON search_events(job_id, id)"
            )
            # Idempotency: a retried/duplicated task cannot append twice.
            # NULL check_id rows (legacy) are exempt, which SQLite allows.
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_check "
                "ON search_events(job_id, check_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_user_status "
                "ON search_jobs(user_id, status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status_heartbeat "
                "ON search_jobs(status, last_heartbeat_at)"
            )
        _initialized = True


def _purge_old(conn) -> None:
    cutoff = _ago(JOB_TTL_HOURS * 3600)
    old = conn.execute(
        "SELECT id FROM search_jobs WHERE updated_at <= ?", (cutoff,)
    ).fetchall()
    for row in old:
        conn.execute("DELETE FROM search_events WHERE job_id = ?", (row["id"],))
        conn.execute("DELETE FROM search_jobs WHERE id = ?", (row["id"],))


def create_job(user_id, meta, total, status=QUEUED, plan=None) -> str:
    job_id = uuid.uuid4().hex
    now = _now()
    with _conn() as conn:
        _purge_old(conn)
        conn.execute(
            """INSERT INTO search_jobs
                 (id, user_id, status, total, meta, plan, cancel,
                  created_at, updated_at, last_heartbeat_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
            (job_id, user_id, status, int(total), json.dumps(meta or {}),
             json.dumps(plan) if plan is not None else None, now, now, now),
        )
    return job_id


def get_plan(job_id):
    """The stored execution plan, or None. Never exposed to clients."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT plan FROM search_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    if not row or not row["plan"]:
        return None
    try:
        return json.loads(row["plan"])
    except ValueError:
        return None


def mark_running(job_id) -> None:
    """First worker to pick up a task flips the job out of ``queued``."""
    now = _now()
    with _conn() as conn:
        conn.execute(
            """UPDATE search_jobs
               SET status = ?, started_at = COALESCE(started_at, ?),
                   updated_at = ?, last_heartbeat_at = ?
               WHERE id = ? AND status = ?""",
            (RUNNING, now, now, now, job_id, QUEUED),
        )


def heartbeat(job_id) -> None:
    now = _now()
    with _conn() as conn:
        conn.execute(
            "UPDATE search_jobs SET last_heartbeat_at = ?, updated_at = ? WHERE id = ?",
            (now, now, job_id),
        )


def add_event(job_id, payload, check_id=None) -> bool:
    """Append a result row. Returns True when it was actually inserted.

    ``check_id`` makes the write idempotent: a duplicate delivery of the same
    logical check is silently dropped rather than producing a second row (which
    the client would render twice, since it dedupes on ``seq``).
    """
    now = _now()
    with _conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO search_events (job_id, payload, created_at, check_id) "
            "VALUES (?, ?, ?, ?)",
            (job_id, json.dumps(payload), now, check_id),
        )
        inserted = bool(cur.rowcount)
        conn.execute(
            "UPDATE search_jobs SET updated_at = ?, last_heartbeat_at = ? WHERE id = ?",
            (now, now, job_id),
        )
    return inserted


def record_result(job_id, check_id, payload, failed=False):
    """Persist one check result and advance job progress atomically.

    Returns ``(inserted, completed_checks, total)``. Progress only advances when
    the row was genuinely new, so retries can't inflate the counter past
    ``total`` and trip a premature finalisation.
    """
    now = _now()
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT OR IGNORE INTO search_events (job_id, payload, created_at, check_id) "
            "VALUES (?, ?, ?, ?)",
            (job_id, json.dumps(payload), now, check_id),
        )
        inserted = bool(cur.rowcount)
        if inserted:
            conn.execute(
                """UPDATE search_jobs
                   SET completed_checks = completed_checks + 1,
                       failed_checks = failed_checks + ?,
                       updated_at = ?, last_heartbeat_at = ?
                   WHERE id = ?""",
                (1 if failed else 0, now, now, job_id),
            )
        else:
            conn.execute(
                "UPDATE search_jobs SET updated_at = ?, last_heartbeat_at = ? WHERE id = ?",
                (now, now, job_id),
            )
        row = conn.execute(
            "SELECT completed_checks, total FROM search_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    completed = int(row["completed_checks"]) if row else 0
    total = int(row["total"]) if row else 0
    return inserted, completed, total


def set_status(job_id, status, detail=None) -> None:
    now = _now()
    completed_at = now if status in TERMINAL_STATUSES else None
    with _conn() as conn:
        conn.execute(
            """UPDATE search_jobs
               SET status = ?, detail = ?, updated_at = ?,
                   completed_at = COALESCE(?, completed_at)
               WHERE id = ?""",
            (status, detail, now, completed_at, job_id),
        )


def finalize_if_complete(job_id) -> str | None:
    """Move a job to its terminal state once every check has reported.

    Called by each task as it finishes; the ``status IN (active)`` guard makes
    it safe for several workers to race on the final check.
    """
    now = _now()
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, cancel, completed_checks, total FROM search_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if not row or row["status"] in TERMINAL_STATUSES:
            return None
        done = int(row["completed_checks"]) >= int(row["total"])
        canceled = bool(row["cancel"])
        if not done and not canceled:
            return None
        # A cancelled run that nevertheless finished everything is reported as
        # done — the user got all their results.
        status = DONE if done else CANCELED
        conn.execute(
            "UPDATE search_jobs SET status = ?, updated_at = ?, completed_at = ? WHERE id = ?",
            (status, now, now, job_id),
        )
    return status


def request_cancel(job_id, user_id=None) -> bool:
    """Flag a job for cancellation. Queued tasks skip; running ones finish."""
    now = _now()
    with _conn() as conn:
        params = [now, CANCELING, job_id]
        sql = ("UPDATE search_jobs SET cancel = 1, updated_at = ?, "
               "status = CASE WHEN status IN ('queued','running') THEN ? ELSE status END "
               "WHERE id = ?")
        if user_id is not None:
            sql += " AND user_id IS ?"
            params.append(user_id)
        cur = conn.execute(sql, params)
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
    # Internal only: the plan can hold hundreds of pincodes and is echoed
    # nowhere, so keep it out of anything a route might serialise.
    d.pop("plan", None)
    return d


def get_events(job_id, after_seq=0, limit=1000):
    """Return result rows with cursor id > ``after_seq`` (oldest first)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, payload FROM search_events "
            "WHERE job_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
            (job_id, int(after_seq or 0), int(limit)),
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


def count_active_jobs(user_id) -> int:
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM search_jobs "
            "WHERE user_id IS ? AND status IN ('queued','running','canceling')",
            (user_id,),
        ).fetchone()
    return int(row["c"]) if row else 0


def _outstanding_sql(where):
    # CASE rather than MAX(a, b): inside SUM(), SQLite resolves a two-argument
    # max() as the aggregate, which would silently give the wrong total.
    return (
        "SELECT COALESCE(SUM(CASE WHEN total > completed_checks "
        "THEN total - completed_checks ELSE 0 END), 0) AS c "
        f"FROM search_jobs WHERE {where}"
    )


def queued_checks_for_user(user_id) -> int:
    """Outstanding (not yet completed) checks across a user's active jobs."""
    with _conn() as conn:
        row = conn.execute(
            _outstanding_sql(
                "user_id IS ? AND status IN ('queued','running','canceling')"),
            (user_id,),
        ).fetchone()
    return int(row["c"]) if row else 0


def total_queued_checks() -> int:
    with _conn() as conn:
        row = conn.execute(
            _outstanding_sql("status IN ('queued','running','canceling')")
        ).fetchone()
    return int(row["c"]) if row else 0


def stale_jobs(timeout_sec):
    """Active jobs with no heartbeat inside ``timeout_sec`` — their workers died."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, user_id, status, total, completed_checks, last_heartbeat_at "
            "FROM search_jobs "
            "WHERE status IN ('queued','running','canceling') "
            "AND (last_heartbeat_at IS NULL OR last_heartbeat_at <= ?)",
            (_ago(timeout_sec),),
        ).fetchall()
    return [dict(r) for r in rows]


def active_job_count() -> int:
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM search_jobs "
            "WHERE status IN ('queued','running','canceling')"
        ).fetchone()
    return int(row["c"]) if row else 0


if __name__ == "__main__":
    init_db()
    print("search_jobs / search_events ready in", config.DB_PATH)
