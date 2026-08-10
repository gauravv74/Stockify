"""The properties that make BigBasket and Instamart fast, and the one that
makes the speedup safe.

Reusing a session across checks is the change here that can produce a *wrong*
answer rather than a slow one: BigBasket expresses location entirely through
cookies, so a leftover cookie means one pincode's results reported under
another's name. TestSessionIsolation pins that down; the rest pin down the work
we stopped doing, since a silent return to eager fetching would be invisible
apart from the latency.
"""
import threading

import pytest

import bigbasket_check as bb
import config


class FakeCookies:
    """Enough of curl_cffi's cookie jar to observe what a check leaves behind."""

    def __init__(self):
        self.store = {}

    def set(self, name, value, domain=None):
        self.store[name] = value

    def delete(self, name, domain=None):
        self.store.pop(name, None)


class FakeSession:
    def __init__(self):
        self.cookies = FakeCookies()
        self.proxies = None
        self.gets = []

    def get(self, url, **kw):
        self.gets.append(url)
        raise AssertionError("unexpected network call in a unit test")


@pytest.fixture
def client(monkeypatch):
    """A BigBasket client whose sessions never touch the network."""
    c = bb.BigBasket()
    monkeypatch.setattr(c, "_seed", lambda: FakeSession())
    return c


class TestSessionIsolation:
    """A reused session must carry no trace of the previous location."""

    # _bb_locSrc records *how* the location was picked ("gps"), not where it is,
    # so it is the one cookie that legitimately survives a location change.
    LOCATION_BEARING = ("_bb_lat_long", "_bb_addressinfo", "_bb_pin_code")

    def test_every_location_cookie_is_rewritten(self, client):
        s = client._session()
        client._set_location(s, "18.52", "73.85", "411001")
        first = dict(s.cookies.store)
        client._set_location(s, "19.07", "72.87", "400001")
        second = dict(s.cookies.store)

        for name in self.LOCATION_BEARING:
            assert second[name] != first[name], (
                f"{name} survived a location change unchanged; the second "
                f"pincode would be searched with the first one's {name}")

    def test_serving_area_is_cleared_not_just_overwritten(self, client):
        """The leak that clearing exists to prevent.

        _resolve_sa only writes the serving-area cookies when it resolves one.
        A location BigBasket does not serve resolves nothing, so without an
        explicit clear the session keeps the previous location's stores and
        happily searches them.
        """
        s = client._session()
        client._set_location(s, "18.52", "73.85", "411001")
        client._set_cookie(s, "_bb_sa_ids", "25207,25208")
        client._set_cookie(s, "_bb_cda_sa_info", "v2.cda_sa.10.25207,25208")

        client._set_location(s, "34.15", "77.57", "194101")

        assert "_bb_sa_ids" not in s.cookies.store
        assert "_bb_cda_sa_info" not in s.cookies.store

    def test_location_cookie_list_covers_what_set_location_writes(self, client):
        """Guards the constant against drift.

        Adding a location cookie without listing it in LOCATION_COOKIES is
        exactly how a leak gets reintroduced, and nothing else would catch it.
        """
        s = client._session()
        client._set_location(s, "18.52", "73.85", "411001")
        assert set(s.cookies.store) <= set(bb.BigBasket.LOCATION_COOKIES)

    def test_threads_do_not_share_a_session(self, client):
        # Keep the objects alive: once a thread exits, CPython is free to reuse
        # its session's address, and comparing ids would report a false share.
        seen = []
        barrier = threading.Barrier(4)

        def grab():
            s = client._session()
            barrier.wait()
            seen.append(s)

        threads = [threading.Thread(target=grab) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len({id(s) for s in seen}) == 4, "threads shared one cookie jar"

    def test_session_is_reused_within_a_thread(self, client):
        assert client._session() is client._session()

    def test_a_failed_check_does_not_poison_the_thread(self, client, monkeypatch):
        """A half-dead session must not be reused for every later check."""
        first = client._session()
        monkeypatch.setattr(client, "_query", lambda *a, **k: 1 / 0)

        out = client.check("18.52", "73.85", "milk", "411001")

        assert out["serviceable"] is None
        assert client._session() is not first


class TestMarketplaceIsLazy:
    """The national catalog costs a second session plus a search, and is
    discarded whenever the local express store stocks the query."""

    def test_check_does_not_fetch_the_marketplace(self, client, monkeypatch):
        calls = []
        monkeypatch.setattr(client, "_set_location", lambda *a: None)
        monkeypatch.setattr(client, "_resolve_sa", lambda s: ([25207], [{"eta": "30 min"}]))
        monkeypatch.setattr(client, "_search", lambda s, q: [{"name": "Amul Milk"}])
        monkeypatch.setattr(client, "_search_marketplace",
                            lambda q, p: calls.append(q) or [])

        client.check("18.52", "73.85", "milk", "411001")

        assert calls == [], "marketplace fetched during a check that never needs it"

    def test_match_row_skips_it_when_express_matches(self, monkeypatch):
        calls = []
        monkeypatch.setattr(bb.client, "marketplace_items",
                            lambda q, p: calls.append(q) or [])
        result = {"serviceable": True, "sa": [25207], "eta": "30 min",
                  "pincode": "411001",
                  "items": [{"name": "Amul Gold Milk 500 ml", "variant": "500 ml",
                             "brand": "Amul", "price": 33.0, "mrp": 33.0,
                             "inStock": True, "source": "express"}]}

        row = bb.match_row("amul gold milk", result)

        assert row["status"] == "available"
        assert calls == []

    def test_match_row_falls_back_when_express_misses(self, monkeypatch):
        calls = []

        def fake_market(query, pincode):
            calls.append((query, pincode))
            return [{"name": "iPhone 17 128GB", "variant": "128 GB",
                     "brand": "Apple", "price": 79900.0, "mrp": 79900.0,
                     "inStock": True, "product_id": 4242, "source": "marketplace"}]

        monkeypatch.setattr(bb.client, "marketplace_items", fake_market)
        monkeypatch.setattr(bb.client, "_pd_avail_status", lambda *a: "001")
        result = {"serviceable": True, "sa": [25207], "eta": "", "pincode": "411001",
                  "lat": "18.52", "lon": "73.85", "items": []}

        row = bb.match_row("iphone 17", result)

        assert calls == [("iphone 17", "411001")], "fallback catalog was not consulted"
        assert row["status"] == "available"

    def test_a_broken_marketplace_is_not_found_not_an_error(self, monkeypatch):
        """The fallback is best-effort; losing it must not turn a real 'we
        looked and it isn't stocked' into an infrastructure failure."""
        monkeypatch.setattr(bb.client, "_search_marketplace",
                            lambda q, p: (_ for _ in ()).throw(RuntimeError("boom")))
        result = {"serviceable": True, "sa": [25207], "eta": "", "pincode": "411001",
                  "lat": "18.52", "lon": "73.85", "items": []}

        assert bb.match_row("iphone 17", result)["status"] == "not_found"


class TestConcurrencyIsBounded:
    def test_bigbasket_admits_several_checks_at_once(self):
        """The old mutex made this exactly 1, which is what made a 53-pincode
        BigBasket search take over a minute of pure serialisation."""
        assert config.platform_slots("bigbasket") > 1

    def test_slots_are_still_capped(self):
        for platform in config.PLATFORM_CONCURRENCY:
            assert 1 <= config.platform_slots(platform) <= 8, (
                f"{platform} would run unbounded against a rate-limited retailer")

    def test_semaphore_actually_blocks_the_surplus(self, monkeypatch):
        """A BoundedSemaphore, not a counter nobody waits on."""
        monkeypatch.setitem(config.PLATFORM_CONCURRENCY, "bigbasket", 2)
        c = bb.BigBasket()
        assert c._slots.acquire(blocking=False)
        assert c._slots.acquire(blocking=False)
        assert not c._slots.acquire(blocking=False)
