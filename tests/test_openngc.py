"""Tests for `server.catalog.openngc.enrich` alias resolution.

The alias index has to bridge several naming conventions:
- canonical id forms ('NGC 7000' / 'NGC7000')
- Messier xref ('M 31' / 'M31')
- Caldwell / Sh2 / LBN identifiers from the OpenNGC `Identifiers` column,
  which OpenNGC stores zero-padded ('C 020', 'LBN 0373') even though
  humans (and FITS headers) write the unpadded form ('C 20', 'LBN 373').

These tests pin the unpadded resolution so the Tonight planner's
captured-overlay join keeps working when a user's library stores a
target under a different alias than OpenNGC's canonical row.
"""

from __future__ import annotations

import pytest

from server.catalog.openngc import enrich


@pytest.mark.parametrize(
    "query",
    ["C 20", "C20", "C020", "c 20", "c20"],
)
def test_caldwell_alias_resolves_to_ngc(query: str) -> None:
    """North America Nebula: catalog row is NGC 7000, but a Dwarf 3 user
    might have it filed as C 20 (Caldwell 20). Both forms must hit the
    same row so the captured-overlay shows the user's sessions."""
    entry = enrich(query)
    assert entry is not None, f"{query!r} should resolve"
    assert entry.canonical == "NGC 7000"
    assert entry.common_name == "North America Nebula"


@pytest.mark.parametrize(
    "query",
    ["LBN 373", "LBN373"],
)
def test_lbn_alias_resolves(query: str) -> None:
    """LBN identifiers are stored unpadded in OpenNGC ('LBN 373'), unlike
    Caldwell which is zero-padded ('C 020'). Both forms still need to
    resolve so a FITS header that wrote the joined form lands on the
    same row."""
    entry = enrich(query)
    assert entry is not None, f"{query!r} should resolve"
    assert entry.canonical == "NGC 7000"


def test_sh2_alias_keeps_hyphen() -> None:
    """Sh2-155 has a hyphen between prefix and number; the unpadded form
    must still resolve, but the joined-without-space form ('SH2155') is
    not a thing humans write, so we don't bother indexing it."""
    entry = enrich("Sh2-155")
    assert entry is not None
    assert entry.canonical == "C 9"


def test_messier_xref_resolves() -> None:
    entry = enrich("M 31")
    assert entry is not None
    assert entry.canonical == "NGC 224"


def test_unknown_name_returns_none() -> None:
    assert enrich("not a real catalog id") is None
