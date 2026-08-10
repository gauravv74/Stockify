"""The offer has to survive the trip from scraper to row, for every platform.

Offers were only ever populated by BigBasket, whose `bank_offers` object is
empty on every SKU we have sampled — so the column read "No offer found" on
every row of every search. The fix derives the shelf discount centrally, after
price and MRP are normalised, which is the only point where one implementation
covers all eight platforms.

These tests pin the two properties that make that safe: a card offer a scraper
did find is never overwritten, and a discount is never dressed up as one.
"""

from __future__ import annotations

import pytest

from stockly import checks, offers


@pytest.fixture
def rows(monkeypatch):
    """Drive execute_platform_check with a stubbed scraper so these tests
    exercise the wiring rather than the network."""
    captured = {}

    def fake_check(platform, product, pincode, lat, lon, session):
        return dict(captured["row"])

    monkeypatch.setattr(checks, "_run_platform_check", fake_check)

    def run(row, platform="zepto"):
        captured["row"] = row
        return checks.execute_platform_check(platform, "iphone", "411001",
                                             lat=18.5, lon=73.8)

    return run


class TestDiscountReachesEveryPlatform:
    @pytest.mark.parametrize("platform", [
        "blinkit", "instamart", "zepto", "bigbasket",
        "flipkart", "jiomart", "apple", "croma",
    ])
    def test_any_platform_with_a_price_below_mrp_gets_an_offer(self, rows, platform):
        """None of the eight scrapers sets best_offer itself except BigBasket,
        so a per-scraper fix would have left seven platforms blank."""
        row = rows({"status": "available", "price": 12099, "mrp": 14999},
                   platform=platform)
        assert row["best_offer"]["savings"] == 2900.0
        assert row["best_offer"]["kind"] == "discount"

    # No offer leaves the key absent rather than None: the caller merges this
    # onto blank_row, whose best_offer=None is the "we looked" default.
    def test_equal_price_and_mrp_still_reports_no_offer(self, rows):
        row = rows({"status": "available", "price": 30, "mrp": 30})
        assert row.get("best_offer") is None

    def test_a_row_with_no_prices_is_untouched(self, rows):
        row = rows({"status": "not_found", "price": "", "mrp": ""})
        assert row.get("best_offer") is None


class TestCardOffersWin:
    def test_a_scraper_card_offer_is_not_replaced_by_the_discount(self, rows):
        """BigBasket can supply both. The published offer is the stronger
        claim and must survive, even though the MRP gap here is larger."""
        card = offers.make(savings_text="₹168 OFF", final_price=675.2)
        row = rows({"status": "available", "price": 843, "mrp": 1299,
                    "best_offer": card}, platform="bigbasket")
        assert row["best_offer"] is card
        assert row["best_offer"]["kind"] == "card"


class TestErrorsStayHonest:
    def test_a_failed_check_never_grows_an_offer(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("scraper exploded")

        monkeypatch.setattr(checks, "_run_platform_check", boom)
        row = checks.execute_platform_check("zepto", "iphone", "411001",
                                            lat=18.5, lon=73.8)
        assert row["status"] == "error"
        assert row.get("best_offer") is None
