"""Pincode labels have to distinguish one pincode from another.

The bug these tests exist to prevent: every pincode in a city rendering the same
administrative string, so fifty rows of results all claim to be in the same
place. The payloads are trimmed copies of live India Post responses.
"""

from __future__ import annotations

from stockly import places


def _payload(offices, district="Pune", state="Maharashtra"):
    return [{"Status": "Success", "PostOffice": [
        {"Name": name, "BranchType": branch, "Block": "Pune City",
         "District": district, "State": state}
        for name, branch in offices
    ]}]


# 411001 and 411014 are the pair that exposed the problem: Nominatim returns
# byte-identical labels for both.
PUNE_411014 = _payload([
    ("9 DRD", "Branch Post Office"),
    ("Dukirkline", "Sub Post Office"),
    ("Vadgaon Sheri", "Sub Post Office"),
    ("Viman nagar", "Sub Post Office"),
])
PUNE_411001 = _payload([
    ("C D A (O)", "Sub Post Office"),
    ("GHORPURI BAZAR", "Sub Post Office"),
    ("Pune", "Head Post Office"),
])


class TestLabel:
    def test_neighbouring_pincodes_get_different_labels(self):
        a = places.label(places.parse(PUNE_411014))
        b = places.label(places.parse(PUNE_411001))
        assert a != b
        assert "Subdistrict" not in a and "Subdistrict" not in b

    def test_label_names_areas_then_the_city(self):
        assert places.label(places.parse(PUNE_411014)).endswith(" · Pune")

    def test_label_stays_short_enough_for_a_table_cell(self):
        label = places.label(places.parse(PUNE_411014))
        assert label.count(",") < places.MAX_LABEL_PARTS

    def test_full_label_keeps_every_locality_for_the_tooltip(self):
        full = places.full_label(places.parse(PUNE_411014))
        for area in ("Dukirkline", "Vadgaon Sheri", "Viman Nagar", "9 DRD"):
            assert area in full
        assert full.endswith("Maharashtra")


class TestRanking:
    def test_branch_offices_rank_below_named_areas(self):
        """"9 DRD" is a depot; "Viman Nagar" is where someone lives."""
        localities = places.parse(PUNE_411014)["localities"]
        assert localities[-1] == "9 DRD"

    def test_a_post_office_named_after_its_city_is_dropped(self):
        # "Pune HO" next to "· Pune" would just say Pune twice.
        assert "Pune" not in places.parse(PUNE_411001)["localities"]

    def test_duplicates_collapse(self):
        parsed = places.parse(_payload([
            ("Viman Nagar", "Sub Post Office"),
            ("VIMAN NAGAR", "Branch Post Office"),
        ]))
        assert parsed["localities"] == ["Viman Nagar"]


class TestCleaning:
    def test_strips_postal_suffixes(self):
        parsed = places.parse(_payload([("Koregaon Park S.O", "Sub Post Office")]))
        assert parsed["localities"] == ["Koregaon Park"]

    def test_normalises_shouty_and_lazy_casing(self):
        parsed = places.parse(_payload([
            ("GHORPURI BAZAR", "Sub Post Office"),
            ("Viman nagar", "Sub Post Office"),
        ]))
        assert parsed["localities"] == ["Ghorpuri Bazar", "Viman Nagar"]

    def test_drops_a_parenthetical_that_repeats_the_city(self):
        # "Market Yard (Pune) · Pune" says Pune twice.
        parsed = places.parse(_payload([("Market Yard (Pune)", "Sub Post Office")]))
        assert parsed["localities"] == ["Market Yard"]

    def test_keeps_a_parenthetical_naming_somewhere_else(self):
        parsed = places.parse(_payload([("Rajbhavan (Bangalore)", "Sub Post Office")]))
        assert parsed["localities"] == ["Rajbhavan (Bangalore)"]

    def test_keeps_acronyms_intact(self):
        parsed = places.parse(_payload([
            ("C D A (O)", "Sub Post Office"),
            ("Dr.B.A. Chowk", "Sub Post Office"),
        ]))
        assert parsed["localities"] == ["C D A", "Dr.B.A. Chowk"]


class TestFailureModes:
    def test_unhelpful_responses_yield_nothing(self):
        for payload in (None, [], {}, "error", [{"Status": "Error"}],
                        [{"Status": "Success"}],
                        [{"Status": "Success", "PostOffice": []}]):
            assert places.parse(payload) is None

    def test_labels_of_nothing_are_empty_not_crashes(self):
        assert places.label(None) == ""
        assert places.full_label(None) == ""

    def test_fetch_swallows_transport_errors(self):
        class Boom:
            def get(self, *a, **k):
                raise OSError("network down")

        # A labelling failure must never fail the stock check that needed it.
        assert places.fetch("411001", Boom()) is None
