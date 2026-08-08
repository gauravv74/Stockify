#!/usr/bin/env python3
"""WhatsApp delivery for Stockly stock alerts.

Free by default. Three interchangeable providers, selected via
``STOCKLY_WHATSAPP_PROVIDER``:

* ``callmebot``  100% free for personal alerts to your own number. One-time
                 setup: WhatsApp the text "I allow callmebot to send me
                 messages" to +34 644 84 71 89; it replies with an API key.
                 Then set STOCKLY_CALLMEBOT_PHONE / STOCKLY_CALLMEBOT_APIKEY.
* ``meta``       WhatsApp Cloud API (Meta) free tier. Sends free-form text
                 inside the 24h service window; outside it you must pass an
                 approved template (STOCKLY_META_TEMPLATE).
* ``none``       No-op (logs only) — handy for local testing.

Public API::

    ok, detail = whatsapp.send("your message", to="9198...")   # to is optional
    whatsapp.is_configured()  -> bool
"""

from __future__ import annotations

import logging
import re

import requests

import config

log = logging.getLogger("stockly.whatsapp")

TIMEOUT = 30


def _clean_phone(phone: str) -> str:
    """Strip everything but digits (CallMeBot & Meta both want bare digits)."""
    return re.sub(r"\D", "", phone or "")


def is_configured() -> bool:
    """True when the selected provider has enough config to actually send."""
    provider = config.WHATSAPP_PROVIDER
    if provider == "webjs":
        return bool(config.WA_BRIDGE_URL and config.WHATSAPP_TO)
    if provider == "callmebot":
        return bool(config.CALLMEBOT_APIKEY and (config.CALLMEBOT_PHONE or config.WHATSAPP_TO))
    if provider == "meta":
        return bool(config.META_TOKEN and config.META_PHONE_ID and config.WHATSAPP_TO)
    return provider == "none"


def _send_callmebot(message: str, to: str) -> tuple[bool, str]:
    phone = _clean_phone(to or config.CALLMEBOT_PHONE or config.WHATSAPP_TO)
    apikey = config.CALLMEBOT_APIKEY
    if not phone or not apikey:
        return False, "callmebot not configured (need phone + apikey)"
    try:
        r = requests.get(
            "https://api.callmebot.com/whatsapp.php",
            params={"phone": phone, "text": message, "apikey": apikey},
            timeout=TIMEOUT,
        )
    except Exception as e:  # network error
        return False, f"callmebot request failed: {e}"
    body = (r.text or "").strip()
    # CallMeBot returns 200 with an HTML/text body; treat explicit error words as
    # failures so a mis-set key surfaces instead of silently "succeeding".
    if r.status_code == 200 and "error" not in body.lower() and "apikey" not in body.lower():
        return True, "sent"
    return False, f"callmebot http={r.status_code} body={body[:180]}"


def _send_meta(message: str, to: str) -> tuple[bool, str]:
    phone = _clean_phone(to or config.WHATSAPP_TO)
    if not (config.META_TOKEN and config.META_PHONE_ID and phone):
        return False, "meta not configured (need token + phone_id + recipient)"
    url = (f"https://graph.facebook.com/{config.META_API_VERSION}"
           f"/{config.META_PHONE_ID}/messages")
    headers = {"Authorization": f"Bearer {config.META_TOKEN}",
               "Content-Type": "application/json"}
    if config.META_TEMPLATE:
        # Template message — required to reach a user outside the 24h window.
        payload = {
            "messaging_product": "whatsapp", "to": phone, "type": "template",
            "template": {
                "name": config.META_TEMPLATE,
                "language": {"code": config.META_TEMPLATE_LANG},
                "components": [{
                    "type": "body",
                    "parameters": [{"type": "text", "text": message}],
                }],
            },
        }
    else:
        payload = {"messaging_product": "whatsapp", "to": phone, "type": "text",
                   "text": {"preview_url": False, "body": message}}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT)
    except Exception as e:
        return False, f"meta request failed: {e}"
    if r.status_code in (200, 201):
        return True, "sent"
    return False, f"meta http={r.status_code} body={(r.text or '')[:180]}"


def _bridge_url() -> str:
    """Bridge base URL: a runtime setting (set by the admin panel) overrides the
    env default so the web container can reach the wa-bridge service by name
    even though only the worker service sets the env var."""
    try:
        import watches
        return (watches.get_setting("wa_bridge_url") or config.WA_BRIDGE_URL)
    except Exception:
        return config.WA_BRIDGE_URL


def _send_webjs(message: str, to: str) -> tuple[bool, str]:
    phone = _clean_phone(to or config.WHATSAPP_TO)
    base = _bridge_url()
    if not base or not phone:
        return False, "webjs bridge not configured (need bridge url + recipient)"
    headers = {}
    if config.WA_BRIDGE_TOKEN:
        headers["X-Auth-Token"] = config.WA_BRIDGE_TOKEN
    try:
        r = requests.post(
            base.rstrip("/") + "/send",
            json={"to": phone, "message": message},
            headers=headers, timeout=TIMEOUT,
        )
    except Exception as e:
        return False, f"webjs request failed (is the bridge running?): {e}"
    if r.status_code == 200:
        return True, "sent"
    # 503 = bridge up but not linked yet (QR unscanned); surfaced as transient.
    return False, f"webjs http={r.status_code} body={(r.text or '')[:180]}"


def send(message: str, to: str | None = None) -> tuple[bool, str]:
    """Send a WhatsApp message. Returns (ok, detail). Never raises."""
    provider = config.WHATSAPP_PROVIDER
    if provider == "none":
        log.info("[whatsapp:none] would send to %s: %s", to or config.WHATSAPP_TO, message)
        return True, "logged (provider=none)"
    try:
        if provider == "webjs":
            ok, detail = _send_webjs(message, to)
        elif provider == "callmebot":
            ok, detail = _send_callmebot(message, to)
        elif provider == "meta":
            ok, detail = _send_meta(message, to)
        else:
            return False, f"unknown provider {provider!r}"
    except Exception as e:  # defensive: delivery must never crash the worker
        log.exception("whatsapp send crashed")
        return False, f"exception: {e}"
    if ok:
        log.info("whatsapp sent via %s to %s", provider, to or config.WHATSAPP_TO)
    else:
        log.warning("whatsapp send failed via %s: %s", provider, detail)
    return ok, detail


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("provider:", config.WHATSAPP_PROVIDER, "configured:", is_configured())
    print(send("Stockly test ✅ — WhatsApp alerts are working."))
