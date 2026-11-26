"""Curated ranking of the targets people most want to image.

Catalog fields cannot tell the Heart Nebula from a dull open cluster, so the
Tonight ranking starts from this list.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

from server.catalog.openngc import enrich

DATA_PATH = Path(__file__).parent / "data" / "showpieces.json"


@cache
def _ranks() -> dict[str, int]:
    names = json.loads(DATA_PATH.read_text(encoding="utf-8"))["targets"]
    ranks: dict[str, int] = {}
    for i, name in enumerate(names):
        entry = enrich(name)
        if entry is not None:
            ranks.setdefault(entry.canonical, i)
    return ranks


def fraction(canonical: str) -> float | None:
    """Position in the list, from 0 for the best target to 1 for the last.
    None for a target that is not on the list."""
    ranks = _ranks()
    rank = ranks.get(canonical)
    return None if rank is None else rank / max(len(ranks) - 1, 1)
