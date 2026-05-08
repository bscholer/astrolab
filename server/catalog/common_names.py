"""Common/popular names for deep-sky targets.

Looked up in a bundled JSON of catalog id to display name. Curated, not
exhaustive; targets without an entry just don't get a pretty name. The
intent is to make the Library view scannable ('Wizard Nebula' beats
'NGC 7380' at a glance), not to be a complete catalog.

Future: add an optional astroquery/SIMBAD path to resolve unmatched names
on first sight, populating the targets.aliases column. Today's curated
file is enough to make the demo readable.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

DATA_PATH = Path(__file__).parent / "data" / "common_names.json"


@cache
def _names() -> dict[str, str]:
    raw = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def normalize(name: str) -> str:
    """Collapse whitespace runs to a single space and strip the result.

    Lookup table keys use the canonical 'M 33' form (one space). FITS
    headers and folder names sometimes carry odd spacing; normalize so
    'M  33' or 'M33  ' all resolve.
    """
    return " ".join(name.split())


def lookup(name: str | None) -> str | None:
    """Return a common name for `name` if known, else None.

    Tries the literal canonicalized name first, then a few common rewrites
    so that 'M33' (no space) and 'NGC7380' (no space) still resolve.
    """
    if not name:
        return None
    table = _names()
    canonical = normalize(name)
    if canonical in table:
        return table[canonical]
    # Insert a space after the catalog prefix when none is present.
    for prefix in ("M", "C", "NGC", "IC", "B", "Sh2-"):
        if canonical.startswith(prefix):
            rest = canonical[len(prefix):].lstrip()
            if rest and rest != canonical[len(prefix):]:
                continue  # already had whitespace handled by canonical
            spaced = f"{prefix} {rest}".strip()
            if spaced in table:
                return table[spaced]
    return None
