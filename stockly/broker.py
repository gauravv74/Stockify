#!/usr/bin/env python3
"""Dramatiq broker wiring.

Dramatiq over Celery: the work here is fire-and-forget tasks with per-queue
concurrency and time limits, which Dramatiq expresses directly and with far
less configuration surface. We never need chords, chains or result backends —
job state lives in the database, not in the queue.

Importing this module must not fail when Redis is unavailable: the API falls
back to legacy in-process execution (``config.QUEUE_ENABLED``), and unit tests
run against a stub broker.
"""

from __future__ import annotations

import logging

import dramatiq
from dramatiq.brokers.stub import StubBroker

import config

log = logging.getLogger("stockly.broker")

_broker = None


def _build_broker():
    """Real Redis broker, or a stub if Redis can't be reached."""
    from dramatiq.brokers.redis import RedisBroker

    broker = RedisBroker(url=config.REDIS_URL)
    # Fail fast at import rather than on the first enqueue inside a request.
    broker.client.ping()
    return broker


def _add_middleware(broker):
    """Expose the in-flight message so a task can read its own retry count.

    Not enabled by default, and the check tasks need it to decide between
    "raise and let Dramatiq retry" and "give up and record an error row".
    """
    from dramatiq.middleware import CurrentMessage

    if not any(isinstance(m, CurrentMessage) for m in broker.middleware):
        broker.add_middleware(CurrentMessage())
    return broker


def get_broker(strict=False):
    """Process-wide broker, created on first use.

    ``strict`` refuses the stub fallback. Worker processes must use it: a
    worker silently attached to a stub broker looks healthy while consuming
    nothing, so it has to crash and let the supervisor restart it. The API does
    the opposite and degrades to inline execution.
    """
    global _broker
    if _broker is not None:
        return _broker

    if not config.QUEUE_ENABLED:
        if strict:
            raise RuntimeError(
                "STOCKLY_QUEUE_ENABLED is off — a worker process has nothing to do.")
        log.warning("queue disabled — using stub broker (legacy inline execution)")
        _broker = StubBroker()
    else:
        try:
            _broker = _build_broker()
            log.info("broker ready", extra={"redis_url": _redacted(config.REDIS_URL)})
        except Exception as exc:  # noqa: BLE001
            if strict:
                raise
            # Never take the web tier down because Redis is briefly gone; the
            # caller degrades to inline execution and /api/health reports it.
            log.error("redis unavailable — falling back to stub broker",
                      extra={"error": str(exc)[:200]})
            _broker = StubBroker()

    _add_middleware(_broker)
    dramatiq.set_broker(_broker)
    return _broker


def is_live() -> bool:
    """True when a real Redis-backed broker is in use."""
    return config.QUEUE_ENABLED and not isinstance(get_broker(), StubBroker)


def ping() -> tuple[bool, str]:
    """Health probe for /api/health."""
    if not config.QUEUE_ENABLED:
        return False, "queue disabled"
    try:
        broker = get_broker()
        if isinstance(broker, StubBroker):
            return False, "stub broker (redis unreachable)"
        broker.client.ping()
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200]


def queue_depths():
    """Approximate pending message count per queue, for health/backpressure."""
    broker = get_broker()
    if isinstance(broker, StubBroker):
        return {}
    depths = {}
    for queue in (config.QUEUE_HTTP, config.QUEUE_BROWSER,
                  config.QUEUE_PROTECTED, config.QUEUE_CONTROL):
        try:
            depths[queue] = int(broker.client.llen(f"dramatiq:{queue}") or 0)
        except Exception:  # noqa: BLE001
            depths[queue] = -1
    return depths


def _redacted(url):
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    return f"{scheme}://***@{rest.split('@', 1)[-1]}"
