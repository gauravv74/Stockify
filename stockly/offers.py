#!/usr/bin/env python3
"""Payment-offer extraction, and the rules about what we are allowed to claim.

A shopper acts on what this column says, so the bar is that every field we show
came from the retailer. Two things follow from that, and they are the whole
reason this module exists rather than a few lines inside each scraper.

**A product discount is not a payment offer.** Every platform gives us MRP and
selling price, and the gap between them is tempting to render as "10% off" — but
that is the shelf price, not something a card unlocks. Swiggy compounds the
confusion by naming its selling price ``offerPrice``. None of that belongs here.

**We do not name a bank the retailer did not name.** BigBasket returns the
discounted price and the saving but not the issuer, so the offer is reported
without one. Inferring "HDFC" from a ₹168 saving would be inventing the single
detail a shopper would act on.

Where a platform genuinely exposes nothing, callers get ``None`` and the UI says
"No offer found". That is a real answer, not a gap to paper over.
"""

from __future__ import annotations

import re

# Every key we read is quoted in tests/test_offers.py against captured payloads.
_MONEY = re.compile(r"(\d[\d,]*\.?\d*)")


def _num(value):
    """Best-effort number from an int, float, or a string like "₹168 OFF"."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not value:
        return None
    m = _MONEY.search(str(value).replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def make(savings_text=None, final_price=None, issuer=None, detail=None,
         base_price=None):
    """Build an offer, or return None if there is nothing trustworthy to show.

    An offer has to tell the shopper what they save; a bare "there is an offer"
    badge is noise. ``issuer`` stays None unless the retailer named one.
    """
    final_price = _num(final_price)
    saving = _num(savings_text)

    # Derive the saving from the prices only when the retailer gave us both —
    # that is arithmetic on its own numbers, not a guess.
    if saving is None and final_price is not None:
        base = _num(base_price)
        if base is not None and base > final_price:
            saving = round(base - final_price, 2)

    if saving is None and final_price is None:
        return None

    return {
        "issuer": (issuer or "").strip() or None,
        "savings_text": (str(savings_text).strip() if savings_text else
                         (f"₹{saving:,.0f} OFF" if saving else "")),
        "savings": saving,
        "final_price": final_price,
        "detail": (detail or "").strip() or None,
    }


def from_bigbasket(pricing):
    """BigBasket's ``pricing.bank_offers``, the one real source we have.

    Populated entries look like::

        {"effective_price": 675.2, "effective_price_text": "BEST DEAL @",
         "savings_text": "₹168 OFF", "base_url": "..."}

    The object is present but empty on most SKUs, which is a no-offer answer.
    No field in it, or on the product page, names the issuing bank.
    """
    if not isinstance(pricing, dict):
        return None
    bank = pricing.get("bank_offers")
    if not isinstance(bank, dict) or not bank:
        return None

    discount = pricing.get("discount") or {}
    prim = discount.get("prim_price") or {}
    return make(
        savings_text=bank.get("savings_text"),
        final_price=bank.get("effective_price"),
        base_price=prim.get("sp"),
        detail=(bank.get("effective_price_text") or "").strip() or None,
    )


def describe(offer):
    """One-line summary for logs, CSV and WhatsApp alerts."""
    if not offer:
        return ""
    bits = []
    if offer.get("issuer"):
        bits.append(offer["issuer"])
    if offer.get("savings_text"):
        bits.append(offer["savings_text"])
    if offer.get("final_price") is not None:
        bits.append(f"pay ₹{offer['final_price']:,.0f}")
    return " · ".join(bits)
