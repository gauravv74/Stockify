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
    def test_worst_case_counts_request_time_not_just_sleeping(self):
        """The earlier version of this test only summed the backoff sleeps, so a
        30s-per-request timeout hid behind a 9s "worst case" and the queue limit
        was sized under the real figure. Each attempt can burn a whole request
        timeout *and* a backoff."""
        bk = importlib.import_module("blinkit_check")
        sleeping_only = sum(3 * attempt for attempt in range(1, bk.MAX_RETRIES + 1))
        worst_case = config.http_scraper_worst_case_sec()
        assert worst_case >= sleeping_only + bk.MAX_RETRIES * bk.REQUEST_TIMEOUT

    def test_worst_case_fits_in_the_task_limit(self):
        worst_case = config.http_scraper_worst_case_sec()
        budget = _limit_sec(config.QUEUE_HTTP)
        assert worst_case < budget, (
            f"Blinkit can run {worst_case}s inside a {budget}s task; the task "
            "would be killed mid-flight and redelivered."
        )

    def test_config_matches_the_scrapers_real_constants(self):
        """The maths lives in config but the loop lives in blinkit_check; if they
        drift the limit is sized against a scraper that no longer exists."""
        bk = importlib.import_module("blinkit_check")
        assert bk.MAX_RETRIES == config.HTTP_SCRAPER_MAX_RETRIES
        assert bk.REQUEST_TIMEOUT == config.HTTP_REQUEST_TIMEOUT_SEC

    def test_raising_the_request_timeout_pushes_the_queue_limit_out(self, monkeypatch):
        """The exact failure that shipped: a per-request timeout equal to the
        task limit, so whichever fired first was a coin toss."""
        monkeypatch.setattr(config, "HTTP_REQUEST_TIMEOUT_SEC", 120.0)
        assert _limit_sec(config.QUEUE_HTTP) > config.http_scraper_worst_case_sec()

    def test_no_hardcoded_request_timeouts_in_the_http_scraper(self):
        """Every curl call must read the shared value, or one site drifts back to
        a timeout the queue maths knows nothing about."""
        import pathlib
        import re

        source = pathlib.Path(bk_path()).read_text()
        assert not re.findall(r"timeout=\d", source)


def bk_path():
    import blinkit_check

    return blinkit_check.__file__
