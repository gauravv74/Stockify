#!/usr/bin/env python3
"""Stock watches — persistent list of (product x pincode x platform) to monitor.

Shares the same SQLite database as ``auth.py`` (WAL mode), so the Flask web app
(which lets a user add/remove watches) and the standalone ``worker.py`` (which
polls them) coordinate purely through the DB — no extra infra, no cost.

Each watch remembers its *last known state* so the worker can detect a change
(e.g. out_of_stock -> available) and fire exactly one alert per transition
instead of spamming on every poll.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

import config

_lock = threading.Lock()
_initialized = False

# Statuses we treat as "transient / unknown" — they must NOT overwrite the last
# good state or trigger an availability alert (WAF challenges, geocode misses,
# network blips are common with Instamart).
TRANSIENT = {"error", "geocode_failed", ""}


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
                CREATE TABLE IF NOT EXISTS watches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    username TEXT,
                    platform TEXT NOT NULL DEFAULT 'instamart',
                    product TEXT NOT NULL,
                    pincode TEXT NOT NULL,
                    place TEXT,
                    lat TEXT,
                    lon TEXT,
                    notify_to TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_status TEXT,
                    last_available INTEGER,
                    last_detail TEXT,
                    last_checked_at TEXT,
                    last_change_at TEXT,
                    last_notified_at TEXT,
                    check_count INTEGER NOT NULL DEFAULT 0,
                    error_streak INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(user_id, platform, product, pincode)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_watches_active "
                "ON watches(active, last_checked_at)"
            )
            # Migration: numeric price of the last non-transient check, used by
            # the "price_drop" notify mode to compare against the current price.
            # ALTER is a no-op-with-guard so existing DBs upgrade in place.
            try:
                conn.execute("ALTER TABLE watches ADD COLUMN last_price REAL")
            except sqlite3.OperationalError:
                pass  # column already exists
            # Migration: optional per-watch target price. In "threshold" notify
            # mode the worker alerts when an in-stock price is <= this value.
            try:
                conn.execute("ALTER TABLE watches ADD COLUMN price_threshold REAL")
            except sqlite3.OperationalError:
                pass  # column already exists
            # Runtime-editable settings (key/value), so an admin can change the
            # global alert mode from the UI without a redeploy.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
        _initialized = True


def get_setting(key, default=None):
    """Read a runtime setting, or ``default`` if unset."""
    if not _initialized:
        init_db()
    with _conn() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    """Persist a runtime setting."""
    if not _initialized:
        init_db()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, None if value is None else str(value)),
        )


# Alert modes the UI exposes as a single global choice.
NOTIFY_MODES = ("threshold", "price_drop", "availability", "change")


def get_notify_mode():
    """Current global alert mode: DB setting overrides the env/config default."""
    mode = (get_setting("notify_on") or config.WATCH_NOTIFY_ON or "price_drop").strip().lower()
    return mode if mode in NOTIFY_MODES else "price_drop"


def get_interval_min():
    """Re-check interval in minutes: DB setting overrides the env/config default."""
    try:
        val = int(get_setting("interval_min") or config.WATCH_INTERVAL_MIN)
    except (TypeError, ValueError):
        val = config.WATCH_INTERVAL_MIN
    return max(1, val)


def _row_to_watch(row) -> dict | None:
    if not row:
        return None
    d = dict(row)
    try:
        d["last_detail"] = json.loads(d.get("last_detail") or "null")
    except Exception:
        d["last_detail"] = None
    d["active"] = bool(d.get("active"))
    return d


def add_watch(user, platform, product, pincode, notify_to=None, price_threshold=None):
    """Create (or re-activate) a watch. Returns (watch, error)."""
    platform = (platform or "instamart").strip().lower()
    product = (product or "").strip()
    pincode = (pincode or "").strip()
    if not product:
        return None, "Product is required."
    if not (len(pincode) == 6 and pincode.isdigit()):
        return None, "Pincode must be 6 digits."
    uid = (user or {}).get("id")
    uname = (user or {}).get("username")
    notify_to = (notify_to or "").strip() or None
    try:
        price_threshold = float(price_threshold) if price_threshold not in (None, "") else None
    except (TypeError, ValueError):
        price_threshold = None
    try:
        with _conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO watches
                    (user_id, username, platform, product, pincode, notify_to,
                     price_threshold, active, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(user_id, platform, product, pincode)
                DO UPDATE SET active = 1, notify_to = excluded.notify_to,
                              price_threshold = excluded.price_threshold
                """,
                (uid, uname, platform, product, pincode, notify_to,
                 price_threshold, _now()),
            )
            wid = cur.lastrowid
            if not wid:
                row = conn.execute(
                    """SELECT id FROM watches WHERE user_id IS ? AND platform = ?
                       AND product = ? AND pincode = ?""",
                    (uid, platform, product, pincode),
                ).fetchone()
                wid = row["id"] if row else None
        return get_watch(wid), None
    except sqlite3.IntegrityError as e:
        return None, str(e)


def get_watch(watch_id):
    with _conn() as conn:
        row = conn.execute("SELECT * FROM watches WHERE id = ?", (watch_id,)).fetchone()
    return _row_to_watch(row)


def list_watches(user_id=None, active_only=False):
    q = "SELECT * FROM watches"
    where, params = [], []
    if user_id is not None:
        where.append("user_id IS ?")
        params.append(user_id)
    if active_only:
        where.append("active = 1")
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY id DESC"
    with _conn() as conn:
        rows = conn.execute(q, params).fetchall()
    return [_row_to_watch(r) for r in rows]


def set_active(watch_id, active, user_id=None):
    with _conn() as conn:
        if user_id is not None:
            cur = conn.execute(
                "UPDATE watches SET active = ? WHERE id = ? AND user_id IS ?",
                (1 if active else 0, watch_id, user_id),
            )
        else:
            cur = conn.execute(
                "UPDATE watches SET active = ? WHERE id = ?",
                (1 if active else 0, watch_id),
            )
        return cur.rowcount > 0


def delete_watch(watch_id, user_id=None):
    with _conn() as conn:
        if user_id is not None:
            cur = conn.execute(
                "DELETE FROM watches WHERE id = ? AND user_id IS ?", (watch_id, user_id)
            )
        else:
            cur = conn.execute("DELETE FROM watches WHERE id = ?", (watch_id,))
        return cur.rowcount > 0


def due_watches(interval_min, limit):
    """Active watches never checked, or last checked >= interval_min ago.

    Uses SQLite datetime math so the cutoff is computed in the DB and multiple
    worker instances (should you ever run more than one) stay consistent.
    """
    cutoff_expr = f"datetime('now', '-{int(interval_min)} minutes')"
    with _conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM watches
            WHERE active = 1
              AND (last_checked_at IS NULL
                   OR datetime(last_checked_at) <= {cutoff_expr})
            ORDER BY (last_checked_at IS NOT NULL), datetime(last_checked_at) ASC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return [_row_to_watch(r) for r in rows]


def save_geo(watch_id, lat, lon, place):
    with _conn() as conn:
        conn.execute(
            "UPDATE watches SET lat = ?, lon = ?, place = ? WHERE id = ?",
            (str(lat) if lat is not None else None,
             str(lon) if lon is not None else None, place, watch_id),
        )


def record_result(watch_id, status, available, detail, changed, notified,
                  price=None):
    """Persist the outcome of one check.

    Transient statuses only bump the error streak + last_checked_at; they never
    clobber the last good state, so a WAF blip can't produce a false alert.

    ``price`` (a parsed float, or None) becomes the new baseline the next
    ``price_drop`` comparison uses. A None price leaves the stored value alone
    so a platform that omits price can't wipe the last known one.
    """
    now = _now()
    detail_json = json.dumps(detail) if detail is not None else None
    with _conn() as conn:
        if status in TRANSIENT:
            conn.execute(
                """UPDATE watches
                   SET last_checked_at = ?, check_count = check_count + 1,
                       error_streak = error_streak + 1
                   WHERE id = ?""",
                (now, watch_id),
            )
            return
        sets = [
            "last_status = ?", "last_available = ?", "last_detail = ?",
            "last_checked_at = ?", "check_count = check_count + 1",
            "error_streak = 0",
        ]
        params = [status, (1 if available else 0), detail_json, now]
        if price is not None:
            sets.append("last_price = ?")
            params.append(price)
        if changed:
            sets.append("last_change_at = ?")
            params.append(now)
        if notified:
            sets.append("last_notified_at = ?")
            params.append(now)
        params.append(watch_id)
        conn.execute(f"UPDATE watches SET {', '.join(sets)} WHERE id = ?", params)


if __name__ == "__main__":
    init_db()
    print("watches table ready in", config.DB_PATH)
    for w in list_watches():
        print(w["id"], w["platform"], w["product"], w["pincode"],
              "->", w.get("last_status"))
