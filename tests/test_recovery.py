"""Stale-job recovery: no job may stay 'running' after its workers die."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jobs
from stockly import recovery


def _age_job(job_id, seconds):
    stale = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    with jobs._conn() as conn:
        conn.execute("UPDATE search_jobs SET last_heartbeat_at = ? WHERE id = ?",
                     (stale, job_id))


class TestSweep:
    def test_dead_job_is_failed_with_an_explanation(self, db, make_user):
        user = make_user()
        job_id = jobs.create_job(user["id"], {}, 10)
        jobs.mark_running(job_id)
        jobs.record_result(job_id, "c1", {"type": "result", "status": "available"})
        _age_job(job_id, 600)

        assert recovery.sweep(timeout_sec=300) == 1
        job = jobs.get_job(job_id)
        assert job["status"] == jobs.ERROR
        assert "1/10" in job["detail"], "the user should see how far it got"

    def test_healthy_job_is_untouched(self, db, make_user):
        user = make_user()
        job_id = jobs.create_job(user["id"], {}, 10)
        jobs.mark_running(job_id)
        jobs.heartbeat(job_id)

        assert recovery.sweep(timeout_sec=300) == 0
        assert jobs.get_job(job_id)["status"] == jobs.RUNNING

    def test_job_that_actually_finished_is_marked_done(self, db, make_user):
        """A worker can deliver the last result and die before finalizing."""
        user = make_user()
        job_id = jobs.create_job(user["id"], {}, 1)
        jobs.mark_running(job_id)
        jobs.record_result(job_id, "c1", {"type": "result", "status": "available"})
        _age_job(job_id, 600)

        recovery.sweep(timeout_sec=300)
        assert jobs.get_job(job_id)["status"] == jobs.DONE

    def test_canceling_job_settles_as_canceled(self, db, make_user):
        user = make_user()
        job_id = jobs.create_job(user["id"], {}, 10)
        jobs.request_cancel(job_id)
        _age_job(job_id, 600)

        recovery.sweep(timeout_sec=300)
        assert jobs.get_job(job_id)["status"] == jobs.CANCELED

    def test_terminal_jobs_are_never_revisited(self, db, make_user):
        user = make_user()
        job_id = jobs.create_job(user["id"], {}, 10)
        jobs.set_status(job_id, jobs.DONE)
        _age_job(job_id, 600)

        assert recovery.sweep(timeout_sec=300) == 0
        assert jobs.get_job(job_id)["status"] == jobs.DONE

    def test_sweep_is_idempotent(self, db, make_user):
        user = make_user()
        job_id = jobs.create_job(user["id"], {}, 10)
        _age_job(job_id, 600)

        assert recovery.sweep(timeout_sec=300) == 1
        assert recovery.sweep(timeout_sec=300) == 0, "already terminal"

    def test_queued_job_never_picked_up_is_also_reaped(self, db, make_user):
        """If every worker was down at enqueue time nothing ever heartbeats."""
        user = make_user()
        job_id = jobs.create_job(user["id"], {}, 10)
        _age_job(job_id, 600)

        recovery.sweep(timeout_sec=300)
        assert jobs.get_job(job_id)["status"] in jobs.TERMINAL_STATUSES
