"""Worker entrypoint for the smoke test: real actors, stubbed retailer calls.

Imported by the ``dramatiq`` CLI instead of ``stockly.tasks`` so the smoke test
exercises the full queue path without touching a real retailer or launching a
browser.
"""

from __future__ import annotations

import random
import time

from stockly import checks


def _fake_check(platform, product, pincode, lat=None, lon=None, session=None, **kw):
    # A little latency so queue behaviour (rather than a tight loop) is tested.
    time.sleep(random.uniform(0.02, 0.15))
    available = random.random() < 0.4
    return {
        "status": "available" if available else "out_of_stock",
        "available": "yes" if available else "no",
        "name": f"{product} ({platform})", "variant": "256GB", "brand": "Apple",
        "price": 79900, "mrp": 84900, "inventory": "5",
        "eta": "10 mins", "merchant_id": "smoke",
    }


checks._run_platform_check = _fake_check

# Import *after* patching so the actors pick up the stub.
from stockly.tasks import (  # noqa: E402,F401
    check_browser, check_http, check_protected, plan_job,
)
