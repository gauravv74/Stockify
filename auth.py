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

ALL_PLATFORMS = ("blinkit", "instamart", "zepto", "bigbasket")
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


def _row_to_user(row):
    if not row:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "password_hash": row["password_hash"],
        "role": row["role"],
        "platforms": _platforms_from_json(row["platforms_json"]),
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


def create_user(username, password, platforms=None, role="user"):
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

    try:
        with _conn() as conn:
            user_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO users
                (id, username, password_hash, role, platforms_json, active, must_change_password, created_at)
                VALUES (?, ?, ?, ?, ?, 1, 0, ?)
                """,
                (
                    user_id,
                    username,
                    generate_password_hash(password),
                    role,
                    _platforms_to_json(plats),
                    _now(),
                ),
            )
        return _public_user(find_user_by_id(user_id)), None
    except sqlite3.IntegrityError:
        return None, "Username already exists."


def update_user(user_id, *, platforms=None, active=None, password=None, role=None):
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
            SET role = ?, platforms_json = ?, active = ?, password_hash = ?, must_change_password = ?
            WHERE id = ?
            """,
            (
                new_role,
                _platforms_to_json(plats),
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
