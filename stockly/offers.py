#!/usr/bin/env python3
"""Payment-offer extraction, and the rules about what we are allowed to claim.

A shopper acts on what this column says, so the bar is that every field we show
came from the retailer. That is the whole reason this module exists rather than
a few lines inside each scraper.

**We do not name a bank the retailer did not name.** Inferring "HDFC" from a
₹168 saving would be inventing the single detail a shopper would act on.

**A card offer and a shelf discount are different claims**, and an offer
carries ``kind`` so the UI can never present one as the other:

``kind="card"``
    A payment offer the retailer published — money off *for paying a
    particular way*. Only ever built from a retailer's own offer object.

``kind="discount"``
    MRP above selling price: a real, checkable saving that needs no card. It
    is arithmetic on two numbers the retailer gave us, not an inference.

The second kind was originally refused here, on the grounds that a shelf
discount is not a payment offer. It still isn't — but refusing it left the
column reading "No offer found" on every row, because **no retailer we scrape
publishes card offers at all**. Measured directly: BigBasket's ``bank_offers``
is empty on every grocery *and* electronics SKU sampled and its product pages
contain no offer language; Zepto's only offer text is referral-coupon
marketing; JioMart's matches are CSS class names; Flipkart and Instamart return
none. So the honest column shows the saving that does exist, labelled as what
it is, and lights up with a real card offer the moment a retailer exposes one.

Where a platform exposes neither, callers get ``None`` and the UI says "No
offer found". That is a real answer, not a gap to paper over.
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
         base_price=None, kind="card", base_label=None):
    """Build an offer, or return None if there is nothing trustworthy to show.

    An offer has to tell the shopper what they save; a bare "there is an offer"
    badge is noise. ``issuer`` stays None unless the retailer named one.
    """
    final_price = _num(final_price)
    saving = _num(savings_text)

    # Derive the saving from the prices only when the retailer gave us both —
    # that is arithmetic on its own numbers, not a guess.
    base = _num(base_price)
    if saving is None and final_price is not None:
        if base is not None and base > final_price:
            saving = round(base - final_price, 2)

    if saving is None and final_price is None:
        return None

    percent = None
    if saving and base and base > 0:
        pct = round(saving * 100.0 / base)
        # Below 1% the rounding says more about the arithmetic than the deal.
        if pct >= 1:
            percent = int(pct)

    return {
        "kind": kind,
        "issuer": (issuer or "").strip() or None,
        "savings_text": (str(savings_text).strip() if savings_text else
                         (f"₹{saving:,.0f} OFF" if saving else "")),
        "savings": saving,
        "percent": percent,
        "final_price": final_price,
        "base_price": base,
        "base_label": base_label,
        "detail": (detail or "").strip() or None,
    }


def from_price(price, mrp):
    """A verified saving from the retailer's own MRP and selling price.

    Not a card offer, and tagged ``kind="discount"`` so it can never be shown
    as one. Returns None unless MRP is genuinely above the selling price:
    equal values (the norm on groceries) mean no saving, and an MRP *below*
    the price is bad data we decline to interpret.
    """
    price = _num(price)
    mrp = _num(mrp)
    if price is None or mrp is None or mrp <= price or price <= 0:
        return None
    return make(final_price=price, base_price=mrp, kind="discount",
                base_label="MRP")


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


def best(*candidates):
    """The offer worth showing, out of everything we found for one product.

    A real card offer always wins, however small: it is a different and
    stronger claim than a shelf discount. Within a kind, more money off wins.
    """
    found = [o for o in candidates if o]
    if not found:
        return None
    return max(found, key=lambda o: (o.get("kind") == "card",
                                     o.get("savings") or 0))


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
