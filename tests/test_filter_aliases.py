"""Tests for filter-name canonicalization.

The matcher joins frames to masters on a plain string equality, so the
canonical form has to be deterministic and consistent across both sides.
These tests pin the cross-scope aliases that we explicitly care about so a
future edit to ``ALIASES`` can't silently break matching.
"""

from __future__ import annotations

import pytest

from server.catalog.filter_aliases import canonicalize


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Dwarf 3 IR-cut and clear collapse with Seestar's IRCUT/LP.
        ("Astro", "None"),
        ("VIS", "None"),
        ("IRCUT", "None"),
        ("IR-Cut", "None"),
        ("LP", "None"),
        ("Clear", "None"),
        ("None", "None"),
        # Dual-band: Dwarf 3 light is "Duo", flat master under CALI_FRAME is
        # "Duo-Band". Both must resolve to the same canonical so the matcher
        # actually joins them.
        ("Duo", "HaOIII"),
        ("Duo-Band", "HaOIII"),
        ("DuoBand", "HaOIII"),
        ("HaOIII", "HaOIII"),
        ("HaO3", "HaOIII"),
        ("Ha-OIII", "HaOIII"),
        # Single narrowband.
        ("Ha", "Ha"),
        ("H-alpha", "Ha"),
        ("HAlpha", "Ha"),
        ("OIII", "OIII"),
        ("O3", "OIII"),
        ("SII", "SII"),
        ("S2", "SII"),
        # LRGB.
        ("L", "L"),
        ("Luminance", "L"),
        ("R", "R"),
    ],
)
def test_known_aliases_canonicalize(raw: str, expected: str) -> None:
    assert canonicalize(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["Astro", "astro", "ASTRO", "  Astro  ", "as-tro", "as_tro"],
)
def test_normalization_ignores_case_whitespace_punctuation(raw: str) -> None:
    assert canonicalize(raw) == "None"


def test_empty_and_none_round_trip() -> None:
    assert canonicalize(None) is None
    assert canonicalize("") is None
    assert canonicalize("   ") is None


def test_unknown_filter_passes_through_unchanged() -> None:
    """An unknown filter is preserved verbatim so we never silently lose data."""
    assert canonicalize("Antlia ALP-T") == "Antlia ALP-T"
    assert canonicalize("MyCustomFilter") == "MyCustomFilter"
