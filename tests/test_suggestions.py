"""Ranking a user's own search history into product shortcuts.

``searches.products`` stores the raw comma-separated text the user typed, so the
interesting cases are all about splitting and normalising that free text without
merging genuinely different products or splitting one product in two.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def history(db, make_user):
    """Log searches as the app does, and return the users who ran them."""
    import auth

    alice = make_user(username="alice")
    bob = make_user(username="bob")

    def log(user, products, times=1):
        for _ in range(times):
            auth.log_search(user, "blinkit", products, ["pune"], 5, 5)

    return {"alice": alice, "bob": bob, "log": log, "auth": auth}


class TestRanking:
    def test_orders_by_how_often_a_product_is_searched(self, history):
        log, alice = history["log"], history["alice"]
        log(alice, ["amul milk"], times=3)
        log(alice, ["iphone 17"], times=5)
        log(alice, ["bread"], times=1)

        top = history["auth"].top_products(alice["id"])
        assert [p["product"] for p in top] == ["iphone 17", "amul milk", "bread"]
        assert [p["count"] for p in top] == [5, 3, 1]

    def test_counts_each_product_in_a_multi_product_search(self, history):
        log, alice = history["log"], history["alice"]
        log(alice, ["amul milk", "bread", "eggs"])
        log(alice, ["bread"])

        counts = {p["product"]: p["count"] for p in history["auth"].top_products(alice["id"])}
        assert counts == {"bread": 2, "amul milk": 1, "eggs": 1}

    def test_returns_at_most_the_requested_number(self, history):
        log, alice = history["log"], history["alice"]
        for name in ("a", "b", "c", "d", "e", "f"):
            log(alice, [name])
        assert len(history["auth"].top_products(alice["id"], limit=4)) == 4

    def test_no_history_yields_no_suggestions(self, history):
        assert history["auth"].top_products(history["alice"]["id"]) == []


class TestNormalisation:
    def test_case_and_spacing_differences_are_one_product(self, history):
        log, alice = history["log"], history["alice"]
        log(alice, ["iPhone 17"])
        log(alice, ["iphone 17"])
        log(alice, ["  IPHONE   17 "])

        top = history["auth"].top_products(alice["id"])
        assert len(top) == 1
        assert top[0]["count"] == 3

    def test_shows_the_most_recent_spelling(self, history):
        log, alice = history["log"], history["alice"]
        log(alice, ["IPHONE 17"])
        log(alice, ["iPhone 17"])
        assert history["auth"].top_products(alice["id"])[0]["product"] == "iPhone 17"

    def test_blank_entries_are_ignored(self, history):
        import auth
        auth.log_search(history["alice"], "blinkit", "milk,, ,  ,bread",
                        ["pune"], 5, 5)
        products = [p["product"] for p in auth.top_products(history["alice"]["id"])]
        assert sorted(products) == ["bread", "milk"]


class TestScoping:
    def test_a_user_only_sees_their_own_searches(self, history):
        log = history["log"]
        log(history["alice"], ["amul milk"], times=3)
        log(history["bob"], ["protein powder"], times=9)

        alice_top = [p["product"] for p in history["auth"].top_products(history["alice"]["id"])]
        assert alice_top == ["amul milk"]
        assert "protein powder" not in alice_top

    def test_omitting_a_user_ranks_across_everyone(self, history):
        log = history["log"]
        log(history["alice"], ["amul milk"], times=3)
        log(history["bob"], ["protein powder"], times=9)

        everyone = [p["product"] for p in history["auth"].top_products()]
        assert everyone[0] == "protein powder"


class TestEndpoint:
    def test_endpoint_returns_the_callers_own_top_products(self, db, make_user):
        import app as app_module
        import auth

        user = make_user(username="carol", password="password123")
        for _ in range(2):
            auth.log_search(user, "blinkit", ["amul milk"], ["pune"], 5, 5)

        app_module.app.config["TESTING"] = True
        with app_module.app.test_client() as client:
            client.post("/api/login", json={"username": "carol",
                                            "password": "password123"})
            resp = client.get("/api/products/top?limit=4")
            assert resp.status_code == 200
            assert resp.get_json()["products"] == [{"product": "amul milk", "count": 2}]

    def test_endpoint_requires_a_session(self, db):
        import app as app_module

        app_module.app.config["TESTING"] = True
        with app_module.app.test_client() as client:
            assert client.get("/api/products/top").status_code == 401
