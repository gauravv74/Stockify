"""Dispatcher: plan expansion, idempotency keys, and safety limits."""

from __future__ import annotations

import pytest

import config
import jobs
from stockly import dispatcher


class TestPlanExpansion:
    def test_expands_full_cartesian_product(self):
        plan = {
            "pincodes": ["411001", "411002", "411003"],
            "products": ["iphone 17"],
            "platforms": ["blinkit", "zepto"],
        }
        rows = list(dispatcher.iter_checks(plan))
        assert len(rows) == 3 * 1 * 2

    def test_index_is_dense_and_one_based(self):
        plan = {"pincodes": ["1", "2"], "products": ["a", "b"],
                "platforms": ["blinkit"]}
        indexes = [index for index, *_ in dispatcher.iter_checks(plan)]
        assert indexes == [1, 2, 3, 4]

    def test_pincode_major_ordering(self):
        """All platforms for one location resolve together — the UI groups by
        location, so results should fill in location by location."""
        plan = {"pincodes": ["411001", "411002"], "products": ["x"],
                "platforms": ["blinkit", "zepto"]}
        pins = [pincode for _, _, _, pincode in dispatcher.iter_checks(plan)]
        assert pins == ["411001", "411001", "411002", "411002"]

    def test_empty_dimension_yields_nothing(self):
        assert list(dispatcher.iter_checks(
            {"pincodes": [], "products": ["a"], "platforms": ["blinkit"]})) == []


class TestCheckId:
    def test_is_deterministic(self):
        a = dispatcher.check_id_for(3, "blinkit", "iphone 17", "411001")
        b = dispatcher.check_id_for(3, "blinkit", "iphone 17", "411001")
        assert a == b

    def test_is_unique_per_check_in_a_plan(self):
        plan = {"pincodes": ["411001", "411002"], "products": ["a", "b"],
                "platforms": ["blinkit", "zepto"]}
        ids = [dispatcher.check_id_for(i, plat, prod, pin)
               for i, plat, prod, pin in dispatcher.iter_checks(plan)]
        assert len(ids) == len(set(ids)) == 8

    def test_duplicate_products_do_not_collide(self):
        """Same product at the same pincode/platform but a different position
        must stay distinct, or one result would overwrite the other."""
        assert (dispatcher.check_id_for(1, "blinkit", "a", "411001")
                != dispatcher.check_id_for(2, "blinkit", "a", "411001"))


class TestDistanceOrdering:
    def test_orders_by_proximity_using_cached_geocodes(self, db):
        from stockly import geo
        geo.init_db()
        geo.store("400001", {"lat": "19.0760", "lon": "72.8777", "place": "Mumbai"})
        geo.store("110001", {"lat": "28.6139", "lon": "77.2090", "place": "Delhi"})
        geo.store("411001", {"lat": "18.5204", "lon": "73.8567", "place": "Pune"})

        # Reference point: Pune.
        ordered = dispatcher.order_by_distance(
            ["110001", "400001", "411001"], 18.5204, 73.8567)
        assert ordered == ["411001", "400001", "110001"]

    def test_uncached_pincodes_go_last_without_geocoding(self, db):
        from stockly import geo
        geo.init_db()
        geo.store("411001", {"lat": "18.5204", "lon": "73.8567", "place": "Pune"})

        ordered = dispatcher.order_by_distance(["999999", "411001"], 18.5204, 73.8567)
        assert ordered == ["411001", "999999"]


class TestLimits:
    def test_rejects_empty_search(self, db, make_user):
        user = make_user()
        with pytest.raises(dispatcher.LimitExceeded) as exc:
            dispatcher.enforce_limits(user["id"], 0)
        assert exc.value.code == "empty_search"
        assert exc.value.status == 400

    def test_rejects_oversized_search(self, db, make_user, monkeypatch):
        monkeypatch.setattr(config, "MAX_SEARCH_CHECKS", 100)
        user = make_user()
        with pytest.raises(dispatcher.LimitExceeded) as exc:
            dispatcher.enforce_limits(user["id"], 101)
        assert exc.value.code == "search_too_large"
        assert exc.value.status == 409

    def test_rejects_too_many_active_jobs(self, db, make_user, monkeypatch):
        monkeypatch.setattr(config, "MAX_ACTIVE_JOBS_PER_USER", 2)
        user = make_user()
        jobs.create_job(user["id"], {}, 10)
        jobs.create_job(user["id"], {}, 10)

        with pytest.raises(dispatcher.LimitExceeded) as exc:
            dispatcher.enforce_limits(user["id"], 10)
        assert exc.value.code == "too_many_active_jobs"
        assert exc.value.status == 409

    def test_finished_jobs_free_the_slot(self, db, make_user, monkeypatch):
        monkeypatch.setattr(config, "MAX_ACTIVE_JOBS_PER_USER", 1)
        user = make_user()
        job_id = jobs.create_job(user["id"], {}, 10)
        jobs.set_status(job_id, jobs.DONE)
        dispatcher.enforce_limits(user["id"], 10)  # must not raise

    def test_rejects_when_user_queue_too_deep(self, db, make_user, monkeypatch):
        monkeypatch.setattr(config, "MAX_ACTIVE_JOBS_PER_USER", 99)
        monkeypatch.setattr(config, "MAX_QUEUED_CHECKS_PER_USER", 100)
        user = make_user()
        jobs.create_job(user["id"], {}, 80)

        with pytest.raises(dispatcher.LimitExceeded) as exc:
            dispatcher.enforce_limits(user["id"], 50)
        assert exc.value.code == "too_many_queued_checks"
        assert exc.value.status == 429

    def test_global_cap_applies_to_admins_too(self, db, make_user, monkeypatch):
        """Admins bypass per-user caps but must not be able to bury the workers."""
        monkeypatch.setattr(config, "MAX_TOTAL_QUEUED_CHECKS", 100)
        user = make_user()
        jobs.create_job(user["id"], {}, 90)

        with pytest.raises(dispatcher.LimitExceeded) as exc:
            dispatcher.enforce_limits("admin-id", 50, is_admin=True)
        assert exc.value.code == "system_busy"

    def test_admin_bypasses_per_user_caps(self, db, monkeypatch):
        monkeypatch.setattr(config, "MAX_ACTIVE_JOBS_PER_USER", 1)
        jobs.create_job("admin-id", {}, 10)
        jobs.create_job("admin-id", {}, 10)
        dispatcher.enforce_limits("admin-id", 10, is_admin=True)  # must not raise

    def test_limit_response_shape_is_actionable(self, db, make_user, monkeypatch):
        monkeypatch.setattr(config, "MAX_ACTIVE_JOBS_PER_USER", 1)
        user = make_user()
        jobs.create_job(user["id"], {}, 10)

        with pytest.raises(dispatcher.LimitExceeded) as exc:
            dispatcher.enforce_limits(user["id"], 5)
        body, status = exc.value.to_response()
        assert body["error"] == "too_many_active_jobs"
        assert body["message"], "the user needs to be told what to do about it"
        assert status == 409
