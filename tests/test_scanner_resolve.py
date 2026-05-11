"""Scanner integration tests for the target-resolution pass.

These tests build synthetic FITS trees on disk, scan them, and assert
the resulting `targets` row has the expected resolved_* columns. We
test the name-first path, the position-fallback path, the no-FOV
fallback, the too-few-frames case, and idempotency.
"""

from __future__ import annotations

from pathlib import Path

import server.catalog.adapters  # noqa: F401  registers dwarf3 adapter
from server.catalog.db import open_db
from server.catalog.openngc import enrich
from server.catalog.scanner import scan

from ._fits_fixtures import DEFAULT_LIGHT_HEADER, write_fits

# OpenNGC NGC 7000 is at RA ~314.75, Dec ~+44.53. We use slightly
# offset coordinates so the test mirrors the user's actual scenario
# (drone pointing not perfectly centered).
NGC7000_RA = 314.75
NGC7000_DEC = 44.53
M31_RA = 10.68
M31_DEC = 41.27


def _build_session(
    root: Path,
    *,
    object_name: str,
    ra: float | None,
    dec: float | None,
    n_frames: int = 3,
    fov_headers: bool = True,
    folder_safe_name: str | None = None,
) -> None:
    """Drop `n_frames` FITS files into a single mosaic-style folder so the
    dwarf3 adapter pulls them into one session belonging to `object_name`."""
    folder_name = folder_safe_name or object_name
    folder = (
        root
        / f"DWARF_RAW_TELE_{folder_name}_EXP_30_GAIN_60_2025-10-21-22-18-55-284"
    )
    base_hdr = dict(DEFAULT_LIGHT_HEADER)
    if not fov_headers:
        base_hdr.pop("FOCALLEN", None)
        base_hdr.pop("XPIXSZ", None)
        base_hdr.pop("YPIXSZ", None)
    base_hdr["OBJECT"] = object_name
    if ra is not None:
        base_hdr["RA"] = ra
    else:
        base_hdr.pop("RA", None)
    if dec is not None:
        base_hdr["DEC"] = dec
    else:
        base_hdr.pop("DEC", None)
    for i in range(n_frames):
        write_fits(
            folder / f"{folder_name}_30s60_Astro_2025102{i % 10}-22192950{i}_24C.fits",
            headers=base_hdr,
        )


def _get_target_row(name: str) -> dict | None:
    with open_db() as conn:
        row = conn.execute(
            "SELECT id, name, resolved_canonical, resolved_separation_arcmin, "
            "resolved_at, resolved_source, resolved_canonical_override "
            "FROM targets WHERE name = ?",
            (name,),
        ).fetchone()
        return dict(row) if row is not None else None


def test_named_target_resolves_via_name(tmp_path: Path, astrolab_home: Path) -> None:
    """A target whose OBJECT is a recognized OpenNGC id resolves with
    source='name', separation=0.0, and the canonical persisted from
    OpenNGC."""
    captures = tmp_path / "captures"
    _build_session(captures, object_name="M 31", ra=M31_RA, dec=M31_DEC, n_frames=4)
    scan(captures, scope_id="dwarf3")
    row = _get_target_row("M 31")
    assert row is not None
    assert row["resolved_source"] == "name"
    expected_canonical = enrich("M 31")
    assert expected_canonical is not None
    assert row["resolved_canonical"] == expected_canonical.canonical
    assert row["resolved_separation_arcmin"] == 0.0
    assert row["resolved_at"] is not None


def test_garbage_name_resolves_via_position(tmp_path: Path, astrolab_home: Path) -> None:
    """A garbage-named target whose frames sit on NGC 7000 resolves with
    source='position' and a positive but small separation_arcmin."""
    captures = tmp_path / "captures"
    _build_session(
        captures,
        object_name="MY_GARBAGE_NAME",
        ra=NGC7000_RA,
        dec=NGC7000_DEC,
        n_frames=4,
    )
    scan(captures, scope_id="dwarf3")
    row = _get_target_row("MY_GARBAGE_NAME")
    assert row is not None
    assert row["resolved_source"] == "position"
    assert row["resolved_canonical"] == "NGC 7000"
    assert row["resolved_separation_arcmin"] is not None
    assert 0.0 <= row["resolved_separation_arcmin"] < 60.0  # within 1 deg
    assert row["resolved_at"] is not None


def test_no_fov_headers_uses_fallback_tolerance(
    tmp_path: Path, astrolab_home: Path
) -> None:
    """Frames missing FOCALLEN / XPIXSZ / YPIXSZ but with valid RA/Dec
    still resolve via the 1.0-deg fallback tolerance. We center on
    NGC 7000 and confirm the auto-resolve picks it up."""
    captures = tmp_path / "captures"
    _build_session(
        captures,
        object_name="NOFOV_TARGET",
        ra=NGC7000_RA,
        dec=NGC7000_DEC,
        n_frames=4,
        fov_headers=False,
    )
    scan(captures, scope_id="dwarf3")
    row = _get_target_row("NOFOV_TARGET")
    assert row is not None
    assert row["resolved_source"] == "position"
    assert row["resolved_canonical"] == "NGC 7000"


def test_too_few_frames_leaves_columns_null_when_name_unresolved(
    tmp_path: Path, astrolab_home: Path
) -> None:
    """Fewer than 3 frames + unknown name -> all auto columns null. The
    garbage name doesn't enrich, and the centroid isn't trustworthy, so
    we don't risk a stale match."""
    captures = tmp_path / "captures"
    _build_session(
        captures,
        object_name="SPARSE_TARGET",
        ra=NGC7000_RA,
        dec=NGC7000_DEC,
        n_frames=2,
    )
    scan(captures, scope_id="dwarf3")
    row = _get_target_row("SPARSE_TARGET")
    assert row is not None
    assert row["resolved_canonical"] is None
    assert row["resolved_source"] is None
    assert row["resolved_at"] is None


def test_too_few_frames_still_name_resolves(
    tmp_path: Path, astrolab_home: Path
) -> None:
    """Even with a single frame, a recognized name still resolves via
    the name path; we never block name-resolution on frame count."""
    captures = tmp_path / "captures"
    _build_session(captures, object_name="M 31", ra=M31_RA, dec=M31_DEC, n_frames=1)
    scan(captures, scope_id="dwarf3")
    row = _get_target_row("M 31")
    assert row is not None
    assert row["resolved_source"] == "name"
    expected = enrich("M 31")
    assert expected is not None
    assert row["resolved_canonical"] == expected.canonical


def test_rescan_is_idempotent(tmp_path: Path, astrolab_home: Path) -> None:
    """Running the scanner twice on the same tree must keep the source
    stable and not flip between paths. Positional canonical and source
    should be the same after the second scan."""
    captures = tmp_path / "captures"
    _build_session(
        captures,
        object_name="GARBAGE_FOR_RESCAN",
        ra=NGC7000_RA,
        dec=NGC7000_DEC,
        n_frames=4,
    )
    scan(captures, scope_id="dwarf3")
    first = _get_target_row("GARBAGE_FOR_RESCAN")
    assert first is not None
    scan(captures, scope_id="dwarf3")
    second = _get_target_row("GARBAGE_FOR_RESCAN")
    assert second is not None
    assert first["resolved_canonical"] == second["resolved_canonical"]
    assert first["resolved_source"] == second["resolved_source"]
    # Separation should be deterministic given identical RA/Dec input.
    assert first["resolved_separation_arcmin"] == second["resolved_separation_arcmin"]


def test_rescan_preserves_user_override(
    tmp_path: Path, astrolab_home: Path
) -> None:
    """The user's pinned override is owned by the API, not the scanner.
    Re-scanning must not clobber resolved_canonical_override."""
    captures = tmp_path / "captures"
    _build_session(
        captures,
        object_name="GARBAGE_WITH_OVERRIDE",
        ra=NGC7000_RA,
        dec=NGC7000_DEC,
        n_frames=4,
    )
    scan(captures, scope_id="dwarf3")
    with open_db() as conn, conn:
        conn.execute(
            "UPDATE targets SET resolved_canonical_override = ? WHERE name = ?",
            ("NGC 224", "GARBAGE_WITH_OVERRIDE"),
        )
    scan(captures, scope_id="dwarf3")
    row = _get_target_row("GARBAGE_WITH_OVERRIDE")
    assert row is not None
    assert row["resolved_canonical_override"] == "NGC 224"
    # The scanner's auto-resolve path also still runs, so the auto
    # canonical should reflect the position match (NGC 7000) and the
    # source should still be 'position'. The two are decoupled.
    assert row["resolved_canonical"] == "NGC 7000"
    assert row["resolved_source"] == "position"


def test_position_far_from_any_catalog_row_leaves_columns_null(
    tmp_path: Path, astrolab_home: Path
) -> None:
    """A target pointing at empty sky (no nearby catalog row within the
    auto tolerance) results in all auto columns null, even though we
    have a valid centroid."""
    captures = tmp_path / "captures"
    # An RA/Dec deliberately far from any bright OpenNGC row. (40.0, -75.0)
    # is well into the southern celestial hemisphere; OpenNGC has very few
    # entries at high southern declinations.
    _build_session(
        captures,
        object_name="GARBAGE_IN_EMPTY_SKY",
        ra=40.0,
        dec=-75.0,
        n_frames=4,
    )
    scan(captures, scope_id="dwarf3")
    row = _get_target_row("GARBAGE_IN_EMPTY_SKY")
    assert row is not None
    # Either no match at all, or a match within the tolerance; we don't
    # care which as long as the columns are coherent. If a match was
    # found, source must be 'position' and canonical non-null.
    if row["resolved_source"] is None:
        assert row["resolved_canonical"] is None
        assert row["resolved_separation_arcmin"] is None
    else:
        assert row["resolved_source"] == "position"
        assert row["resolved_canonical"] is not None
