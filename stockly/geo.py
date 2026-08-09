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
from stockly import obs, places

log = logging.getLogger("stockly.geo")

_lock = threading.Lock()
_seeded = False

# Bumped when the labelling rules change. Rows below it keep their coordinates
# (those never go stale) and get a fresh label on next use, so an improvement
# reaches pincodes geocoded months ago without re-running the whole cache.
LABEL_VERSION = 3


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
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(geocache)")}
            if "place_full" not in cols:
                conn.execute("ALTER TABLE geocache ADD COLUMN place_full TEXT")
            if "label_v" not in cols:
                conn.execute(
                    "ALTER TABLE geocache ADD COLUMN label_v INTEGER NOT NULL DEFAULT 0")
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
            "SELECT lat, lon, place, place_full, label_v FROM geocache "
            "WHERE pincode = ?", (str(pincode),)
        ).fetchone()
    if not row or not row["lat"]:
        return None
    return {"lat": row["lat"], "lon": row["lon"], "place": row["place"] or "",
            "place_full": row["place_full"] or "",
            "label_v": row["label_v"] or 0}


def store(pincode, result) -> None:
    with _conn() as conn:
        conn.execute(
            """INSERT INTO geocache
                   (pincode, lat, lon, place, place_full, label_v, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(pincode) DO UPDATE SET
                   lat = excluded.lat, lon = excluded.lon,
                   place = excluded.place, place_full = excluded.place_full,
                   label_v = excluded.label_v, updated_at = excluded.updated_at""",
            (str(pincode), str(result.get("lat")) if result.get("lat") else None,
             str(result.get("lon")) if result.get("lon") else None,
             result.get("place") or "", result.get("place_full") or "",
             int(result.get("label_v") or 0), _now()),
        )


def _relabel(pincode, cached, session):
    """Replace an administrative label with the pincode's actual localities.

    Coordinates are left alone: they were correct when cached and are what the
    scrapers and distance sorting depend on. Only the human-facing label moves,
    which is why an old row costs one cheap India Post call rather than a full
    re-geocode behind Nominatim's one-request-per-second limit.
    """
    if session is None:
        from curl_cffi import requests as cffi_requests
        session = cffi_requests.Session()

    detail = places.fetch(pincode, session)
    if not detail:
        # Leave the old label in place; a worse label beats a failed check.
        obs.metrics.incr("relabel_failures")
        return cached

    updated = dict(cached)
    updated["place"] = places.label(detail)
    updated["place_full"] = places.full_label(detail)
    updated["label_v"] = LABEL_VERSION
    store(pincode, updated)
    obs.metrics.incr("relabelled")
    return updated


def resolve(pincode, session=None):
    """Return ``{lat, lon, place}`` for ``pincode``, geocoding on a cache miss.

    A failed lookup is *not* cached: geocoding failures are usually transient
    (rate limit, network), and caching them would poison the pincode forever.
    """
    cached = lookup(pincode)
    if cached:
        obs.metrics.incr("geocode_cache_hits")
        if cached.get("label_v", 0) < LABEL_VERSION:
            cached = _relabel(pincode, cached, session)
        return cached

    obs.metrics.incr("geocode_cache_misses")
    if session is None:
        from curl_cffi import requests as cffi_requests
        session = cffi_requests.Session()

    # persist=False: we own persistence, and the JSON file is not process-safe.
    result = bk.geocode_pincode(str(pincode), {}, session, persist=False)
    if result and result.get("lat"):
        detail = places.fetch(pincode, session)
        resolved = {
            "lat": result["lat"], "lon": result["lon"],
            "place": places.label(detail) or result.get("place") or "",
            "place_full": places.full_label(detail),
            "label_v": LABEL_VERSION if detail else 0,
        }
        store(pincode, resolved)
        return resolved

    log.warning("geocode_failed", extra={"pincode": pincode})
    obs.metrics.incr("geocode_failures")
    return None


def stale_label_pincodes(limit=None):
    """Cached pincodes still carrying a label from an older LABEL_VERSION."""
    sql = ("SELECT pincode FROM geocache WHERE label_v < ? AND lat IS NOT NULL "
           "ORDER BY pincode")
    params = [LABEL_VERSION]
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    with _conn() as conn:
        return [r["pincode"] for r in conn.execute(sql, params)]


def backfill_labels(session=None, pause=0.2, progress=None):
    """Relabel every stale pincode up front instead of waiting for a check.

    Labels otherwise improve lazily, one pincode per check, so a cache built
    under older rules takes several searches to fully catch up. Running this
    after a deploy makes the change visible immediately. Coordinates are never
    touched, so it is safe to re-run and safe to interrupt.
    """
    import time as _time

    if session is None:
        from curl_cffi import requests as cffi_requests
        session = cffi_requests.Session()

    pincodes = stale_label_pincodes()
    done = failed = 0
    for i, pin in enumerate(pincodes, 1):
        cached = lookup(pin)
        if not cached:
            continue
        before = cached.get("place")
        after = _relabel(pin, cached, session)
        if after.get("label_v") == LABEL_VERSION:
            done += 1
        else:
            failed += 1
        if progress:
            progress(i, len(pincodes), pin, before, after.get("place"))
        if pause:
            _time.sleep(pause)   # the API is free; don't hammer it
    return {"total": len(pincodes), "relabelled": done, "failed": failed}


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
