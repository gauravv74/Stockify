"""The store cache remembers where a retailer serves from, never what it has.

The distinction is the whole safety argument: a store id is a fact about the
retailer's footprint and is safe to reuse for a day, whereas stock moves by the
minute and is re-fetched on every check. A cache that quietly started holding
availability would make Stockly report stock it never observed.
"""
import importlib
from datetime import datetime, timedelta, timezone

import pytest

import config


@pytest.fixture
def stores(tmp_path, monkeypatch):
    """A stores module bound to a throwaway database."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    from stockly import stores as mod
    importlib.reload(mod)
    mod.init_db()
    return mod


class TestRoundTrip:
    def test_remembers_a_store(self, stores):
        stores.put("instamart", "18.5236,73.8807", "1403455")
        assert stores.get("instamart", "18.5236,73.8807") == "1403455"

    def test_unknown_location_is_a_miss(self, stores):
        assert stores.get("instamart", "0.0,0.0") is None

    def test_platforms_do_not_collide(self, stores):
        stores.put("instamart", "k", "store-a")
        stores.put("bigbasket", "k", "store-b")
        assert stores.get("instamart", "k") == "store-a"
        assert stores.get("bigbasket", "k") == "store-b"

    def test_relocating_a_store_overwrites(self, stores):
        stores.put("instamart", "k", "old")
        stores.put("instamart", "k", "new")
        assert stores.get("instamart", "k") == "new"

    def test_forget_forces_a_fresh_lookup(self, stores):
        stores.put("instamart", "k", "1403455")
        stores.forget("instamart", "k")
        assert stores.get("instamart", "k") is None

    def test_forgetting_an_absent_key_is_harmless(self, stores):
        stores.forget("instamart", "never-seen")

    def test_refuses_to_cache_nothing(self, stores):
        """A failed lookup must not be remembered as an answer."""
        stores.put("instamart", "k", None)
        stores.put("instamart", "k", "")
        assert stores.get("instamart", "k") is None


class TestExpiry:
    def test_entries_go_stale(self, stores, monkeypatch):
        stores.put("instamart", "k", "1403455")
        monkeypatch.setattr(
            stores, "_now",
            lambda: datetime.now(timezone.utc)
            + timedelta(seconds=config.STORE_CACHE_TTL_SEC + 60))
        assert stores.get("instamart", "k") is None, "a closed store would be cached forever"

    def test_fresh_entries_survive(self, stores, monkeypatch):
        stores.put("instamart", "k", "1403455")
        monkeypatch.setattr(
            stores, "_now",
            lambda: datetime.now(timezone.utc) + timedelta(seconds=60))
        assert stores.get("instamart", "k") == "1403455"

    def test_corrupt_timestamp_is_a_miss(self, stores):
        with stores._conn() as conn:
            conn.execute(
                "INSERT INTO store_cache (platform, key, store_id, updated_at) "
                "VALUES ('instamart', 'k', '1', 'not-a-date')")
        assert stores.get("instamart", "k") is None


class TestKeying:
    def test_same_coordinates_hit_the_same_row(self, stores):
        """Coordinates arrive as floats or strings depending on the caller."""
        assert stores.key_for(18.5236118, 73.8806684) == stores.key_for(
            "18.5236118", "73.8806684")

    def test_nearby_coordinates_are_one_location(self, stores):
        """~11m of rounding: the same pincode must not spread across rows."""
        assert stores.key_for(18.52361, 73.88066) == stores.key_for(18.52362, 73.88067)

    def test_different_areas_stay_distinct(self, stores):
        """Measured: Pune pincodes map to distinct Instamart stores, so keying
        must not merge them or one area's store would serve another's search."""
        assert stores.key_for(18.5236, 73.8807) != stores.key_for(18.5622, 73.9197)

    def test_unusable_coordinates_do_not_explode(self, stores):
        assert stores.key_for(None, None)


class TestDegradesQuietly:
    def test_an_unwritable_cache_is_slow_not_broken(self, stores, monkeypatch):
        """Losing the cache costs a round trip per check; it must never fail one."""
        import sqlite3

        def boom(*a, **k):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(stores, "_conn", boom)
        stores.put("instamart", "k", "1403455")
        assert stores.get("instamart", "k") is None
        stores.forget("instamart", "k")
