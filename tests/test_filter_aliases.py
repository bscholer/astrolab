"""Tests for filter-name canonicalization.

The matcher joins frames to masters on plain string equality, so the
canonical form has to be deterministic and consistent across both sides.
"""

from __future__ import annotations

import pytest

from server.catalog.filter_aliases import canonicalize


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Dwarf 3 dual-band: lights write "Duo", factory flats are labelled
        # "Duo-Band" in the Dwarf docs. Same physical filter as a NINA
        # user's "HaOIII"; all must resolve to one canonical so the
        # matcher can join across them.
        ("Duo", "HaOIII"),
        ("Duo-Band", "HaOIII"),
        ("HaOIII", "HaOIII"),
        ("HaOiii", "HaOIII"),  # case-insensitive
        ("Ha-OIII", "HaOIII"),  # punctuation-insensitive
        ("HaO3", "HaOIII"),  # alternate notation
    ],
)
def test_known_aliases_canonicalize(raw: str, expected: str) -> None:
    assert canonicalize(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["Duo", "duo", "DUO", "  Duo  ", "Du-o", "Du_o"],
)
def test_normalization_ignores_case_whitespace_punctuation(raw: str) -> None:
    """Case and inline punctuation get folded in the lookup key, so a
    capture program that writes 'duo' or 'Du-o' lands on the same
    canonical as the standard spelling."""
    assert canonicalize(raw) == "HaOIII"


def test_empty_and_none_round_trip() -> None:
    assert canonicalize(None) is None
    assert canonicalize("") is None
    assert canonicalize("   ") is None


@pytest.mark.parametrize(
    "raw",
    [
        # Dwarf 3 IR-cut and visible filters are not in the alias table;
        # they pass through verbatim so the UI shows the user the name
        # their device wrote into the FITS header.
        "Astro",
        "VIS",
        # Seestar's filters likewise.
        "IRCUT",
        "LP",
        # NINA users get to keep their custom filter names.
        "Antlia ALP-T",
        "L-eXtreme",
        "MyCustomFilter",
    ],
)
def test_unaliased_filters_pass_through_unchanged(raw: str) -> None:
    """An unknown filter is preserved verbatim. We deliberately do NOT
    collapse cross-scope synonyms (e.g. Dwarf 3's "Astro" IR-cut and
    Seestar's "IRCUT") because they belong to different scopes and the
    matcher already filters on ``instrument``; folding them would lose
    information without enabling any real cross-scope match."""
    assert canonicalize(raw) == raw
