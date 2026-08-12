"""Unit tests for Amazon + Flipkart.com marketplace scrapers (no network)."""

from __future__ import annotations

import json

import amazon_check as az
import flipkart_com_check as fkc


def test_amazon_parse_search_extracts_asin_title_price():
    html = r'''
    <div data-component-type="s-search-result" data-asin="B0TESTASIN">
      <div data-cy="title-recipe">
        <span class="a-size-medium a-color-base">Apple</span>
        <span class="a-size-base-plus">iPhone 15 (128 GB) - Black</span>
      </div>
      <div data-cy="price-recipe">
        <span class="a-price-whole">57,749</span>
        <span class="a-price a-text-price"><span class="a-offscreen">₹69,900</span></span>
      </div>
      <a href="/Apple-iPhone-15/dp/B0TESTASIN/ref=sr">link</a>
    </div>
    '''
    items = az._parse_search(html)
    assert len(items) == 1
    assert items[0]["merchant_id"] == "B0TESTASIN"
    assert "iPhone 15" in items[0]["name"]
    assert items[0]["price"] == 57749.0
    assert items[0]["mrp"] == 69900.0
    assert items[0]["inStock"] is True


def test_amazon_parse_marks_unavailable():
    html = r'''
    <div data-component-type="s-search-result">
      <div data-cy="title-recipe">
        <span>Sony</span>
        <span>WH-1000XM5 Wireless Headphones</span>
      </div>
      <div data-cy="price-recipe"><span class="a-price-whole">25,990</span></div>
      Currently unavailable
      <a href="/dp/B0OOSASIN1">x</a>
    </div>
    '''
    items = az._parse_search(html)
    assert items[0]["inStock"] is False


def test_amazon_match_row_statuses():
    assert az.match_row("x", {"serviceable": None, "error": "boom"})["status"] == "error"
    assert az.match_row("x", {"serviceable": False, "items": []})["status"] == "not_serviceable"
    assert az.match_row("iphone 15", {"serviceable": True, "items": []})["status"] == "not_found"
    row = az.match_row("iphone 15", {
        "serviceable": True,
        "items": [{
            "name": "Apple iPhone 15 128GB Black", "brand": "Apple",
            "variant": "", "price": 57999, "mrp": 69900,
            "inStock": True, "eta": "", "merchant_id": "B0X",
        }],
    })
    assert row["status"] == "available"
    assert row["price"] == 57999


def test_flipkart_com_items_from_state():
    state = {
        "pageDataV4": {
            "page": {
                "data": {
                    "10003": [{
                        "widget": {
                            "data": {
                                "products": [{
                                    "productInfo": {
                                        "value": {
                                            "id": "MOB123",
                                            "listingId": "LSTMOB123",
                                            "titles": {
                                                "title": "Apple iPhone 15 (Black, 128 GB)",
                                                "superTitle": "Apple",
                                            },
                                            "pricing": {
                                                "prices": [
                                                    {"strikeOff": True, "value": 69900},
                                                    {"strikeOff": False, "value": 57749},
                                                ],
                                            },
                                            "availability": {"displayState": "IN_STOCK"},
                                            "keySpecs": ["128 GB ROM"],
                                        }
                                    }
                                }]
                            }
                        }
                    }]
                }
            }
        }
    }
    items = fkc._items_from_state(state)
    assert len(items) == 1
    assert items[0]["name"].startswith("Apple iPhone 15")
    assert items[0]["price"] == 57749.0
    assert items[0]["mrp"] == 69900.0
    assert items[0]["inStock"] is True
    assert items[0]["variant"] == "128 GB ROM"


def test_flipkart_com_out_of_stock():
    state = {
        "x": {
            "productInfo": {
                "value": {
                    "id": "MLK1",
                    "listingId": "LSTMLK1",
                    "titles": {"title": "Amul Cow Milk", "superTitle": "Amul"},
                    "pricing": {"prices": [{"strikeOff": False, "value": 59}]},
                    "availability": {"displayState": "OUT_OF_STOCK"},
                }
            }
        }
    }
    items = fkc._items_from_state(state)
    assert items[0]["inStock"] is False
    row = fkc.match_row("amul milk", {"serviceable": True, "items": items})
    assert row["status"] == "out_of_stock"


def test_flipkart_com_match_row_error_paths():
    assert fkc.match_row("x", {"serviceable": None, "error": "x"})["status"] == "error"
    assert fkc.match_row("x", {"serviceable": True, "items": []})["status"] == "not_found"


def test_norm_keeps_decimal_screen_sizes_from_matching_model_numbers():
    """Regression: Amazon titles like '15.93 cm Display' must not satisfy '15'."""
    import blinkit_check as bk
    assert "15" not in bk._norm("15.93 cm Display").split()
    assert "15.93" in bk._norm("15.93 cm Display").split()
    products = [{
        "name": "Apple iPhone 17 Pro 512 GB: 15.93 cm Display",
        "variant": "", "brand": "Apple", "price": 150900, "inStock": True,
    }, {
        "name": "Apple iPhone 15 128 GB Black",
        "variant": "", "brand": "Apple", "price": 57999, "inStock": True,
    }]
    m = bk.best_match("iphone 15", products)
    assert m is not None
    assert "iPhone 15" in m["name"]
