"""The UI's DOM contract.

The frontend is one hand-written HTML file whose script reaches into the markup
by id — `$('runBtn')` and friends — with no framework or compiler in between.
Nothing fails loudly when an id in the markup and an id in the script drift
apart: the element is simply null, and one feature quietly stops working while
the rest of the page looks fine.

These tests pin that contract down so a restyle or a layout change can't silently
detach the behaviour from the markup.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parent.parent / "static" / "index.html"


@pytest.fixture(scope="module")
def html():
    return INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def markup_ids(html):
    return set(re.findall(r'\bid="([^"]+)"', html))


@pytest.fixture(scope="module")
def script(html):
    body = re.search(r"<script>(.*)</script>", html, re.S)
    assert body, "index.html should contain a script block"
    return body.group(1)


def _referenced_ids(script):
    """ids the script looks up via the `$` helper, minus ones it creates itself.

    The product picker injects its close button through innerHTML and then looks
    it up, so it legitimately never appears in the static markup.
    """
    dynamic = {"poClose"}
    return set(re.findall(r"\$\('([^']+)'\)", script)) - dynamic


class TestEveryLookupResolves:
    def test_no_script_lookup_is_missing_from_the_markup(self, script, markup_ids):
        missing = sorted(_referenced_ids(script) - markup_ids)
        assert not missing, (
            "the script looks up ids that no element defines, so these features "
            f"are silently dead: {missing}"
        )

    def test_the_contract_is_not_trivially_empty(self, script):
        """Guard against a regex change that makes the check vacuous."""
        assert len(_referenced_ids(script)) > 100


class TestStructuralSelectors:
    """Selectors the script queries by class or attribute rather than by id."""

    SELECTORS = [
        (".plat", r'class="[^"]*\bplat\b'),
        (".seg button", r'class="[^"]*\bseg\b'),
        (".authed-only", r'class="[^"]*\bauthed-only\b'),
        (".admin-only-wa", r'class="[^"]*\badmin-only-wa\b'),
        (".pw-toggle", r'class="[^"]*\bpw-toggle\b'),
        ("[data-platform]", r"data-platform="),
        ("[data-loc]", r"data-loc="),
        ("[data-plat]", r"data-plat="),
        ("[data-target] (password toggles)", r"data-target="),
    ]

    @pytest.mark.parametrize("name,pattern", SELECTORS, ids=[s[0] for s in SELECTORS])
    def test_selector_still_matches_something(self, html, name, pattern):
        assert re.search(pattern, html), f"nothing in the markup matches {name}"


class TestPlatformCoverage:
    """Every platform the script knows about needs a control in the markup."""

    PLATFORMS = ["blinkit", "instamart", "zepto", "bigbasket",
                 "flipkart", "jiomart", "apple", "croma"]

    @pytest.mark.parametrize("platform", PLATFORMS)
    def test_platform_has_a_selector_button(self, html, platform):
        assert f'data-platform="{platform}"' in html

    @pytest.mark.parametrize("platform", PLATFORMS)
    def test_platform_has_an_admin_access_toggle(self, html, platform):
        assert f'data-plat="{platform}"' in html


class TestNoDuplicateIds:
    def test_ids_are_unique(self, html):
        """Only the static markup: ids inside the script are template literals
        that expand to distinct values at runtime."""
        static_markup = html[:html.index("<script>")]
        ids = re.findall(r'\bid="([^"]+)"', static_markup)
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        assert not dupes, f"duplicate ids make $() ambiguous: {dupes}"


class TestLayoutContract:
    """Structure the restyle introduced, which the script now depends on."""

    def test_views_exist(self, html):
        for view_id in ("viewSearch", "watchesModal", "adminModal"):
            assert f'id="{view_id}"' in html

    def test_sidebar_navigation_exists(self, html):
        for nav_id in ("sidebar", "navSearch", "navCollapse", "navToggle", "navScrim"):
            assert f'id="{nav_id}"' in html

    def test_status_filters_cover_every_status(self, html):
        for status in ("all", "available", "out_of_stock", "not_found", "not_serviceable"):
            assert f'data-filter="{status}"' in html


class TestResultsColumns:
    """The table header and the cells the renderer emits have to stay in step:
    a mismatched count silently shifts every value one column sideways."""

    def test_offer_replaced_eta_stock(self, html):
        assert "Best Card Offer" in html
        assert "ETA / Stock" not in html

    def test_header_and_row_cell_counts_match(self, html):
        header = re.search(r"<thead>(.*?)</thead>", html, re.S).group(1)
        columns = len(re.findall(r"<th\b", header))
        row = re.search(r"const tr = `(.*?)`;", html, re.S).group(1)
        assert len(re.findall(r"<td\b", row)) == columns

    def test_results_empty_states_span_every_column(self, html):
        """Only the results table: the watches and admin tables have their own
        column counts and their own colspans."""
        header = re.search(r"<thead>(.*?)</thead>", html, re.S).group(1)
        columns = len(re.findall(r"<th\b", header))

        static_empty = re.search(r'<tbody id="tbody">.*?colspan="(\d+)"', html, re.S)
        assert static_empty and int(static_empty.group(1)) == columns

        injected = re.findall(r"""\$\('tbody'\)\.innerHTML\s*=\s*`?[^`\n]*colspan="(\d+)\"""",
                              html)
        assert injected, "expected the script to render empty states into #tbody"
        assert all(int(span) == columns for span in injected), \
            f"empty-state colspans {set(injected)} do not match {columns} columns"


class TestMobileTablesStayReachable:
    """Only the results table may be hidden on a phone.

    `.table-scroll` wraps four tables, but just one — the results table — has a
    card layout to fall back on. An unscoped `display: none` therefore deleted
    Your watches, Existing accounts and Recent searches from mobile entirely,
    with no error and nothing on screen to suggest anything was missing.
    """

    @pytest.fixture(scope="class")
    def mobile_css(self, html):
        start = html.index("@media (max-width: 860px)")
        depth, i = 0, html.index("{", start)
        for j in range(i, len(html)):
            if html[j] == "{":
                depth += 1
            elif html[j] == "}":
                depth -= 1
                if depth == 0:
                    return html[start:j]
        raise AssertionError("unterminated media query")

    def test_the_hide_rule_is_scoped_to_the_results_table(self, mobile_css):
        assert ".results .table-scroll { display: none; }" in mobile_css
        assert not re.search(r"(?<![\w.\-]\s)^\s*\.table-scroll\s*\{[^}]*display:\s*none",
                             mobile_css, re.M), \
            "hiding every .table-scroll also hides the watches and admin tables"

    def test_tables_without_a_card_fallback_are_not_hidden(self, html, mobile_css):
        """Each non-results .table-scroll must have no cards sibling to swap to."""
        wrappers = html.count('class="table-scroll"') + html.count('class="table-scroll" ')
        assert wrappers >= 3, "expected the watches and admin tables to use .table-scroll"
        assert html.count('id="cards"') == 1, \
            "only the results table has a card layout; a second one would change this rule"

    def test_they_can_scroll_sideways_instead(self, mobile_css):
        assert re.search(r"\.users-table\s*\{[^}]*min-width", mobile_css), \
            "without a min-width the columns collapse instead of scrolling"


class TestNewControls:
    def test_in_stock_toggle_is_wired(self, html, script):
        assert 'id="inStockOnly"' in html
        assert "inStockOnly" in script and "applyStatusFilter" in script

    def test_product_suggestions_are_wired(self, html, script):
        assert 'id="prodSuggestChips"' in html
        assert "/api/products/top" in script

    def test_offers_are_never_fabricated_client_side(self, script):
        """The UI must render what the backend found, not infer an offer from
        the price gap — that inference is the failure mode this feature bans."""
        offer_fn = re.search(r"function offerHtml\(r\)\s*\{(.*?)\n\}", script, re.S)
        assert offer_fn, "offerHtml should exist"
        body = offer_fn.group(1)
        assert "No offer found" in body
        assert "r.mrp" not in body, "an MRP gap is a shelf discount, not a card offer"
