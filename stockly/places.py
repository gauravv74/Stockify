#!/usr/bin/env python3
"""Turning a pincode into a label that tells the user where they are looking.

Results used to be labelled from Nominatim's ``display_name`` for the postcode,
which is an administrative boundary rather than a place: every Pune pincode came
back as "Pune City Subdistrict, Pune District, Maharashtra". Fifty-three rows of
identical text carry no information — you cannot tell 411001 from 411014.

Asking Nominatim for ``addressdetails`` does not help. A postcode match resolves
to the polygon, so the response contains ``county`` and ``state_district`` and no
suburb or neighbourhood at all; both pincodes above return byte-identical
address objects.

India Post does have the answer, because a pincode *is* a postal unit: 411014
lists Viman Nagar and Vadgaon Sheri. The existing fallback already called this
API but read ``Block`` first, which is the subdistrict — the same generic name
for every pincode in the city. Reading the post office names instead is the
whole fix.

Coordinates still come from Nominatim; this module only produces labels.
"""

from __future__ import annotations

import re

INDIA_POST_URL = "https://api.postalpincode.in/pincode/{pin}"

# Head/sub offices are named after recognisable areas; branch offices are often
# an institution or a village hamlet ("9 DRD"), so they rank last.
_BRANCH_RANK = {"Head Post Office": 0, "Sub Post Office": 1, "Branch Post Office": 2}

# Postal suffixes that mean nothing to a shopper: "Viman Nagar S.O", "C D A (O)".
_SUFFIX = re.compile(r"\s*(\(\s*o\s*\)|\b[SBH]\.?O\.?)\s*$", re.I)

MAX_LABEL_PARTS = 2


def _word(word):
    # Keep acronyms as written — "DRD", "C D A", "Dr.B.A." — since title-casing
    # turns them into nonsense like "Drd" and "Dr.b.a.".
    if "." in word or (word.isupper() and len(word) <= 4):
        return word
    # Capitalise the first *letter*, so "(pune)" doesn't defeat the rule.
    m = re.search(r"[a-zA-Z]", word)
    if not m:
        return word
    i = m.start()
    return word[:i] + word[i].upper() + word[i + 1:].lower()


def _clean(name, drop=None):
    """India Post casing is inconsistent: "Viman nagar", "GHORPURI BAZAR".

    ``drop`` removes a trailing parenthetical that merely repeats the city, as
    in "Market Yard (Pune)", which the label would otherwise render as
    "Market Yard (Pune) · Pune". A parenthetical naming somewhere *else* is
    genuine disambiguation and is kept.
    """
    if not name:
        return ""
    name = str(name).strip()
    if drop:
        name = re.sub(r"\s*\(\s*%s\s*\)\s*$" % re.escape(drop), "", name, flags=re.I)
    name = _SUFFIX.sub("", name)
    name = re.sub(r"\s+", " ", name).strip(" ,-")
    return " ".join(_word(w) for w in name.split(" "))


def parse(payload):
    """Structure an India Post response. Returns None when it has no answer."""
    if not isinstance(payload, list) or not payload:
        return None
    head = payload[0] or {}
    if head.get("Status") != "Success":
        return None
    offices = head.get("PostOffice") or []
    if not offices:
        return None

    first = offices[0] or {}
    district = _clean(first.get("District"))
    state = _clean(first.get("State"))

    ranked = sorted(
        offices, key=lambda o: _BRANCH_RANK.get(o.get("BranchType"), 3)
    )
    localities, seen = [], set()
    for office in ranked:
        name = _clean(office.get("Name"), drop=district)
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        localities.append(name)

    # A post office named after its own city adds nothing next to the district.
    localities = [x for x in localities if x.lower() != (district or "").lower()] \
        or localities
    if not localities:
        return None
    return {"localities": localities, "district": district, "state": state}


def label(detail):
    """Short label for the results table: the areas, then the city.

    Kept to two areas because this sits in a table cell; the full list travels
    alongside it so the UI can offer the rest on hover.
    """
    if not detail or not detail.get("localities"):
        return ""
    areas = ", ".join(detail["localities"][:MAX_LABEL_PARTS])
    district = detail.get("district")
    return f"{areas} · {district}" if district else areas


def full_label(detail):
    """Every locality the pincode covers, for the hover title."""
    if not detail or not detail.get("localities"):
        return ""
    parts = [", ".join(detail["localities"])]
    for key in ("district", "state"):
        if detail.get(key):
            parts.append(detail[key])
    return " · ".join(parts)


def fetch(pincode, session):
    """Look up ``pincode`` at India Post. Returns None on any failure.

    Callers treat None as "keep whatever label we already had": a labelling
    problem must never fail a stock check.
    """
    try:
        r = session.get(INDIA_POST_URL.format(pin=pincode), timeout=15)
        return parse(r.json())
    except Exception:
        return None
