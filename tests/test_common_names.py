"""Common-name lookup tests."""

from __future__ import annotations

from server.catalog.common_names import lookup, normalize


def test_messier_lookup() -> None:
    assert lookup("M 33") == "Triangulum Galaxy"
    assert lookup("M 31") == "Andromeda Galaxy"
    assert lookup("M 42") == "Orion Nebula"


def test_caldwell_lookup() -> None:
    assert lookup("C 34") == "Western Veil Nebula"
    assert lookup("C 20") == "North America Nebula"


def test_ngc_and_ic_lookup() -> None:
    assert lookup("NGC 7380") == "Wizard Nebula"
    assert lookup("IC 1805") == "Heart Nebula"
    assert lookup("IC 1848") == "Soul Nebula"


def test_unknown_returns_none() -> None:
    assert lookup("HD 173764") is None
    assert lookup("Moon") is None
    assert lookup("") is None
    assert lookup(None) is None


def test_extra_whitespace_normalized() -> None:
    assert lookup("M  33") == "Triangulum Galaxy"
    assert lookup("  NGC 7380  ") == "Wizard Nebula"


def test_normalize_collapses_runs() -> None:
    assert normalize("  M   33  ") == "M 33"
    assert normalize("NGC 7380") == "NGC 7380"
