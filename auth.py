#!/usr/bin/env python3
"""Stockly auth — SQLite-backed admin + shared accounts with platform access."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps

from flask import jsonify, session
from werkzeug.security import check_password_hash, generate_password_hash

import config

ALL_PLATFORMS = ("blinkit", "instamart", "zepto", "bigbasket", "flipkart", "jiomart", "apple", "croma")
DEFAULT_ADMIN_USER = config.DEFAULT_ADMIN_USER
DEFAULT_ADMIN_PASS = config.DEFAULT_ADMIN_PASS

_lock = threading.Lock()
_initialized = False


def _now():
    return datetime.now(timezone.utc).isoformat()


def ensure_secret_key():
    if config.SECRET_KEY:
        return config.SECRET_KEY
    if config.SECRET_FILE.exists():
        return config.SECRET_FILE.read_text().strip()
    key = secrets.token_hex(32)
    config.SECRET_FILE.write_text(key)
    try:
        os.chmod(config.SECRET_FILE, 0o600)
    except OSError:
        pass
    return key


def _default_platforms(enabled=True):
    return {p: bool(enabled) for p in ALL_PLATFORMS}


def _platforms_to_json(platforms):
    base = _default_platforms(False)
    if isinstance(platforms, dict):
        for p in ALL_PLATFORMS:
            base[p] = bool(platforms.get(p, False))
    return json.dumps(base)


def _platforms_from_json(raw):
    try:
        data = json.loads(raw or "{}")
    except Exception:
        data = {}
    return {p: bool(data.get(p, False)) for p in ALL_PLATFORMS}


def _cities_to_json(cities):
    """Store the allowed-city ids as a JSON list. Empty list == all cities."""
    if not isinstance(cities, (list, tuple, set)):
        return "[]"
    seen, out = set(), []
    for c in cities:
        cid = str(c).strip().lower().replace(" ", "-")
        if cid and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return json.dumps(out)


def _cities_from_json(raw):
    try:
        data = json.loads(raw or "[]")
    except Exception:
        data = []
    if not isinstance(data, list):
        return []
    return [str(c) for c in data]


def _row_to_user(row):
    if not row:
        return None
    keys = row.keys()
    return {
        "id": row["id"],
        "username": row["username"],
        "password_hash": row["password_hash"],
        "role": row["role"],
        "platforms": _platforms_from_json(row["platforms_json"]),
        "cities": _cities_from_json(row["cities_json"]) if "cities_json" in keys else [],
        "allow_pincodes": bool(row["allow_pincodes"]) if "allow_pincodes" in keys else True,
        "token_balance": int(row["token_balance"]) if "token_balance" in keys and row["token_balance"] is not None else 0,
        "active": bool(row["active"]),
        "must_change_password": bool(row["must_change_password"]),
        "created_at": row["created_at"],
    }


def _public_user(u):
    return {
        "id": u["id"],
        "username": u["username"],
        "role": u["role"],
        "platforms": {p: bool(u.get("platforms", {}).get(p, False)) for p in ALL_PLATFORMS},
        # Empty list == unrestricted (all cities). Admins are always unrestricted.
        "cities": [] if u.get("role") == "admin" else list(u.get("cities", []) or []),
        "allow_pincodes": True if u.get("role") == "admin" else bool(u.get("allow_pincodes", True)),
        # Admins are unlimited; expose null so the UI can show "∞" for them.
        "token_balance": None if u.get("role") == "admin" else int(u.get("token_balance", 0) or 0),
        "active": bool(u.get("active", True)),
        "must_change_password": bool(u.get("must_change_password", False)),
        "created_at": u.get("created_at"),
    }


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


def _migrate_from_json(conn):
    path = config.LEGACY_USERS_JSON
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text())
    except Exception:
        return 0
    users = data.get("users") if isinstance(data, dict) else None
    if not users:
        return 0
    count = 0
    for u in users:
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO users
                (id, username, password_hash, role, platforms_json, active, must_change_password, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    u.get("id") or str(uuid.uuid4()),
                    u.get("username"),
                    u.get("password_hash"),
                    u.get("role") or "user",
                    _platforms_to_json(u.get("platforms") or _default_platforms(u.get("role") == "admin")),
                    1 if u.get("active", True) else 0,
                    1 if u.get("username") == DEFAULT_ADMIN_USER else 0,
                    u.get("created_at") or _now(),
                ),
            )
            count += 1
        except Exception:
            continue
    # Keep legacy file as backup rename once
    bak = path.with_suffix(".json.bak")
    if not bak.exists():
        try:
            path.rename(bak)
        except OSError:
            pass
    return count


def init_db():
    global _initialized
    with _lock:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with _conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('admin','user')),
                    platforms_json TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    must_change_password INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            # Additive migrations for per-user location access. Existing rows get
            # cities_json='[]' (all cities) and allow_pincodes=1 (backward compatible).
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
            if "cities_json" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN cities_json TEXT NOT NULL DEFAULT '[]'")
            if "allow_pincodes" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN allow_pincodes INTEGER NOT NULL DEFAULT 1")
            # Token/credit wallet (monetisation). New users start at 0; an admin
            # tops them up. Admins are never charged regardless of this value.
            if "token_balance" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN token_balance INTEGER NOT NULL DEFAULT 0")
            # Audit log of what each user searched (admins can review it).
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS searches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    username TEXT,
                    created_at TEXT NOT NULL,
                    platform TEXT,
                    products TEXT,
                    cities TEXT,
                    pincode_count INTEGER NOT NULL DEFAULT 0,
                    total_checks INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_searches_created ON searches(created_at DESC)")
            # Token ledger: an audit trail of every grant (+) and spend (-) so
            # admins can reconcile balances and see usage. balance_after is the
            # wallet total right after the entry was applied.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS token_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    delta INTEGER NOT NULL,
                    reason TEXT,
                    balance_after INTEGER,
                    actor TEXT,
                    meta TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_user ON token_ledger(user_id, id DESC)")
            n = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            created_default = False
            if n == 0:
                migrated = _migrate_from_json(conn)
                n = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
                if n == 0:
                    conn.execute(
                        """
                        INSERT INTO users
                        (id, username, password_hash, role, platforms_json, active, must_change_password, created_at)
                        VALUES (?, ?, ?, 'admin', ?, 1, 1, ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            DEFAULT_ADMIN_USER,
                            generate_password_hash(DEFAULT_ADMIN_PASS),
                            _platforms_to_json(_default_platforms(True)),
                            _now(),
                        ),
                    )
                    created_default = True
                elif migrated:
                    created_default = False
            _initialized = True
            return created_default


def ensure_users_file():
    """Back-compat boot hook used by app.py."""
    created = init_db()
    return None, created


def list_users():
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()
    return [_public_user(_row_to_user(r)) for r in rows]


def find_user_by_username(username):
    username = (username or "").strip()
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()
    return _row_to_user(row)


def find_user_by_id(user_id):
    with _conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _row_to_user(row)


def authenticate(username, password):
    user = find_user_by_username(username)
    if not user or not user.get("active", True):
        return None
    if not check_password_hash(user.get("password_hash", ""), password or ""):
        return None
    return _public_user(user)


def create_user(username, password, platforms=None, role="user", cities=None,
                 allow_pincodes=True):
    username = (username or "").strip()
    if not username or len(username) < 3:
        return None, "Username must be at least 3 characters."
    if not password or len(password) < 8:
        return None, "Password must be at least 8 characters."
    if role not in ("admin", "user"):
        role = "user"

    plats = _default_platforms(role == "admin")
    if role == "user" and isinstance(platforms, dict):
        plats = {p: bool(platforms.get(p, False)) for p in ALL_PLATFORMS}
        if not any(plats.values()):
            return None, "Enable at least one platform."

    cities_json = "[]" if role == "admin" else _cities_to_json(cities)
    allow_pin = True if role == "admin" else bool(allow_pincodes)
    # A restricted user must be able to reach at least one location.
    if role == "user" and not allow_pin and cities_json == "[]":
        return None, "Give access to at least one city, or allow pincode entry."

    try:
        with _conn() as conn:
            user_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO users
                (id, username, password_hash, role, platforms_json, cities_json,
                 allow_pincodes, active, must_change_password, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?)
                """,
                (
                    user_id,
                    username,
                    generate_password_hash(password),
                    role,
                    _platforms_to_json(plats),
                    cities_json,
                    1 if allow_pin else 0,
                    _now(),
                ),
            )
        return _public_user(find_user_by_id(user_id)), None
    except sqlite3.IntegrityError:
        return None, "Username already exists."


def update_user(user_id, *, platforms=None, active=None, password=None, role=None,
                cities=None, allow_pincodes=None):
    user = find_user_by_id(user_id)
    if not user:
        return None, "User not found."

    plats = user["platforms"]
    if platforms is not None:
        if not isinstance(platforms, dict):
            return None, "Invalid platforms payload."
        plats = {p: bool(platforms.get(p, False)) for p in ALL_PLATFORMS}
        if user["role"] != "admin" and not any(plats.values()):
            return None, "Enable at least one platform."

    new_active = user["active"] if active is None else bool(active)
    new_role = user["role"] if role is None else role
    if new_role not in ("admin", "user"):
        return None, "Invalid role."

    # Location access (ignored for admins, who are always unrestricted).
    new_cities_json = _cities_to_json(user.get("cities", []))
    if cities is not None:
        new_cities_json = _cities_to_json(cities)
    new_allow_pin = bool(user.get("allow_pincodes", True))
    if allow_pincodes is not None:
        new_allow_pin = bool(allow_pincodes)
    if new_role == "admin":
        new_cities_json, new_allow_pin = "[]", True
    elif not new_allow_pin and new_cities_json == "[]":
        return None, "Give access to at least one city, or allow pincode entry."

    with _conn() as conn:
        if user["role"] == "admin" and (not new_active or new_role != "admin"):
            other = conn.execute(
                """
                SELECT COUNT(*) AS c FROM users
                WHERE id != ? AND role = 'admin' AND active = 1
                """,
                (user_id,),
            ).fetchone()["c"]
            if other == 0:
                return None, "Cannot disable/demote the last admin."

        if new_role == "admin":
            plats = _default_platforms(True)

        pwd_hash = user["password_hash"]
        must_change = user.get("must_change_password", False)
        if password is not None:
            if len(password) < 8:
                return None, "Password must be at least 8 characters."
            pwd_hash = generate_password_hash(password)
            must_change = False

        conn.execute(
            """
            UPDATE users
            SET role = ?, platforms_json = ?, cities_json = ?, allow_pincodes = ?,
                active = ?, password_hash = ?, must_change_password = ?
            WHERE id = ?
            """,
            (
                new_role,
                _platforms_to_json(plats),
                new_cities_json,
                1 if new_allow_pin else 0,
                1 if new_active else 0,
                pwd_hash,
                1 if must_change else 0,
                user_id,
            ),
        )
    return _public_user(find_user_by_id(user_id)), None


def change_password(user_id, current_password, new_password):
    user = find_user_by_id(user_id)
    if not user:
        return None, "User not found."
    if not check_password_hash(user["password_hash"], current_password or ""):
        return None, "Current password is incorrect."
    if not new_password or len(new_password) < 8:
        return None, "New password must be at least 8 characters."
    if current_password == new_password:
        return None, "New password must be different."
    with _conn() as conn:
        conn.execute(
            """
            UPDATE users
            SET password_hash = ?, must_change_password = 0
            WHERE id = ?
            """,
            (generate_password_hash(new_password), user_id),
        )
    return _public_user(find_user_by_id(user_id)), None


def delete_user(user_id):
    user = find_user_by_id(user_id)
    if not user:
        return False, "User not found."
    with _conn() as conn:
        if user["role"] == "admin":
            other = conn.execute(
                """
                SELECT COUNT(*) AS c FROM users
                WHERE id != ? AND role = 'admin' AND active = 1
                """,
                (user_id,),
            ).fetchone()["c"]
            if other == 0:
                return False, "Cannot delete the last admin."
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return True, None


def log_search(user, platform, products, cities, pincode_count, total_checks):
    """Record a search request so admins can review user activity."""
    try:
        uname = (user or {}).get("username")
        uid = (user or {}).get("id")
        products_s = ", ".join(products) if isinstance(products, (list, tuple)) else str(products or "")
        cities_s = ", ".join(cities) if isinstance(cities, (list, tuple)) else str(cities or "")
        with _conn() as conn:
            conn.execute(
                """
                INSERT INTO searches
                (user_id, username, created_at, platform, products, cities, pincode_count, total_checks)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (uid, uname, _now(), platform, products_s[:500], cities_s[:500],
                 int(pincode_count or 0), int(total_checks or 0)),
            )
    except Exception:
        # Never let audit logging break a search.
        pass


def list_searches(limit=200):
    limit = max(1, min(int(limit or 200), 1000))
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM searches ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [
        {
            "id": r["id"],
            "username": r["username"],
            "created_at": r["created_at"],
            "platform": r["platform"],
            "products": r["products"],
            "cities": r["cities"],
            "pincode_count": r["pincode_count"],
            "total_checks": r["total_checks"],
        }
        for r in rows
    ]


# ── Token wallet ────────────────────────────────────────────────────────────
def get_balance(user_id):
    """Current token balance for a user (0 if unknown). Admins are unlimited but
    this returns their stored integer; callers gate on role for 'unlimited'."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT token_balance FROM users WHERE id = ?", (user_id,)).fetchone()
    return int(row["token_balance"]) if row and row["token_balance"] is not None else 0


def grant_tokens(user_id, amount, actor=None, note=None):
    """Admin action: add (or, with a negative amount, deduct) tokens. Balance is
    clamped at 0. Returns (new_balance, error)."""
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return None, "Amount must be a whole number."
    if amount == 0:
        return None, "Amount must be non-zero."
    user = find_user_by_id(user_id)
    if not user:
        return None, "User not found."
    if user["role"] == "admin":
        return None, "Admins have unlimited tokens — no need to top up."
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT token_balance FROM users WHERE id = ?", (user_id,)).fetchone()
        cur = int(row["token_balance"] or 0)
        new = max(0, cur + amount)
        applied = new - cur
        conn.execute(
            "UPDATE users SET token_balance = ? WHERE id = ?", (new, user_id))
        conn.execute(
            """INSERT INTO token_ledger
               (user_id, delta, reason, balance_after, actor, meta, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, applied, "grant" if amount > 0 else "adjust", new,
             actor, note, _now()),
        )
    return new, None


def consume_tokens(user_id, amount, reason="search", meta=None):
    """Atomically spend up to ``amount`` tokens. Never goes negative and never
    charges admins. Returns ``(consumed, balance_after)``.

    Uses BEGIN IMMEDIATE so concurrent web workers can't lose an update by
    reading the same balance before either writes.
    """
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return 0, 0
    if amount <= 0:
        return 0, get_balance(user_id)
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT token_balance, role FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return 0, 0
        if row["role"] == "admin":
            return 0, int(row["token_balance"] or 0)  # unlimited — never charged
        cur = int(row["token_balance"] or 0)
        consumed = min(amount, cur)
        if consumed <= 0:
            return 0, cur
        new = cur - consumed
        conn.execute(
            "UPDATE users SET token_balance = ? WHERE id = ?", (new, user_id))
        conn.execute(
            """INSERT INTO token_ledger
               (user_id, delta, reason, balance_after, actor, meta, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, -consumed, reason, new, None, meta, _now()),
        )
    return consumed, new


def list_ledger(user_id=None, limit=200):
    """Recent token movements, optionally filtered to one user (newest first)."""
    limit = max(1, min(int(limit or 200), 1000))
    with _conn() as conn:
        if user_id:
            rows = conn.execute(
                "SELECT * FROM token_ledger WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM token_ledger ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"], "user_id": r["user_id"], "delta": r["delta"],
            "reason": r["reason"], "balance_after": r["balance_after"],
            "actor": r["actor"], "meta": r["meta"], "created_at": r["created_at"],
        })
    return out


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    user = find_user_by_id(uid)
    if not user or not user.get("active", True):
        return None
    return _public_user(user)


def login_user(user_public):
    session.clear()
    session["user_id"] = user_public["id"]
    session["username"] = user_public["username"]
    session["role"] = user_public["role"]
    session.permanent = True


def logout_user():
    session.clear()


def allowed_platforms(user):
    if not user:
        return []
    if user.get("role") == "admin":
        return list(ALL_PLATFORMS)
    plats = user.get("platforms") or {}
    return [p for p in ALL_PLATFORMS if plats.get(p)]


def allowed_cities(user):
    """Return the set of city ids a user may access, or None if unrestricted.

    Admins and users with an empty allow-list are unrestricted (all cities).
    """
    if not user or user.get("role") == "admin":
        return None
    ids = [str(c).strip().lower() for c in (user.get("cities") or []) if str(c).strip()]
    return set(ids) if ids else None


def can_use_pincodes(user):
    if not user:
        return False
    if user.get("role") == "admin":
        return True
    return bool(user.get("allow_pincodes", True))


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify({"error": "Unauthorized"}), 401
        if user.get("must_change_password"):
            # allow only password change + me/logout while forced reset is pending
            from flask import request as flask_request
            path = flask_request.path
            if path not in ("/api/me", "/api/logout", "/api/change-password"):
                return jsonify({
                    "error": "Password change required",
                    "must_change_password": True,
                }), 403
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify({"error": "Unauthorized"}), 401
        if user.get("must_change_password"):
            return jsonify({
                "error": "Password change required",
                "must_change_password": True,
            }), 403
        if user.get("role") != "admin":
            return jsonify({"error": "Admin only"}), 403
        return fn(*args, **kwargs)
    return wrapper
