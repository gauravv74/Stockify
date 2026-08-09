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
