#!/usr/bin/env python3
"""Process-safe pincode → coordinates resolution.

The original cache was a JSON file that every caller loaded wholesale, mutated
and rewrote (``blinkit_check.save_cache``). With one web process that was fine;
with several worker containers it loses updates and can truncate the file
mid-write. Geocodes are permanent facts, so they belong in the database.

The legacy JSON is imported once as a warm-start seed and then left alone.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

import blinkit_check as bk
import config
from stockly import obs

log = logging.getLogger("stockly.geo")

_lock = threading.Lock()
_seeded = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    global _seeded
    with _lock:
        with _conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS geocache (
                    pincode TEXT PRIMARY KEY,
                    lat TEXT,
                    lon TEXT,
                    place TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
        if not _seeded:
            _seed_from_json()
            _seeded = True


def _seed_from_json() -> None:
    """One-off import of the legacy JSON cache so we don't re-geocode everything."""
    path = getattr(bk, "GEO_CACHE", None)
    if not path:
        return
    try:
        with open(path) as fh:
            legacy = json.load(fh)
    except (OSError, ValueError):
        return
    if not isinstance(legacy, dict) or not legacy:
        return

    imported = 0
    with _conn() as conn:
        for pin, entry in legacy.items():
            if not isinstance(entry, dict) or not entry.get("lat"):
                continue
            cur = conn.execute(
                """INSERT INTO geocache (pincode, lat, lon, place, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(pincode) DO NOTHING""",
                (str(pin), str(entry.get("lat")), str(entry.get("lon")),
                 entry.get("place") or "", _now()),
            )
            imported += cur.rowcount or 0
    if imported:
        log.info("geocache seeded", extra={"imported": imported})


def lookup(pincode):
    """Cached coordinates for ``pincode``, or None. Never hits the network."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT lat, lon, place FROM geocache WHERE pincode = ?", (str(pincode),)
        ).fetchone()
    if not row or not row["lat"]:
        return None
    return {"lat": row["lat"], "lon": row["lon"], "place": row["place"] or ""}


def store(pincode, result) -> None:
    with _conn() as conn:
        conn.execute(
            """INSERT INTO geocache (pincode, lat, lon, place, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(pincode) DO UPDATE SET
                   lat = excluded.lat, lon = excluded.lon,
                   place = excluded.place, updated_at = excluded.updated_at""",
            (str(pincode), str(result.get("lat")) if result.get("lat") else None,
             str(result.get("lon")) if result.get("lon") else None,
             result.get("place") or "", _now()),
        )


def resolve(pincode, session=None):
    """Return ``{lat, lon, place}`` for ``pincode``, geocoding on a cache miss.

    A failed lookup is *not* cached: geocoding failures are usually transient
    (rate limit, network), and caching them would poison the pincode forever.
    """
    cached = lookup(pincode)
    if cached:
        obs.metrics.incr("geocode_cache_hits")
        return cached

    obs.metrics.incr("geocode_cache_misses")
    if session is None:
        from curl_cffi import requests as cffi_requests
        session = cffi_requests.Session()

    # persist=False: we own persistence, and the JSON file is not process-safe.
    result = bk.geocode_pincode(str(pincode), {}, session, persist=False)
    if result and result.get("lat"):
        store(pincode, result)
        return {"lat": result["lat"], "lon": result["lon"],
                "place": result.get("place") or ""}

    log.warning("geocode_failed", extra={"pincode": pincode})
    obs.metrics.incr("geocode_failures")
    return None


def preloaded(pincodes):
    """Bulk cached-only lookup, for nearest-first ordering without network I/O."""
    pincodes = [str(p) for p in pincodes]
    if not pincodes:
        return {}
    out = {}
    with _conn() as conn:
        # Chunked to stay well under SQLite's variable limit.
        for i in range(0, len(pincodes), 500):
            chunk = pincodes[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT pincode, lat, lon, place FROM geocache "
                f"WHERE pincode IN ({placeholders})", chunk
            ):
                if row["lat"]:
                    out[row["pincode"]] = {
                        "lat": row["lat"], "lon": row["lon"],
                        "place": row["place"] or "",
                    }
    return out
