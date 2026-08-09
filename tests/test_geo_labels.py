"""Upgrading the labels of pincodes that were geocoded before the new rules.

The cache holds two very different things: coordinates, which are permanent and
expensive to fetch, and a label, which is presentation and cheap to redo. This
distinction is the whole design — a labelling change must not trigger a
re-geocode behind Nominatim's one-request-per-second limit, and must never make
a stock check fail.
"""

from __future__ import annotations

import pytest

INDIA_POST_411014 = [{"Status": "Success", "PostOffice": [
    {"Name": "Viman nagar", "BranchType": "Sub Post Office",
     "Block": "Pune City", "District": "Pune", "State": "Maharashtra"},
    {"Name": "Vadgaon Sheri", "BranchType": "Sub Post Office",
     "Block": "Pune City", "District": "Pune", "State": "Maharashtra"},
]}]

OLD_LABEL = "Pune City Subdistrict, Pune District, Maharashtra"


@pytest.fixture
def geo(db, monkeypatch):
    from stockly import geo as geo_module

    geo_module._seeded = True          # skip the legacy JSON import
    geo_module.init_db()
    return geo_module


class _Session:
    """India Post stand-in that records how often it was asked."""

    def __init__(self, payload=INDIA_POST_411014):
        self.payload = payload
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        payload = self.payload

        class R:
            @staticmethod
            def json():
                if isinstance(payload, Exception):
                    raise payload
                return payload
        return R()


def _seed_legacy(geo, pincode="411014"):
    """A row as written by the previous version: good coordinates, poor label."""
    with geo._conn() as conn:
        conn.execute(
            "INSERT INTO geocache (pincode, lat, lon, place, updated_at) "
            "VALUES (?, ?, ?, ?, '2026-01-01T00:00:00+00:00')",
            (pincode, "18.5679", "73.9143", OLD_LABEL),
        )


class TestMigration:
    def test_existing_rows_default_to_the_old_label_version(self, geo):
        _seed_legacy(geo)
        assert geo.lookup("411014")["label_v"] == 0

    def test_a_stale_row_is_relabelled_on_next_use(self, geo, monkeypatch):
        _seed_legacy(geo)
        session = _Session()
        monkeypatch.setattr(geo.bk, "geocode_pincode",
                            lambda *a, **k: pytest.fail("must not re-geocode"))

        result = geo.resolve("411014", session=session)

        assert result["place"] == "Viman Nagar, Vadgaon Sheri · Pune"
        assert "Subdistrict" not in result["place"]
        assert session.calls == 1

    def test_relabelling_keeps_the_coordinates(self, geo):
        _seed_legacy(geo)
        result = geo.resolve("411014", session=_Session())
        assert (result["lat"], result["lon"]) == ("18.5679", "73.9143")

    def test_relabelling_happens_once(self, geo):
        _seed_legacy(geo)
        session = _Session()
        geo.resolve("411014", session=session)
        geo.resolve("411014", session=session)
        geo.resolve("411014", session=session)
        assert session.calls == 1, "the new label should be persisted, not refetched"

    def test_the_tooltip_list_is_stored_too(self, geo):
        _seed_legacy(geo)
        geo.resolve("411014", session=_Session())
        assert "Vadgaon Sheri" in geo.lookup("411014")["place_full"]


class TestResilience:
    def test_a_failed_relabel_keeps_the_old_label(self, geo):
        _seed_legacy(geo)
        result = geo.resolve("411014", session=_Session(payload=RuntimeError("502")))
        assert result["place"] == OLD_LABEL
        assert result["lat"] == "18.5679"

    def test_a_failed_relabel_is_retried_next_time(self, geo):
        """A transient outage must not permanently freeze a bad label."""
        _seed_legacy(geo)
        geo.resolve("411014", session=_Session(payload=RuntimeError("502")))
        assert geo.lookup("411014")["label_v"] == 0

        result = geo.resolve("411014", session=_Session())
        assert result["place"] == "Viman Nagar, Vadgaon Sheri · Pune"


class TestBackfill:
    """The bulk path run after a deploy, so labels don't trickle in per search."""

    def test_lists_only_rows_below_the_current_version(self, geo):
        _seed_legacy(geo, "411014")
        _seed_legacy(geo, "411001")
        geo.resolve("411014", session=_Session())

        assert geo.stale_label_pincodes() == ["411001"]

    def test_relabels_everything_stale(self, geo):
        for pin in ("411001", "411014", "411038"):
            _seed_legacy(geo, pin)

        summary = geo.backfill_labels(session=_Session(), pause=0)

        assert summary == {"total": 3, "relabelled": 3, "failed": 0}
        assert geo.stale_label_pincodes() == []

    def test_is_safe_to_run_twice(self, geo):
        _seed_legacy(geo)
        geo.backfill_labels(session=_Session(), pause=0)
        assert geo.backfill_labels(session=_Session(), pause=0)["total"] == 0

    def test_reports_failures_without_raising(self, geo):
        _seed_legacy(geo)
        summary = geo.backfill_labels(session=_Session(payload=RuntimeError("502")),
                                      pause=0)
        assert summary == {"total": 1, "relabelled": 0, "failed": 1}
        assert geo.lookup("411014")["place"] == OLD_LABEL

    def test_never_moves_coordinates(self, geo):
        _seed_legacy(geo)
        geo.backfill_labels(session=_Session(), pause=0)
        row = geo.lookup("411014")
        assert (row["lat"], row["lon"]) == ("18.5679", "73.9143")


class TestFreshGeocode:
    def test_a_new_pincode_gets_a_locality_label(self, geo, monkeypatch):
        monkeypatch.setattr(geo.bk, "geocode_pincode", lambda *a, **k: {
            "lat": "18.5679", "lon": "73.9143", "place": OLD_LABEL})

        result = geo.resolve("411014", session=_Session())

        assert result["place"] == "Viman Nagar, Vadgaon Sheri · Pune"
        assert geo.lookup("411014")["label_v"] == geo.LABEL_VERSION

    def test_falls_back_to_the_geocoder_label_when_india_post_is_silent(
            self, geo, monkeypatch):
        monkeypatch.setattr(geo.bk, "geocode_pincode", lambda *a, **k: {
            "lat": "18.5679", "lon": "73.9143", "place": OLD_LABEL})

        result = geo.resolve("411014", session=_Session(payload=[{"Status": "Error"}]))

        assert result["place"] == OLD_LABEL
        assert geo.lookup("411014")["label_v"] == 0, "so it retries later"
