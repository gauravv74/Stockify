"""HTTP contract for the search API.

The web and mobile clients depend on these shapes, so the point of these tests
is that the queue migration is invisible from the outside — apart from the new
`queued` status and the 202.
"""

from __future__ import annotations

import pytest

import config
import jobs
from stockly import broker as broker_mod


@pytest.fixture
def client(db, monkeypatch):
    import app as app_module

    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture
def logged_in(client, make_user):
    user = make_user(password="password123", tokens=1000,
                     platforms=["blinkit"], allow_pincodes=True)
    resp = client.post("/api/login", json={"username": user["username"],
                                           "password": "password123"})
    assert resp.status_code == 200, resp.get_json()
    return user


@pytest.fixture
def no_broker(monkeypatch):
    """Force the 'Redis is down' path so no real work is enqueued."""
    monkeypatch.setattr(broker_mod, "is_live", lambda: False)


class TestAuth:
    def test_start_requires_login(self, client):
        resp = client.post("/api/check/start", json={"pincodes": "411001"})
        assert resp.status_code in (401, 403)

    def test_poll_requires_login(self, client):
        assert client.get("/api/check/poll?job_id=x").status_code in (401, 403)

    def test_cannot_poll_another_users_job(self, client, logged_in, make_user):
        stranger = make_user()
        job_id = jobs.create_job(stranger["id"], {}, 1)
        # Scoped by owner, so it must look absent rather than forbidden.
        assert client.get(f"/api/check/poll?job_id={job_id}").status_code == 404

    def test_cannot_cancel_another_users_job(self, client, logged_in, make_user):
        stranger = make_user()
        job_id = jobs.create_job(stranger["id"], {}, 1)
        client.post("/api/check/cancel", json={"job_id": job_id})
        assert jobs.is_canceled(job_id) is False


class TestStart:
    def test_returns_202_with_job_id_and_total(self, client, logged_in, no_broker,
                                               monkeypatch):
        # Inline fallback would start a real scrape; suppress it.
        import app as app_module
        monkeypatch.setattr(app_module.threading, "Thread",
                            lambda *a, **kw: type("T", (), {"start": lambda s: None})())

        resp = client.post("/api/check/start", json={
            "pincodes": "411001,411002", "products": ["iphone 17"],
            "platform": "blinkit",
        })
        assert resp.status_code == 202
        body = resp.get_json()
        assert body["job_id"]
        assert body["total"] == 2
        assert body["status"] in (jobs.QUEUED, jobs.RUNNING)

    def test_rejects_search_over_the_size_limit(self, client, logged_in, monkeypatch):
        monkeypatch.setattr(config, "MAX_SEARCH_CHECKS", 1)
        resp = client.post("/api/check/start", json={
            "pincodes": "411001,411002", "products": ["iphone 17"],
            "platform": "blinkit",
        })
        assert resp.status_code == 409
        body = resp.get_json()
        assert body["error"] == "search_too_large"
        assert "message" in body, "the user must be told how to fix it"

    def test_rejects_when_too_many_jobs_are_active(self, client, logged_in,
                                                  monkeypatch):
        monkeypatch.setattr(config, "MAX_ACTIVE_JOBS_PER_USER", 1)
        jobs.create_job(logged_in["id"], {}, 5)

        resp = client.post("/api/check/start", json={
            "pincodes": "411001", "products": ["iphone 17"], "platform": "blinkit",
        })
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "too_many_active_jobs"

    def test_empty_wallet_is_blocked_before_any_work(self, client, make_user):
        user = make_user(password="password123", tokens=0, platforms=["blinkit"])
        client.post("/api/login", json={"username": user["username"],
                                        "password": "password123"})
        resp = client.post("/api/check/start", json={
            "pincodes": "411001", "products": ["iphone 17"], "platform": "blinkit",
        })
        assert resp.status_code == 402
        assert resp.get_json()["code"] == "tokens_exhausted"

    def test_platform_grants_are_enforced_server_side(self, client, make_user):
        """A client asking for a platform it wasn't granted must not get it."""
        user = make_user(password="password123", tokens=100, platforms=["blinkit"])
        client.post("/api/login", json={"username": user["username"],
                                        "password": "password123"})
        resp = client.post("/api/check/start", json={
            "pincodes": "411001", "products": ["iphone 17"], "platform": "zepto",
        })
        if resp.status_code == 202:
            plan = jobs.get_plan(resp.get_json()["job_id"])
            assert "zepto" not in plan["platforms"]
        else:
            assert resp.status_code == 403


class TestPoll:
    def test_shape_is_unchanged(self, client, logged_in):
        job_id = jobs.create_job(logged_in["id"], {"total": 1}, 1)
        body = client.get(f"/api/check/poll?job_id={job_id}&cursor=0").get_json()
        for field in ("status", "total", "meta", "events", "cursor", "balance"):
            assert field in body

    def test_never_leaks_the_internal_plan(self, client, logged_in):
        job_id = jobs.create_job(logged_in["id"], {"total": 1}, 1,
                                 plan={"pincodes": ["411001"] * 500})
        body = client.get(f"/api/check/poll?job_id={job_id}").get_json()
        assert "plan" not in body
        assert "plan" not in (body.get("meta") or {})

    def test_reports_queued_before_a_worker_picks_it_up(self, client, logged_in):
        job_id = jobs.create_job(logged_in["id"], {}, 5)
        body = client.get(f"/api/check/poll?job_id={job_id}").get_json()
        assert body["status"] == jobs.QUEUED

    def test_cursor_advances_and_does_not_repeat(self, client, logged_in):
        job_id = jobs.create_job(logged_in["id"], {}, 2)
        jobs.record_result(job_id, "c1", {"type": "result", "status": "available"})
        jobs.record_result(job_id, "c2", {"type": "result", "status": "out_of_stock"})

        first = client.get(f"/api/check/poll?job_id={job_id}&cursor=0").get_json()
        assert len(first["events"]) == 2

        second = client.get(
            f"/api/check/poll?job_id={job_id}&cursor={first['cursor']}").get_json()
        assert second["events"] == [], "polling from the cursor must be idempotent"

    def test_unknown_job_is_404(self, client, logged_in):
        assert client.get("/api/check/poll?job_id=nope").status_code == 404


class TestCancel:
    def test_marks_the_job_canceling(self, client, logged_in):
        job_id = jobs.create_job(logged_in["id"], {}, 10)
        resp = client.post("/api/check/cancel", json={"job_id": job_id})
        assert resp.status_code == 200
        assert jobs.get_job(job_id)["status"] == jobs.CANCELING


class TestHealth:
    def test_public_health_hides_infrastructure_detail(self, client):
        body = client.get("/api/health").get_json()
        assert "checks" in body
        # Queue depths and the Redis URL are admin-only.
        assert "queues" not in body
        assert "redis_url" not in body
        assert "db" not in body

    def test_admin_health_requires_admin(self, client, logged_in):
        assert client.get("/api/admin/health").status_code in (401, 403)
