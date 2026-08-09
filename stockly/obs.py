#!/usr/bin/env python3
"""Structured logging and lightweight in-process metrics.

Every check crosses three processes (API → broker → worker), so free-text logs
are close to useless for answering "what happened to this user's search". Logs
carry stable identifiers (``request_id``, ``job_id``, ``check_id``, ``user_id``,
``platform``, ``pincode``) and, in production, are emitted as one JSON object
per line so they can be shipped and queried without regex parsing.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from contextlib import contextmanager
from threading import Lock, local

import config

_ctx = local()

# Attributes the stdlib puts on every LogRecord; anything else a caller passed
# via `extra=` is application context and belongs in the JSON payload.
_STD_ATTRS = frozenset((
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info",
    "thread", "threadName", "taskName",
))


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def bind(**fields):
    """Attach context to every log record emitted by this thread."""
    current = getattr(_ctx, "fields", None) or {}
    merged = {**current, **{k: v for k, v in fields.items() if v is not None}}
    _ctx.fields = merged
    return merged


def clear():
    _ctx.fields = {}


def context() -> dict:
    return dict(getattr(_ctx, "fields", None) or {})


@contextmanager
def scope(**fields):
    """Bind context for the duration of a block, then restore what was there."""
    previous = context()
    bind(**fields)
    try:
        yield
    finally:
        _ctx.fields = previous


class _ContextFilter(logging.Filter):
    def filter(self, record):
        for key, value in context().items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable local format that still surfaces bound context."""

    def format(self, record):
        base = super().format(record)
        extra = {
            k: v for k, v in record.__dict__.items()
            if k not in _STD_ATTRS and not k.startswith("_")
        }
        if extra:
            base += "  " + " ".join(f"{k}={v}" for k, v in extra.items())
        return base


_configured = False
_configure_lock = Lock()


def setup(service="stockly"):
    """Install the root handler. Safe to call from every entrypoint."""
    global _configured
    with _configure_lock:
        if _configured:
            return logging.getLogger(service)
        handler = logging.StreamHandler()
        if config.LOG_JSON:
            handler.setFormatter(JsonFormatter())
        else:
            handler.setFormatter(TextFormatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handler.addFilter(_ContextFilter())

        root = logging.getLogger()
        for existing in list(root.handlers):
            root.removeHandler(existing)
        root.addHandler(handler)
        root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))

        # These are chatty at DEBUG and drown out application events.
        for noisy in ("urllib3", "asyncio", "werkzeug"):
            logging.getLogger(noisy).setLevel(logging.INFO)

        _configured = True
        bind(service=service, pid=os.getpid())
        return logging.getLogger(service)


# ── Metrics ────────────────────────────────────────────────────────────────
# Deliberately in-process and dependency-free: enough to answer "is the system
# healthy" via /api/health without committing to Prometheus yet. Counters are
# per-process, so an aggregator must sum across workers.
class _Metrics:
    def __init__(self):
        self._lock = Lock()
        self._counters = {}
        self._timings = {}

    def incr(self, name, value=1, **labels):
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + value

    def observe(self, name, ms, **labels):
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            bucket = self._timings.setdefault(key, {"n": 0, "sum": 0.0, "max": 0.0})
            bucket["n"] += 1
            bucket["sum"] += ms
            bucket["max"] = max(bucket["max"], ms)

    def snapshot(self):
        with self._lock:
            counters = [
                {"name": n, "labels": dict(l), "value": v}
                for (n, l), v in self._counters.items()
            ]
            timings = [
                {"name": n, "labels": dict(l), "count": b["n"],
                 "avg_ms": round(b["sum"] / b["n"], 1) if b["n"] else 0.0,
                 "max_ms": round(b["max"], 1)}
                for (n, l), b in self._timings.items()
            ]
        return {"counters": counters, "timings": timings}


metrics = _Metrics()


@contextmanager
def timed(name, **labels):
    started = time.monotonic()
    try:
        yield
    finally:
        metrics.observe(name, (time.monotonic() - started) * 1000.0, **labels)
