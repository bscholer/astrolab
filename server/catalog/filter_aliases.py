"""Filter-name canonicalization.

The matcher joins ``frames.filter`` to ``masters.filter`` with a plain
string equality, so any two strings that denote the same physical filter
must be rewritten to the same canonical form on write. This module is
the rewrite step.

Scope: minimal. Only entries we've verified against real capture data
live here. The single case that matters today:

- The Dwarf 3 Ha+OIII dual-band slot. Lights write ``FILTER=Duo`` (from
  the device's own app vocabulary); factory flat masters under
  ``CALI_FRAME/flat/cam_*/`` are labelled ``Duo-Band`` in the Dwarf
  documentation. A NINA or ASIAIR user with the same physical filter
  would label it ``HaOIII`` or ``HaO3``. All four refer to the same
  dual-band Ha+OIII filter and must collapse so a captured-elsewhere
  flat library could in principle match Dwarf 3 lights, and so the UI
  shows one filter name rather than four.

Case, embedded whitespace, hyphens, and underscores are all collapsed in
the lookup key so callers don't need to enumerate every spelling
(``HaOiii`` and ``Ha-OIII`` and ``ha oiii`` all hit the same entry).
Unknown filter strings pass through with whitespace stripped but
otherwise unchanged — the UI shows the user the label their FITS header
produced.
"""

from __future__ import annotations

import re

_NORMALIZE_RE = re.compile(r"[\s_\-]+")


def _key(name: str) -> str:
    return _NORMALIZE_RE.sub("", name).lower()


# Canonical-name -> list of accepted source spellings. Add entries only
# when two strings have been observed denoting the same physical filter
# in real capture data; don't speculate.
ALIASES: dict[str, list[str]] = {
    # Ha + OIII dual-band. "Duo" is what Dwarf 3 lights write; "Duo-Band"
    # is the Dwarf docs label used on factory flat masters under
    # CALI_FRAME/. "HaO3" is the same filter in alternate astronomical
    # notation. Confirmed against codegistics' Dwarf 3 sample data via
    # starbash issue geeksville/starbash#1.
    "HaOIII": ["HaOIII", "HaO3", "Duo", "Duo-Band"],
}


_LOOKUP: dict[str, str] = {}
for canonical, names in ALIASES.items():
    for n in names:
        _LOOKUP[_key(n)] = canonical


def canonicalize(name: str | None) -> str | None:
    """Map a filter string to its canonical form, or pass it through unchanged.

    Returns Python ``None`` when the input is ``None`` or empty. Unknown
    filter strings are returned with whitespace stripped but otherwise
    untouched, so the UI shows the label the FITS header produced.
    """
    if name is None:
        return None
    stripped = name.strip()
    if not stripped:
        return None
    return _LOOKUP.get(_key(stripped), stripped)
