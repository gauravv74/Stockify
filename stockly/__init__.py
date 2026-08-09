"""Stockly internals for the queued execution model.

Legacy top-level modules (``app``, ``auth``, ``jobs``, ``watches``, ``config``)
are intentionally left where they are; this package holds the new job/queue,
shared check-execution and observability code so the two can evolve
independently during the migration.
"""
