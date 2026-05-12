"""Filter-name canonicalization.

Different capture programs write the same physical filter under different
strings. Dwarf 3 calls its IR-cut filter ``Astro``; Seestar writes ``IRCUT``
or ``LP``. The Dwarf 3 dual-band slot reports ``Duo`` in shotsInfo but flat
masters under ``CALI_FRAME/`` are filed as ``Duo-Band``. None of that
matters to the matcher, which joins ``frames.filter`` to ``masters.filter``
with a string equality - so we normalize both at scanner write time.

Canonical forms are chosen to match the most common written convention
(``HaOIII``, ``L``, ``R``, ``Ha``, etc.). The ``None`` value is a literal
string, not Python ``None``: it tags a frame that was captured with no
narrowband filter in the optical path, including OSC captures behind an
IR-cut element where the IR-cut is conceptually "the no-filter baseline".

To add an alias, drop a new entry into ``ALIASES``. The lookup is
case-insensitive and ignores whitespace/punctuation, so ``Ha-OIII`` and
``HAOIII`` resolve identically without separate entries.
"""

from __future__ import annotations

import re

_NORMALIZE_RE = re.compile(r"[\s_\-]+")


def _key(name: str) -> str:
    return _NORMALIZE_RE.sub("", name).lower()


# Canonical-name -> list of accepted aliases. The canonical name itself is
# included in the alias list so the lookup table built below covers it too.
ALIASES: dict[str, list[str]] = {
    # Clear / IR-cut / OSC-baseline.
    "None": ["None", "Astro", "IRCUT", "IR-Cut", "LP", "VIS", "Clear", "Open", "L-Pro"],
    # Mono LRGB.
    "L": ["L", "Lum", "Luminance"],
    "R": ["R", "Red"],
    "G": ["G", "Green"],
    "B": ["B", "Blue"],
    # Single narrowband.
    "Ha": ["Ha", "H-alpha", "Halpha", "HAlpha"],
    "OIII": ["OIII", "O3"],
    "SII": ["SII", "S2"],
    "Hb": ["Hb", "H-beta", "Hbeta"],
    # Dual narrowband (Ha + OIII). "Duo" is the Dwarf 3 name; "Duo-Band" is
    # what their CALI_FRAME masters are filed under.
    "HaOIII": ["HaOIII", "HaO3", "Ha-OIII", "Duo", "Duo-Band", "DuoBand", "DualBand"],
    # Alt dual narrowband (SII + Hb).
    "SIIHb": ["SIIHb", "S2Hb", "SII-Hb"],
    # Alt dual narrowband (SII + OIII).
    "SIIOIII": ["SIIOIII", "S2O3", "SII-OIII"],
}


_LOOKUP: dict[str, str] = {}
for canonical, names in ALIASES.items():
    for n in names:
        _LOOKUP[_key(n)] = canonical


def canonicalize(name: str | None) -> str | None:
    """Map a filter string to its canonical form, or pass it through unchanged.

    Returns ``None`` only when the input is ``None`` or empty. An unknown
    filter string is returned unchanged so we never silently lose data; it
    just won't match aliased entries in the masters table until someone
    teaches ``ALIASES`` about it.
    """
    if name is None:
        return None
    stripped = name.strip()
    if not stripped:
        return None
    return _LOOKUP.get(_key(stripped), stripped)
