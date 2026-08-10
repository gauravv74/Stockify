"""Remembering a user's last selected platform.

The rule that matters is the negative one: Blinkit is the default *only* for a
user who has never chosen. Everyone else gets what they last picked, which
means the preference has to survive a fresh login, not just a page reload.

The other half is that a preference is untrusted input written straight from a
browser, so it cannot be used to widen access or to smuggle arbitrary state
into the user record.
"""

from __future__ import annotations

import json

import pytest

import auth


class TestRoundTrip:
    def test_a_new_user_has_no_remembered_platform(self, make_user):
        """This absence is what makes the Blinkit default apply to them."""
        user = make_user()
        assert user["prefs"] == {}

    def test_a_saved_platform_comes_back(self, make_user):
        user = make_user()
        auth.save_prefs(user["id"], {"platform": "zepto"})
        assert auth.get_prefs(user["id"])["platform"] == "zepto"

    def test_saving_again_replaces_the_choice(self, make_user):
        user = make_user()
        auth.save_prefs(user["id"], {"platform": "zepto"})
        auth.save_prefs(user["id"], {"platform": "bigbasket"})
        assert auth.get_prefs(user["id"])["platform"] == "bigbasket"

    def test_all_platforms_is_a_choice_worth_remembering(self, make_user):
        user = make_user(platforms=["blinkit", "zepto"])
        auth.save_prefs(user["id"], {"platform": "all"})
        assert auth.get_prefs(user["id"])["platform"] == "all"


class TestItCannotWidenAccess:
    def test_a_platform_the_user_cannot_use_is_refused(self, make_user):
        """Otherwise a revoked platform would be restored on every login and
        fail the access check each time, stranding the user on a dead tab."""
        user = make_user(platforms=["blinkit"])
        auth.save_prefs(user["id"], {"platform": "croma"})
        assert auth.get_prefs(user["id"]) == {}

    def test_all_is_refused_when_only_one_platform_is_allowed(self, make_user):
        user = make_user(platforms=["blinkit"])
        auth.save_prefs(user["id"], {"platform": "all"})
        assert auth.get_prefs(user["id"]) == {}

    def test_a_revoked_platform_stays_on_file_but_is_not_restored(self, make_user):
        """Losing access should not erase the choice: if access comes back, so
        should the platform the user actually wanted."""
        user = make_user(platforms=["blinkit", "zepto"])
        auth.save_prefs(user["id"], {"platform": "zepto"})
        auth.update_user(user["id"], platforms={"blinkit": True})

        stored = auth.get_prefs(user["id"])
        assert stored["platform"] == "zepto"
        assert "zepto" not in auth.allowed_platforms(auth.find_user_by_id(user["id"]))


class TestItRejectsJunk:
    def test_unknown_keys_are_dropped(self, make_user):
        user = make_user()
        auth.save_prefs(user["id"], {"platform": "zepto", "role": "admin",
                                     "token_balance": 99999})
        assert auth.get_prefs(user["id"]) == {"platform": "zepto"}

    def test_a_known_key_survives_a_write_that_does_not_mention_it(self, make_user):
        """Merge, not replace: a client that knows about one preference must
        not wipe one it has never heard of."""
        user = make_user()
        auth.save_prefs(user["id"], {"platform": "zepto"})
        auth.save_prefs(user["id"], {"unknown": "x"})
        assert auth.get_prefs(user["id"])["platform"] == "zepto"

    @pytest.mark.parametrize("payload", [None, [], "zepto", 7, {}])
    def test_junk_payloads_change_nothing(self, make_user, payload):
        user = make_user()
        auth.save_prefs(user["id"], {"platform": "zepto"})
        auth.save_prefs(user["id"], payload)
        assert auth.get_prefs(user["id"])["platform"] == "zepto"

    def test_corrupt_stored_json_reads_as_no_preference(self, make_user, db):
        """A user whose row is unreadable falls back to the default rather
        than failing their login."""
        user = make_user()
        with auth._conn() as conn:
            conn.execute("UPDATE users SET prefs_json = ? WHERE id = ?",
                         ("{not json", user["id"]))
        assert auth.get_prefs(user["id"]) == {}

    def test_an_unknown_user_is_a_no_op(self, db):
        assert auth.save_prefs("nobody", {"platform": "zepto"}) == {}
        assert auth.save_prefs(None, {"platform": "zepto"}) == {}


class TestItReachesTheClient:
    def test_every_route_that_starts_a_session_returns_prefs(self, make_user):
        """The restore happens on login, so a payload missing prefs there
        would silently reset the user to Blinkit on every sign-in."""
        import app as app_module

        user = make_user(password="password123")
        auth.save_prefs(user["id"], {"platform": "bigbasket"})
        client = app_module.app.test_client()

        login = client.post("/api/login", json={"username": user["username"],
                                                "password": "password123"})
        assert login.get_json()["prefs"] == {"platform": "bigbasket"}

        me = client.get("/api/me")
        assert me.get_json()["prefs"] == {"platform": "bigbasket"}

    def test_the_save_endpoint_persists_and_echoes(self, make_user):
        import app as app_module

        user = make_user(password="password123")
        client = app_module.app.test_client()
        client.post("/api/login", json={"username": user["username"],
                                        "password": "password123"})

        resp = client.post("/api/prefs", json={"platform": "jiomart"})
        assert resp.get_json()["prefs"] == {"platform": "jiomart"}
        assert client.get("/api/me").get_json()["prefs"]["platform"] == "jiomart"

    def test_saving_requires_a_session(self, db):
        import app as app_module

        client = app_module.app.test_client()
        assert client.post("/api/prefs", json={"platform": "zepto"}).status_code == 401


class TestMigration:
    def test_existing_rows_get_an_empty_preference(self, db):
        """Users created before this column existed must not be broken by it,
        and must keep the Blinkit default until they choose."""
        with auth._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
            assert "prefs_json" in cols
            row = conn.execute("SELECT prefs_json FROM users LIMIT 1").fetchone()
        assert json.loads(row["prefs_json"]) == {}
