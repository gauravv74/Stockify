"""Job store: idempotency, progress, cancellation, cursors, stale detection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jobs


def _row(index=1, platform="blinkit", status="available"):
    return {"type": "result", "index": index, "platform": platform, "status": status}


class TestIdempotency:
    def test_same_check_id_inserts_once(self, job):
        job_id = job["job_id"]
        first, completed, _ = jobs.record_result(job_id, "c1", _row())
        second, completed_again, _ = jobs.record_result(job_id, "c1", _row())

        assert first is True
        assert second is False, "a redelivered task must not create a second row"
        assert completed == 1
        assert completed_again == 1, "progress must not advance on a duplicate"
        assert len(jobs.get_events(job_id)) == 1

    def test_distinct_check_ids_both_insert(self, job):
        job_id = job["job_id"]
        jobs.record_result(job_id, "c1", _row(1))
        jobs.record_result(job_id, "c2", _row(2))
        assert len(jobs.get_events(job_id)) == 2

    def test_same_check_id_across_jobs_is_allowed(self, db, make_user):
        user = make_user()
        a = jobs.create_job(user["id"], {}, 1)
        b = jobs.create_job(user["id"], {}, 1)
        assert jobs.record_result(a, "c1", _row())[0] is True
        assert jobs.record_result(b, "c1", _row())[0] is True

    def test_notices_without_check_id_are_not_deduped(self, job):
        job_id = job["job_id"]
        jobs.add_event(job_id, {"type": "notice"})
        jobs.add_event(job_id, {"type": "notice"})
        assert len(jobs.get_events(job_id)) == 2


class TestCursor:
    def test_seq_is_monotonic_and_resumable(self, job):
        job_id = job["job_id"]
        for i in range(5):
            jobs.record_result(job_id, f"c{i}", _row(i))

        events = jobs.get_events(job_id)
        seqs = [e["seq"] for e in events]
        assert seqs == sorted(seqs), "cursor must be monotonically increasing"

        midpoint = seqs[1]
        rest = jobs.get_events(job_id, after_seq=midpoint)
        assert [e["seq"] for e in rest] == seqs[2:]

    def test_cursor_past_end_returns_nothing(self, job):
        job_id = job["job_id"]
        jobs.record_result(job_id, "c1", _row())
        last = jobs.get_events(job_id)[-1]["seq"]
        assert jobs.get_events(job_id, after_seq=last) == []


class TestProgressAndFinalize:
    def test_job_finalizes_only_when_all_checks_report(self, db, make_user):
        user = make_user()
        job_id = jobs.create_job(user["id"], {}, 2)

        jobs.record_result(job_id, "c1", _row(1))
        assert jobs.finalize_if_complete(job_id) is None
        assert jobs.get_job(job_id)["status"] == jobs.QUEUED

        jobs.record_result(job_id, "c2", _row(2))
        assert jobs.finalize_if_complete(job_id) == jobs.DONE
        assert jobs.get_job(job_id)["status"] == jobs.DONE

    def test_finalize_is_idempotent_under_races(self, db, make_user):
        user = make_user()
        job_id = jobs.create_job(user["id"], {}, 1)
        jobs.record_result(job_id, "c1", _row())

        assert jobs.finalize_if_complete(job_id) == jobs.DONE
        assert jobs.finalize_if_complete(job_id) is None, "must not re-finalize"

    def test_failed_checks_counted_separately(self, db, make_user):
        user = make_user()
        job_id = jobs.create_job(user["id"], {}, 2)
        jobs.record_result(job_id, "c1", _row(status="available"))
        jobs.record_result(job_id, "c2", _row(status="error"), failed=True)

        row = jobs.get_job(job_id)
        assert row["completed_checks"] == 2
        assert row["failed_checks"] == 1

    def test_mark_running_only_from_queued(self, db, make_user):
        user = make_user()
        job_id = jobs.create_job(user["id"], {}, 1)
        jobs.mark_running(job_id)
        assert jobs.get_job(job_id)["status"] == jobs.RUNNING

        jobs.set_status(job_id, jobs.DONE)
        jobs.mark_running(job_id)
        assert jobs.get_job(job_id)["status"] == jobs.DONE, "terminal must be sticky"


class TestCancellation:
    def test_cancel_sets_flag_and_transitional_status(self, job):
        job_id = job["job_id"]
        assert jobs.request_cancel(job_id) is True
        row = jobs.get_job(job_id)
        assert row["cancel"] is True
        assert row["status"] == jobs.CANCELING
        assert jobs.is_canceled(job_id) is True

    def test_cancel_scoped_to_owner(self, job, make_user):
        other = make_user()
        assert jobs.request_cancel(job["job_id"], user_id=other["id"]) is False
        assert jobs.is_canceled(job["job_id"]) is False

    def test_canceled_job_finalizes_as_canceled(self, db, make_user):
        user = make_user()
        job_id = jobs.create_job(user["id"], {}, 10)
        jobs.record_result(job_id, "c1", _row())
        jobs.request_cancel(job_id)
        assert jobs.finalize_if_complete(job_id) == jobs.CANCELED

    def test_cancel_after_all_results_still_reports_done(self, db, make_user):
        user = make_user()
        job_id = jobs.create_job(user["id"], {}, 1)
        jobs.record_result(job_id, "c1", _row())
        jobs.request_cancel(job_id)
        # Everything was delivered, so the user got a complete result set.
        assert jobs.finalize_if_complete(job_id) == jobs.DONE


class TestLimitsAndRecovery:
    def test_active_job_and_outstanding_counts(self, db, make_user):
        user = make_user()
        jobs.create_job(user["id"], {}, 100)
        jobs.create_job(user["id"], {}, 50)

        assert jobs.count_active_jobs(user["id"]) == 2
        assert jobs.queued_checks_for_user(user["id"]) == 150
        assert jobs.total_queued_checks() == 150

    def test_terminal_jobs_drop_out_of_counts(self, db, make_user):
        user = make_user()
        job_id = jobs.create_job(user["id"], {}, 100)
        jobs.set_status(job_id, jobs.DONE)
        assert jobs.count_active_jobs(user["id"]) == 0
        assert jobs.queued_checks_for_user(user["id"]) == 0

    def test_outstanding_never_negative_on_overshoot(self, db, make_user):
        """Bounded overshoot is expected under parallelism; totals must not go negative."""
        user = make_user()
        job_id = jobs.create_job(user["id"], {}, 1)
        jobs.record_result(job_id, "c1", _row(1))
        jobs.record_result(job_id, "c2", _row(2))
        assert jobs.queued_checks_for_user(user["id"]) == 0

    def test_stale_job_detected_same_day(self, db, make_user):
        """Regression: ISO-8601 vs SQLite datetime() string comparison.

        Both formats share a date prefix, but 'T' > ' ', so a naive comparison
        never matched a job that went stale on the same calendar day.
        """
        user = make_user()
        job_id = jobs.create_job(user["id"], {}, 5)
        stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
        with jobs._conn() as conn:
            conn.execute(
                "UPDATE search_jobs SET last_heartbeat_at = ? WHERE id = ?",
                (stale_ts, job_id),
            )

        found = {j["id"] for j in jobs.stale_jobs(300)}
        assert job_id in found

    def test_fresh_job_not_stale(self, job):
        jobs.heartbeat(job["job_id"])
        assert job["job_id"] not in {j["id"] for j in jobs.stale_jobs(300)}
