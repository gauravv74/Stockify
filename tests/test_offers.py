"""What we are allowed to call a card offer.

The payloads here are trimmed copies of live responses captured from the
retailers, so these tests fail if a platform changes shape *or* if someone
loosens the rule that an offer must come from the retailer. The second failure
mode is the important one: a fabricated offer is worse than no offer, because
the user acts on it at the checkout page.
"""

from __future__ import annotations

from stockly import offers

# Captured from BigBasket's listing API: the only populated bank_offers we have
# found across eight platforms. Note there is no issuer anywhere in it.
BB_WITH_OFFER = {
    "bank_offers": {
        "base_url": "https://www.bbassets.com/media/assets/",
        "effective_price": 675.2,
        "effective_price_text": "BEST DEAL @",
        "savings_text": "\u20b9168 OFF",
    },
    "discount": {"mrp": "1299", "d_text": "35% OFF", "prim_price": {"sp": "843"}},
}

# The overwhelmingly common case: the keys exist but carry nothing.
BB_NO_OFFER = {
    "offer_communication": {},
    "available_offer_type": "offer",
    "offer_badge_text": "",
    "emi_offers": {},
    "bank_offers": {},
    "discount": {"mrp": "999", "d_text": "80% OFF", "prim_price": {"sp": "199"}},
    "offer": {},
}


class TestBigBasket:
    def test_reads_a_real_bank_offer(self):
        offer = offers.from_bigbasket(BB_WITH_OFFER)
        assert offer is not None
        assert offer["final_price"] == 675.2
        assert offer["savings"] == 168.0
        assert offer["savings_text"] == "\u20b9168 OFF"

    def test_does_not_invent_an_issuer(self):
        # BigBasket never names the bank, so neither do we.
        assert offers.from_bigbasket(BB_WITH_OFFER)["issuer"] is None

    def test_empty_containers_mean_no_offer(self):
        assert offers.from_bigbasket(BB_NO_OFFER) is None

    def test_product_discount_is_not_a_card_offer(self):
        """An 80% shelf discount with no bank_offers must not become an offer."""
        assert BB_NO_OFFER["discount"]["d_text"] == "80% OFF"
        assert offers.from_bigbasket(BB_NO_OFFER) is None

    def test_survives_junk(self):
        for junk in (None, {}, [], "nope", {"bank_offers": None},
                     {"bank_offers": "yes"}, {"bank_offers": {"base_url": "x"}}):
            assert offers.from_bigbasket(junk) is None


class TestMake:
    def test_needs_something_concrete_to_say(self):
        assert offers.make() is None
        assert offers.make(issuer="HDFC Bank") is None, \
            "an issuer alone tells the shopper nothing they can act on"

    def test_derives_saving_from_the_retailers_own_numbers(self):
        offer = offers.make(final_price=675.2, base_price="843")
        assert offer["savings"] == 167.8
        assert offer["savings_text"] == "₹168 OFF"

    def test_does_not_derive_a_saving_from_a_higher_final_price(self):
        assert offers.make(final_price=900, base_price="843")["savings"] is None

    def test_parses_money_out_of_text(self):
        assert offers.make(savings_text="₹1,168 OFF")["savings"] == 1168.0
        assert offers.make(savings_text="Save ₹2000 with any card")["savings"] == 2000.0

    def test_keeps_an_issuer_the_retailer_named(self):
        offer = offers.make(savings_text="10% off", issuer="HDFC Bank")
        assert offer["issuer"] == "HDFC Bank"


class TestFromPrice:
    """MRP above selling price is the only saving any retailer actually gives
    us. It is real and checkable, but it is not a payment offer, and the whole
    value of `kind` is that the two can never be confused downstream."""

    def test_a_price_below_mrp_is_a_saving(self):
        offer = offers.from_price(131199, 134900)
        assert offer["savings"] == 3701.0
        assert offer["final_price"] == 131199.0
        assert offer["base_price"] == 134900.0
        assert offer["percent"] == 3

    def test_it_is_never_labelled_a_card_offer(self):
        assert offers.from_price(3899, 5695)["kind"] == "discount"
        assert offers.from_bigbasket(BB_WITH_OFFER)["kind"] == "card"

    def test_it_never_names_a_bank(self):
        assert offers.from_price(3899, 5695)["issuer"] is None

    def test_equal_mrp_and_price_is_not_a_saving(self):
        """The normal case on groceries: BigBasket and Blinkit returned
        mrp == price on every product sampled. That is no offer, not a ₹0 one."""
        assert offers.from_price(30, 30) is None

    def test_mrp_below_price_is_bad_data_we_decline_to_read(self):
        assert offers.from_price(900, 843) is None

    def test_missing_or_junk_numbers_yield_nothing(self):
        for price, mrp in ((None, 100), (100, None), ("", ""), (0, 100),
                           ("abc", "def"), (20, None)):
            assert offers.from_price(price, mrp) is None

    def test_percent_is_suppressed_when_it_would_round_to_nothing(self):
        assert offers.from_price(9999, 10000)["percent"] is None


class TestBest:
    def test_a_real_card_offer_beats_a_bigger_shelf_discount(self):
        """Different claims, not different sizes: a card offer is the stronger
        statement even when the rupee saving is smaller."""
        card = offers.make(savings_text="₹50 OFF", final_price=950)
        discount = offers.from_price(500, 5000)
        assert offers.best(card, discount) is card

    def test_larger_saving_wins_within_a_kind(self):
        small = offers.from_price(95, 100)
        large = offers.from_price(50, 100)
        assert offers.best(small, large) is large

    def test_nothing_found_stays_nothing(self):
        assert offers.best(None, None) is None
        assert offers.best() is None


class TestDescribe:
    def test_no_offer_describes_as_empty(self):
        assert offers.describe(None) == ""

    def test_summarises_the_parts_present(self):
        assert offers.describe(offers.from_bigbasket(BB_WITH_OFFER)) == \
            "₹168 OFF · pay ₹675"
