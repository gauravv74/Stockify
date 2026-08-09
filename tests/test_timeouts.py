"""Timeout layering.

The queue's time limit must always sit outside whatever timeout the scraper
enforces itself. When the outer limit wins, the task is killed mid-flight and
redelivered — which sends another request to a retailer that is usually already
rate-limiting us, so the retry makes the problem worse. When the inner one
wins, we get a normal error result and choose whether to retry.
"""

from __future__ import annotations

import importlib

import config


def _limit_sec(queue):
    return config.task_time_limit_ms(queue) / 1000.0


class TestQueueLimitExceedsScraperCeiling:
    def test_browser_queue_outlives_the_scraper(self):
        assert _limit_sec(config.QUEUE_BROWSER) > config.SCRAPER_CHECK_TIMEOUT_SEC

    def test_protected_queue_outlives_the_scraper(self):
        assert _limit_sec(config.QUEUE_PROTECTED) > config.SCRAPER_CHECK_TIMEOUT_SEC

    def test_http_queue_outlives_its_own_budget(self):
        assert _limit_sec(config.QUEUE_HTTP) > config.HTTP_CHECK_TIMEOUT_SEC

    def test_holds_even_if_the_scraper_ceiling_is_raised(self, monkeypatch):
        """Someone raising STOCKLY_SCRAPER_CHECK_TIMEOUT_SEC must not silently
        reintroduce the conflict."""
        monkeypatch.setattr(config, "SCRAPER_CHECK_TIMEOUT_SEC", 300.0)
        for queue in (config.QUEUE_BROWSER, config.QUEUE_PROTECTED):
            assert _limit_sec(queue) > 300.0


class TestBlinkitInternalRetryFitsItsBudget:
    def test_worst_case_backoff_fits_in_the_task_limit(self):
        """blinkit_search sleeps 3s * attempt between tries. With the original
        4 attempts that was 30s of sleeping inside a 20s task — unfinishable."""
        bk = importlib.import_module("blinkit_check")
        worst_case_backoff = sum(3 * attempt for attempt in range(1, bk.MAX_RETRIES + 1))
        budget = _limit_sec(config.QUEUE_HTTP)
        assert worst_case_backoff < budget, (
            f"Blinkit can sleep {worst_case_backoff}s inside a {budget}s task; "
            "the task would be killed mid-backoff and redelivered."
        )
