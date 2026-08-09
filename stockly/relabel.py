#!/usr/bin/env python3
"""Refresh cached pincode labels in bulk.

Labels improve lazily — a pincode is relabelled the next time a check needs it —
so a cache built under older rules catches up only as searches happen. Run this
once after deploying a labelling change to make it visible everywhere at once:

    python -m stockly.relabel

Coordinates are never modified, so this is safe to re-run and safe to interrupt.
"""

from __future__ import annotations

import sys

from stockly import geo


def _progress(i, total, pincode, before, after):
    changed = "  " if before == after else "->"
    print(f"[{i:>4}/{total}] {pincode} {changed} {after!r}", flush=True)
    if before != after:
        print(f"           was {before!r}", flush=True)


def main():
    geo.init_db()
    stale = geo.stale_label_pincodes()
    if not stale:
        print(f"All cached labels are already at version {geo.LABEL_VERSION}.")
        return 0

    print(f"Relabelling {len(stale)} pincode(s) to version {geo.LABEL_VERSION}…")
    summary = geo.backfill_labels(progress=_progress)
    print(f"\nrelabelled {summary['relabelled']}, failed {summary['failed']}, "
          f"of {summary['total']}")
    if summary["failed"]:
        print("Failed lookups keep their previous label and retry on next use.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
