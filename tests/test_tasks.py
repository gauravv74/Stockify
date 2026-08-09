"""End-to-end task execution against an in-memory broker.

These exercise the real actors and dispatcher through a live Dramatiq worker —
only the retailer call itself is stubbed — so they cover the behaviour that
matters most in the new model: no duplicate rows, cancellation actually stops
work, and token exhaustion halts a run.
"""

from __future__ import annotations

import pytest
from dramatiq import Worker

import auth
import config
import jobs
from stockly import broker as broker_mod
from stockly import checks, dispatcher, geo, tasks

ALL_QUEUES = (config.QUEUE_CONTROL, config.QUEUE_HTTP,
              config.QUEUE_BROWSER, config.QUEUE_PROTECTED)


@pytest.fixture
def queue(monkeypatch, db):
    """A live in-memory broker plus a worker, with geocoding pre-warmed."""
    stub = broker_mod.get_broker()
    stub.flush_all()
    # The dispatcher refuses to enqueue unless a broker is live; the stub is
    # functionally live for our purposes.
    monkeypatch.setattr(broker_mod, "is_live", lambda: True)

    geo.init_db()
    for pin in ("411001", "411002", "411003"):
        geo.store(pin, {"lat": "18.5204", "lon": "73.8567", "place": f"Pune {pin}"})

    worker = Worker(stub, worker_timeout=50, worker_threads=2)
    worker.start()

    def drain():
        for _ in range(4):  # control task fans out into check queues
            for name in ALL_QUEUES:
                stub.join(name, fail_fast=True)
            worker.join()

    try:
        yield drain
    finally:
        worker.stop()
        stub.flush_all()


def _start(user, pincodes, products, platforms):
    job_id, total, queued = dispatcher.start_search(
        user, {"total": 0}, pincodes, products, platforms)
    assert queued is True
    return job_id, total


class TestHappyPath:
    def test_every_check_produces_exactly_one_row(self, queue, make_user, stub_check):
        user = make_user(tokens=10_000)
        job_id, total = _start(user, ["411001", "411002"], ["iphone 17"],
                               ["blinkit", "zepto"])
        queue()

        events = [e for e in jobs.get_events(job_id) if e.get("type") == "result"]
        assert total == 4
        assert len(events) == 4
        assert len(stub_check) == 4

    def test_job_reaches_done_and_progress_matches(self, queue, make_user, stub_check):
        user = make_user(tokens=10_000)
        job_id, total = _start(user, ["411001"], ["iphone 17"], ["blinkit"])
        queue()

        job = jobs.get_job(job_id)
        assert job["status"] == jobs.DONE
        assert job["completed_checks"] == total
        assert job["completed_at"] is not None

    def test_result_rows_keep_the_documented_shape(self, queue, make_user, stub_check):
        user = make_user(tokens=10_000)
        job_id, _ = _start(user, ["411001"], ["iphone 17"], ["blinkit"])
        queue()

        row = [e for e in jobs.get_events(job_id) if e.get("type") == "result"][0]
        for field in ("index", "pincode", "platform", "location", "lat", "lon",
                      "product", "status", "available", "name", "price", "seq"):
            assert field in row, f"clients depend on '{field}'"

    def test_cursor_lets_a_client_resume(self, queue, make_user, stub_check):
        user = make_user(tokens=10_000)
        job_id, _ = _start(user, ["411001", "411002"], ["iphone 17"], ["blinkit"])
        queue()

        first = jobs.get_events(job_id, after_seq=0)
        resumed = jobs.get_events(job_id, after_seq=first[0]["seq"])
        assert len(resumed) == len(first) - 1
        assert all(e["seq"] > first[0]["seq"] for e in resumed)


class TestIdempotency:
    def test_redelivered_task_does_not_duplicate_a_row(self, queue, make_user, stub_check):
        user = make_user(tokens=10_000)
        job_id, _ = _start(user, ["411001"], ["iphone 17"], ["blinkit"])
        queue()

        before = len(jobs.get_events(job_id))
        # Replay the identical message, exactly as a crashed-worker redelivery would.
        check_id = dispatcher.check_id_for(1, "blinkit", "iphone 17", "411001")
        tasks.check_http.send(job_id, check_id, 1, "blinkit", "iphone 17",
                              "411001", user["id"], True)
        queue()

        assert len(jobs.get_events(job_id)) == before
        assert jobs.get_job(job_id)["completed_checks"] == 1


class TestCancellation:
    def test_queued_checks_are_skipped_after_cancel(self, queue, make_user, stub_check):
        user = make_user(tokens=10_000)
        job_id = jobs.create_job(user["id"], {}, 3, plan={
            "pincodes": ["411001", "411002", "411003"],
            "products": ["iphone 17"], "platforms": ["blinkit"], "charge": False,
        })
        jobs.request_cancel(job_id)

        dispatcher.enqueue_checks(job_id)
        queue()

        assert stub_check == [], "no retailer should be contacted after cancel"
        assert jobs.get_job(job_id)["status"] == jobs.CANCELED

    def test_cancel_midway_leaves_earlier_results_intact(self, queue, make_user, stub_check):
        user = make_user(tokens=10_000)
        job_id, _ = _start(user, ["411001"], ["iphone 17"], ["blinkit"])
        queue()
        delivered = len(jobs.get_events(job_id))

        jobs.request_cancel(job_id)
        assert len(jobs.get_events(job_id)) == delivered


class TestTokens:
    def test_charges_per_billable_result(self, queue, make_user, stub_check):
        user = make_user(tokens=100)
        job_id, _ = _start(user, ["411001", "411002"], ["iphone 17"], ["blinkit"])
        queue()

        # stub_check always returns 'available' -> TOKEN_COST_IN_STOCK each.
        expected = 100 - 2 * config.TOKEN_COST["available"]
        assert auth.get_balance(user["id"]) == expected

    def test_balance_never_goes_negative_and_run_stops(self, queue, make_user, stub_check):
        """Bounded overshoot: parallel checks may land after the wallet empties,
        but consume_tokens clamps at zero so the user is never overcharged."""
        user = make_user(tokens=3)  # affords one 2-token result, not two
        job_id, _ = _start(user, ["411001", "411002", "411003"], ["iphone 17"],
                           ["blinkit"])
        queue()

        assert auth.get_balance(user["id"]) >= 0
        assert jobs.get_job(job_id)["status"] == jobs.EXHAUSTED

        notices = [e for e in jobs.get_events(job_id)
                   if e.get("kind") == "tokens_exhausted"]
        assert len(notices) == 1, "the exhausted banner must not be emitted twice"

    def test_admin_is_never_charged(self, queue, admin_user, stub_check):
        job_id, _ = _start(admin_user, ["411001"], ["iphone 17"], ["blinkit"])
        queue()
        assert jobs.get_job(job_id)["status"] == jobs.DONE


class TestErrorSemantics:
    def test_geocode_failure_is_recorded_not_retried_forever(
            self, queue, make_user, monkeypatch, stub_check):
        monkeypatch.setattr(geo, "resolve", lambda pin, session=None: None)
        user = make_user(tokens=100)
        job_id, _ = _start(user, ["999999"], ["iphone 17"], ["blinkit"])
        queue()

        rows = [e for e in jobs.get_events(job_id) if e.get("type") == "result"]
        assert [r["status"] for r in rows] == ["geocode_failed"]
        assert jobs.get_job(job_id)["status"] == jobs.DONE
        assert stub_check == [], "a failed geocode must not reach the retailer"

    def test_infrastructure_failure_never_becomes_out_of_stock(
            self, queue, make_user, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("connection reset by peer")

        monkeypatch.setattr(checks, "_run_platform_check", boom)
        user = make_user(tokens=100)
        job_id, _ = _start(user, ["411001"], ["iphone 17"], ["blinkit"])
        queue()

        rows = [e for e in jobs.get_events(job_id) if e.get("type") == "result"]
        assert rows, "a permanently failing check must still record a row"
        assert rows[0]["status"] != "out_of_stock"
        assert checks.is_infrastructure_status(rows[0]["status"])

    def test_final_timeout_records_a_row_so_the_job_can_finish(
            self, db, make_user, monkeypatch):
        """Regression: TimeLimitExceeded subclasses BaseException, so it slips
        past `except Exception`. Without explicit handling the last attempt
        writes no row and the job hangs at completed < total until the reaper."""
        from dramatiq.middleware.time_limit import TimeLimitExceeded

        def timeout(*a, **kw):
            raise TimeLimitExceeded()

        monkeypatch.setattr(checks, "_run_platform_check", timeout)
        monkeypatch.setattr(config, "CHECK_MAX_RETRIES", 0)  # this is the last attempt
        geo.init_db()
        geo.store("411001", {"lat": "18.5", "lon": "73.8", "place": "Pune"})

        user = make_user(tokens=100)
        job_id = jobs.create_job(user["id"], {}, 1)
        tasks._run_check(job_id, "c1", 1, "blinkit", "iphone 17", "411001",
                         user["id"], False)

        rows = [e for e in jobs.get_events(job_id) if e.get("type") == "result"]
        assert len(rows) == 1, "a timed-out check must still record a result"
        assert rows[0]["status"] == "error"
        assert jobs.get_job(job_id)["status"] == jobs.DONE

    def test_timeout_still_retries_while_attempts_remain(self, db, make_user,
                                                         monkeypatch):
        from dramatiq.middleware.time_limit import TimeLimitExceeded

        def timeout(*a, **kw):
            raise TimeLimitExceeded()

        monkeypatch.setattr(checks, "_run_platform_check", timeout)
        monkeypatch.setattr(config, "CHECK_MAX_RETRIES", 2)
        geo.init_db()
        geo.store("411001", {"lat": "18.5", "lon": "73.8", "place": "Pune"})

        user = make_user(tokens=100)
        job_id = jobs.create_job(user["id"], {}, 1)
        with pytest.raises(TimeLimitExceeded):
            tasks._run_check(job_id, "c1", 1, "blinkit", "iphone 17", "411001",
                             user["id"], False)
        assert jobs.get_events(job_id) == [], "no row until retries are exhausted"

    def test_failed_checks_do_not_charge_tokens(self, queue, make_user, monkeypatch):
        monkeypatch.setattr(checks, "_run_platform_check",
                            lambda *a, **kw: {"status": "not_found"})
        user = make_user(tokens=100)
        job_id, _ = _start(user, ["411001"], ["iphone 17"], ["blinkit"])
        queue()
        assert auth.get_balance(user["id"]) == 100
