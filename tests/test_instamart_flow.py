"""Instamart's check flow, with the browser replaced by fakes.

Two behaviours matter here. The store cache must remove the browser round trip
from the warm path without ever removing the search — the cache holds where to
look, never what was found. And the client must stop being a mutex: it now
serves several checks from one Chromium, which is only sound because the warm
path touches no Playwright object at all.
"""
import importlib

import pytest

import config

pytest.importorskip("playwright")
pytest.importorskip("curl_cffi")


@pytest.fixture
def sw(tmp_path, monkeypatch):
    """swiggy_check wired to a throwaway store cache, with no browser."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    from stockly import stores as stores_mod
    importlib.reload(stores_mod)
    stores_mod.init_db()

    import swiggy_check as mod
    importlib.reload(mod)
    monkeypatch.setattr(mod, "stores", stores_mod)
    return mod


@pytest.fixture
def client(sw):
    c = sw.SwiggyInstamart()
    calls = {"ensure": 0, "lookup": 0, "search": [], "prime": 0}

    async def fake_ensure():
        calls["ensure"] += 1

    async def fake_lookup(lat, lon):
        calls["lookup"] += 1
        return "1403455", True

    async def fake_search(store, query):
        calls["search"].append((store, query))
        return {"status": 200, "items": [{"name": "Amul Milk", "inStock": True}]}

    async def fake_prime(verify=True):
        calls["prime"] += 1
        return True

    c._ensure = fake_ensure
    c._store_lookup = fake_lookup
    c._search_cffi = fake_search
    c._prime = fake_prime
    c.calls = calls
    return c


class TestStoreCache:
    def test_first_check_resolves_and_remembers(self, client, sw):
        out = client.check(18.5236, 73.8807, "milk")

        assert out["serviceable"] is True
        assert client.calls["lookup"] == 1
        assert sw.stores.get("instamart", sw.stores.key_for(18.5236, 73.8807)) == "1403455"

    def test_second_check_skips_the_browser_lookup(self, client):
        client.check(18.5236, 73.8807, "milk")
        client.check(18.5236, 73.8807, "bread")

        assert client.calls["lookup"] == 1, (
            "the store was resolved again; caching it is the whole speedup")

    def test_but_still_runs_the_search(self, client):
        """Caching the store must never cache the answer."""
        client.check(18.5236, 73.8807, "milk")
        client.check(18.5236, 73.8807, "milk")

        assert client.calls["search"] == [("1403455", "milk"), ("1403455", "milk")], (
            "a second check reused a previous result instead of asking Swiggy")

    def test_a_different_location_resolves_its_own_store(self, client):
        client.check(18.5236, 73.8807, "milk")
        client.check(18.5622, 73.9197, "milk")

        assert client.calls["lookup"] == 2

    def test_an_unserviceable_location_is_not_cached(self, client, sw):
        async def no_store(lat, lon):
            return None, True

        client._store_lookup = no_store
        out = client.check(1.0, 1.0, "milk")

        assert out["serviceable"] is False
        assert sw.stores.get("instamart", sw.stores.key_for(1.0, 1.0)) is None

    def test_a_cached_store_that_fails_is_forgotten(self, client, sw):
        """Otherwise a closed store would fail every check until it expired."""
        key = sw.stores.key_for(18.5236, 73.8807)
        sw.stores.put("instamart", key, "stale-store")

        async def unrecoverable(lat, lon):
            return None, False

        client._store_lookup = unrecoverable

        async def empty(store, query):
            return {"status": 200, "items": []}

        client._search_cffi = empty
        out = client.check(18.5236, 73.8807, "milk")

        assert out["serviceable"] is None
        assert sw.stores.get("instamart", key) is None

    def test_a_stale_session_reprimes_and_retries(self, client):
        attempts = []

        async def flaky(store, query):
            attempts.append(store)
            if len(attempts) == 1:
                return {"status": 200, "items": []}      # looks like a dead session
            return {"status": 200, "items": [{"name": "Amul Milk"}]}

        client._search_cffi = flaky
        out = client.check(18.5236, 73.8807, "milk")

        assert client.calls["prime"] == 1
        assert len(attempts) == 2
        assert out["items"]


class TestConcurrency:
    def test_checks_are_no_longer_serialised(self, client):
        assert config.platform_slots("instamart") > 1, (
            "Instamart ran one check at a time; 53 pincodes meant 53 in a row")

    def test_slots_are_bounded(self, client):
        limit = config.platform_slots("instamart")
        for _ in range(limit):
            assert client._slots.acquire(blocking=False)
        assert not client._slots.acquire(blocking=False), (
            "unbounded concurrency against a WAF-protected retailer")

    def test_warm_path_touches_no_playwright_object(self, client):
        """What makes one browser safe to share.

        The warm path must not reach into the page or context; if it does, it
        needs the lock back and the concurrency above becomes a race.
        """
        client.check(18.5236, 73.8807, "milk")          # warms the store cache
        client._ctx = client._page = None               # any access now raises
        client.check(18.5236, 73.8807, "bread")

        assert client.calls["search"][-1] == ("1403455", "bread")

    def test_search_uses_the_cookie_snapshot(self, sw):
        c = sw.SwiggyInstamart()
        c._cookies = {"aws-waf-token": "abc", "sid": "xyz"}
        sent = {}

        class FakeResp:
            status_code = 200

            @staticmethod
            def json():
                return {}

        def fake_post(url, **kw):
            sent.update(kw)
            return FakeResp()

        monkey = pytest.MonkeyPatch()
        monkey.setattr(sw.cffi_requests, "post", fake_post)
        monkey.setattr(sw, "extract_items", lambda j: [])
        try:
            c._run(c._search_cffi("1403455", "milk"))
        finally:
            monkey.undo()

        assert sent["cookies"] == {"aws-waf-token": "abc", "sid": "xyz"}
