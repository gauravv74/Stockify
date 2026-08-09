"""The deploy topology is part of the system's correctness.

A queue with no consumer doesn't fail anywhere the application can see it: the
API accepts the job, Redis accepts the message, every container reports healthy,
and the job simply sits in `queued` forever. That happened in production —
`--queues protected_checks,control` was read by dramatiq as one queue literally
named "protected_checks,control", so nothing consumed `control` and no search
ever fanned out. These tests read the compose file the same way dramatiq's CLI
reads its argv.
"""
import pathlib

import pytest

yaml = pytest.importorskip("yaml")

import config

COMPOSE = pathlib.Path(__file__).resolve().parent.parent / "docker-compose.yml"

ALL_QUEUES = {
    config.QUEUE_CONTROL,
    config.QUEUE_HTTP,
    config.QUEUE_BROWSER,
    config.QUEUE_PROTECTED,
}


def _services():
    return yaml.safe_load(COMPOSE.read_text())["services"]


def _dramatiq_services():
    """Every service running a dramatiq worker, mapped to its argv list."""
    out = {}
    for name, spec in _services().items():
        cmd = spec.get("command")
        if isinstance(cmd, list) and cmd and cmd[0] == "dramatiq":
            out[name] = cmd
    return out


def _consumed_queues(argv):
    """Mirror argparse's `--queues [QUEUES ...]`: every token until the next flag."""
    if "--queues" not in argv:
        return set()          # no --queues means "listen to all queues"
    tail = argv[argv.index("--queues") + 1:]
    queues = []
    for token in tail:
        if token.startswith("-"):
            break
        queues.append(token)
    return set(queues)


def test_compose_defines_dramatiq_workers():
    assert _dramatiq_services(), "no dramatiq workers found in docker-compose.yml"


def test_queue_names_are_separate_arguments():
    """dramatiq splits on spaces, never on commas."""
    for name, argv in _dramatiq_services().items():
        for queue in _consumed_queues(argv):
            assert "," not in queue, (
                f"{name} passes {queue!r} to --queues. argparse takes this as a "
                f"single queue name; use separate arguments instead."
            )


def test_every_queue_name_is_real():
    known = ALL_QUEUES
    for name, argv in _dramatiq_services().items():
        for queue in _consumed_queues(argv):
            assert queue in known, (
                f"{name} listens on unknown queue {queue!r}; "
                f"nothing sends to it. Known queues: {sorted(known)}"
            )


def test_every_queue_has_a_consumer():
    """The failure this file exists for: an actor whose queue nobody drains."""
    consumed = set()
    for argv in _dramatiq_services().values():
        queues = _consumed_queues(argv)
        if not queues:
            return          # a catch-all worker drains everything
        consumed |= queues
    missing = ALL_QUEUES - consumed
    assert not missing, (
        f"no worker consumes {sorted(missing)} — jobs on these queues would "
        f"stay pending forever while every container reports healthy"
    )


def test_control_is_not_starved_by_scrapers():
    """Fan-out must not queue behind slow checks.

    `control` turns an accepted job into its individual checks, so any delay
    here is time the user spends watching "Queued". Sharing a container with
    scrapers means inheriting their latency; on the single-threaded protected
    worker that is a 45s WAF check per fan-out.
    """
    for name, argv in _dramatiq_services().items():
        queues = _consumed_queues(argv)
        if config.QUEUE_CONTROL in queues:
            assert queues == {config.QUEUE_CONTROL}, (
                f"{name} consumes control alongside {sorted(queues - {config.QUEUE_CONTROL})}; "
                f"fan-out would wait behind scrape work"
            )
