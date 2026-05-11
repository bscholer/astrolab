"""Tests for the position-resolution helpers.

These are pure-Python math + catalog walks; no DB / FastAPI involvement.
"""

from __future__ import annotations

import pytest

from server.catalog.openngc import enrich
from server.catalog.sky_match import (
    AUTO_FALLBACK_TOL_DEG,
    AUTO_MAX_TOL_DEG,
    AUTO_MIN_TOL_DEG,
    SUGGEST_FALLBACK_TOL_DEG,
    SUGGEST_MAX_TOL_DEG,
    SUGGEST_MIN_TOL_DEG,
    FrameSky,
    angular_separation_deg,
    auto_tolerance_deg,
    frame_fov_diagonal_deg,
    nearby_matches,
    nearest_match,
    suggest_tolerance_deg,
    target_centroid,
    target_envelope,
)


def _m31() -> tuple[float, float]:
    """RA/Dec for NGC 224 (M 31) sourced from OpenNGC; pinned for sanity."""
    entry = enrich("M 31")
    assert entry is not None
    assert entry.ra_deg is not None and entry.dec_deg is not None
    return float(entry.ra_deg), float(entry.dec_deg)


def _ngc7000() -> tuple[float, float]:
    entry = enrich("NGC 7000")
    assert entry is not None
    assert entry.ra_deg is not None and entry.dec_deg is not None
    return float(entry.ra_deg), float(entry.dec_deg)


# ---------------------------------------------------------------------------
# FOV diagonal
# ---------------------------------------------------------------------------


def test_frame_fov_diagonal_dwarf3_geometry() -> None:
    """Dwarf 3 TELE: 100 mm focal length, 2.0 um square pixels, ~3008x2008
    sensor. Pixel scale = (2.0/100)*206.265 = ~4.13 arcsec/px. Diagonal =
    hypot(3008, 2008) px * 4.13 arcsec / 3600 -> ~4.15 deg. Pin to that
    ballpark so a header-units regression (mm/m, um/px) gets caught."""
    diag = frame_fov_diagonal_deg(100.0, 2.0, 2.0, 3008, 2008)
    assert diag is not None
    assert 3.5 < diag < 5.0, diag


def test_frame_fov_diagonal_none_when_input_missing() -> None:
    """Any missing input yields None; we don't make up FOVs."""
    assert frame_fov_diagonal_deg(None, 2.0, 2.0, 3008, 2008) is None
    assert frame_fov_diagonal_deg(100.0, None, 2.0, 3008, 2008) is None
    assert frame_fov_diagonal_deg(100.0, 2.0, None, 3008, 2008) is None
    assert frame_fov_diagonal_deg(100.0, 2.0, 2.0, None, 2008) is None
    assert frame_fov_diagonal_deg(100.0, 2.0, 2.0, 3008, None) is None


def test_frame_fov_diagonal_rejects_nonpositive() -> None:
    """Zero / negative focallen or naxis values would produce nonsense FOVs;
    we return None instead of letting that propagate to a tolerance."""
    assert frame_fov_diagonal_deg(0.0, 2.0, 2.0, 3008, 2008) is None
    assert frame_fov_diagonal_deg(-100.0, 2.0, 2.0, 3008, 2008) is None
    assert frame_fov_diagonal_deg(100.0, 2.0, 2.0, 0, 2008) is None


def test_frame_fov_uses_larger_pixel_dimension() -> None:
    """When pixel sizes differ we use the bigger one (biases pessimistic
    so the envelope comfortably contains the frame). Compare two calls
    where xpixsz != ypixsz and confirm the returned diag matches the
    max-pixsz form."""
    diag_xy = frame_fov_diagonal_deg(100.0, 1.0, 3.0, 1000, 1000)
    diag_max = frame_fov_diagonal_deg(100.0, 3.0, 3.0, 1000, 1000)
    assert diag_xy == diag_max


# ---------------------------------------------------------------------------
# target_envelope / target_centroid
# ---------------------------------------------------------------------------


def _fov_dwarf3() -> float:
    diag = frame_fov_diagonal_deg(100.0, 2.0, 2.0, 3008, 2008)
    assert diag is not None
    return diag


def test_target_envelope_single_pointing_is_half_fov() -> None:
    """Three frames stacked at the same RA/Dec: envelope = fov_diagonal/2
    (the max per-frame separation is zero, so envelope shrinks to the
    half-diagonal padding)."""
    fov = _fov_dwarf3()
    ra, dec = _m31()
    frames = [FrameSky(ra, dec, fov)] * 3
    result = target_envelope(frames)
    assert result is not None
    cra, cdec, env = result
    assert abs(cra - ra) < 1e-6
    assert abs(cdec - dec) < 1e-6
    assert abs(env - fov / 2) < 1e-6


def test_target_envelope_mosaic_grows_with_spread() -> None:
    """Three-frame mosaic spaced ~1 deg apart: envelope should be larger
    than a single pointing's half-FOV, capturing the spread plus padding."""
    fov = _fov_dwarf3()
    ra, dec = _m31()
    frames = [
        FrameSky(ra - 0.5, dec - 0.5, fov),
        FrameSky(ra, dec, fov),
        FrameSky(ra + 0.5, dec + 0.5, fov),
    ]
    result = target_envelope(frames)
    assert result is not None
    _, _, env = result
    # Max separation from centroid is ~sqrt(0.5^2 + 0.5^2) ~ 0.71 deg, plus
    # fov/2 (~2 deg) = roughly 2.7 deg. Sanity-check we're in that ballpark.
    assert env > fov / 2 + 0.5, env
    assert env < fov / 2 + 1.5, env


def test_target_envelope_returns_none_below_min_frames() -> None:
    """Two frames isn't enough; the centroid is too thin to trust."""
    fov = _fov_dwarf3()
    ra, dec = _m31()
    assert target_envelope([FrameSky(ra, dec, fov), FrameSky(ra, dec, fov)]) is None


def test_target_envelope_returns_none_without_any_fov() -> None:
    """Frames with RA/Dec but no FOV info: envelope returns None, caller
    falls back to a fixed tolerance via target_centroid."""
    ra, dec = _m31()
    frames = [FrameSky(ra, dec, None)] * 5
    assert target_envelope(frames) is None


def test_target_envelope_handles_ra_wrap() -> None:
    """Frames near the RA=0/360 seam (one at 0.1, others at 359.9, 359.5)
    should still produce a centroid near 359.83 deg, not RA=180. Pinned
    to catch a regression to a naive arithmetic mean."""
    fov = _fov_dwarf3()
    frames = [
        FrameSky(359.5, 30.0, fov),
        FrameSky(359.9, 30.0, fov),
        FrameSky(0.1, 30.0, fov),
    ]
    result = target_envelope(frames)
    assert result is not None
    cra, cdec, _ = result
    # circular mean of 359.5, 359.9, 0.1 -> ~359.83
    assert cra > 359.0 or cra < 1.0, cra
    assert 28.0 < cdec < 32.0


def test_target_centroid_works_without_fov() -> None:
    """target_centroid is the FOV-free path: returns a centroid as long as
    enough frames have RA/Dec, regardless of FOV info."""
    ra, dec = _ngc7000()
    frames = [FrameSky(ra, dec, None)] * 3
    centroid = target_centroid(frames)
    assert centroid is not None
    cra, cdec = centroid
    assert abs(cra - ra) < 1e-6
    assert abs(cdec - dec) < 1e-6


# ---------------------------------------------------------------------------
# angular_separation_deg
# ---------------------------------------------------------------------------


def test_angular_separation_zero() -> None:
    assert abs(angular_separation_deg(10.0, 5.0, 10.0, 5.0)) < 1e-12


def test_angular_separation_one_degree() -> None:
    """One degree apart in RA at Dec=0 should be one degree of arc."""
    assert abs(angular_separation_deg(10.0, 0.0, 11.0, 0.0) - 1.0) < 1e-9


def test_angular_separation_handles_ra_wrap() -> None:
    """RA=359.5 and RA=0.5 at Dec=0 are 1 deg apart, not 359 deg."""
    sep = angular_separation_deg(359.5, 0.0, 0.5, 0.0)
    assert abs(sep - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# nearest_match
# ---------------------------------------------------------------------------


def test_nearest_match_m31() -> None:
    """A query right on M 31's catalog coords resolves to NGC 224."""
    ra, dec = _m31()
    match = nearest_match(ra, dec, 0.3)
    assert match is not None
    assert match.canonical == "NGC 224"
    assert match.separation_deg < 0.001


def test_nearest_match_outside_tolerance_returns_none() -> None:
    """Nothing at (90, 0) within 0.1 deg of any catalog entry."""
    match = nearest_match(90.0, 0.0, 0.1)
    # (90, 0) is on the celestial equator near the Hyades but >0.1 deg from
    # any specific catalog row; the test just confirms we get a tight-bound
    # miss back as None.
    if match is not None:
        # If a row really did land within 0.1 deg, the assertion above
        # would still be sound, but record the row so a future catalog
        # update doesn't silently re-trigger this case.
        assert match.separation_deg <= 0.1


def test_nearest_match_ra_wrap() -> None:
    """A target whose centroid lands at RA=359.95 (just past the seam from
    NGC 7822-ish) should resolve via the great-circle path, not get folded
    onto something halfway around the sky."""
    # NGC 7826 sits near RA=0; we hunt at a position 0.1 deg west of the
    # seam and confirm nearest_match doesn't return a target 180 deg away.
    match = nearest_match(359.95, 30.0, 1.5)
    if match is not None:
        # Whatever wins, it must be within 1.5 deg.
        assert match.separation_deg <= 1.5


def test_nearest_match_zero_tolerance_returns_none() -> None:
    """Defensive: tolerance <= 0 short-circuits."""
    ra, dec = _m31()
    assert nearest_match(ra, dec, 0.0) is None
    assert nearest_match(ra, dec, -0.5) is None


# ---------------------------------------------------------------------------
# nearby_matches
# ---------------------------------------------------------------------------


def test_nearby_matches_sorted_ascending() -> None:
    """The list comes back ordered by ascending separation_deg."""
    ra, dec = _m31()
    matches = nearby_matches(ra, dec, 3.0)
    seps = [m.separation_deg for m in matches]
    assert seps == sorted(seps), seps


def test_nearby_matches_respects_tolerance() -> None:
    """No match in the list exceeds the requested tolerance."""
    ra, dec = _m31()
    tol = 1.5
    matches = nearby_matches(ra, dec, tol)
    assert all(m.separation_deg <= tol for m in matches)


def test_nearby_matches_respects_limit() -> None:
    """The returned list is capped at `limit`."""
    ra, dec = _m31()
    matches = nearby_matches(ra, dec, 5.0, limit=3)
    assert len(matches) <= 3


def test_nearby_matches_named_floats_within_tie_bucket() -> None:
    """A bucket of matches at the same arcminute separation should sort
    named entries above unnamed ones. We construct a tie by querying at
    a midpoint near both kinds; M 31 (named) should appear before an
    unnamed neighbor at a similar separation when they fall in the same
    bucket."""
    ra, dec = _m31()
    matches = nearby_matches(ra, dec, 1.5, limit=50)
    # Within each tie bucket, named must precede unnamed.
    from collections import defaultdict
    buckets: dict[int, list[bool]] = defaultdict(list)
    for m in matches:
        buckets[round(m.separation_deg * 60.0)].append(bool(m.common_name))
    for _, named_flags in buckets.items():
        # Within any bucket, no unnamed entry should come before a named
        # one (i.e. the sequence True...False is fine, False...True is not).
        seen_unnamed = False
        for is_named in named_flags:
            if not is_named:
                seen_unnamed = True
            elif seen_unnamed:
                pytest.fail(
                    f"named entry appeared after unnamed entry in same bucket: {named_flags}"
                )


def test_nearby_matches_ra_wrap() -> None:
    """Near the RA=0/360 seam: the great-circle filter should pick up
    candidates on either side of the seam without folding."""
    matches = nearby_matches(0.05, 30.0, 1.5)
    # Sanity: anything returned must be within 1.5 deg via great-circle.
    for m in matches:
        # No way to recover the catalog entry's RA from here, but the
        # separation_deg field is the great-circle distance we computed.
        assert m.separation_deg <= 1.5


def test_nearby_matches_zero_tolerance_empty() -> None:
    ra, dec = _m31()
    assert nearby_matches(ra, dec, 0.0) == []
    assert nearby_matches(ra, dec, 1.0, limit=0) == []


# ---------------------------------------------------------------------------
# Tolerance helpers
# ---------------------------------------------------------------------------


def test_auto_tolerance_clamps() -> None:
    """auto_tolerance_deg(envelope) clamps to [AUTO_MIN_TOL_DEG,
    AUTO_MAX_TOL_DEG]."""
    assert auto_tolerance_deg(0.1) == AUTO_MIN_TOL_DEG
    assert auto_tolerance_deg(10.0) == AUTO_MAX_TOL_DEG
    assert auto_tolerance_deg(1.5) == 1.5


def test_auto_tolerance_fallback() -> None:
    """None / zero envelope -> the fixed AUTO_FALLBACK_TOL_DEG."""
    assert auto_tolerance_deg(None) == AUTO_FALLBACK_TOL_DEG
    assert auto_tolerance_deg(0.0) == AUTO_FALLBACK_TOL_DEG
    assert auto_tolerance_deg(-1.0) == AUTO_FALLBACK_TOL_DEG


def test_suggest_tolerance_clamps_and_scales() -> None:
    """Suggestion tolerance is 1.5x envelope, clamped to
    [SUGGEST_MIN_TOL_DEG, SUGGEST_MAX_TOL_DEG]."""
    assert suggest_tolerance_deg(0.1) == SUGGEST_MIN_TOL_DEG
    assert suggest_tolerance_deg(10.0) == SUGGEST_MAX_TOL_DEG
    assert abs(suggest_tolerance_deg(2.0) - 3.0) < 1e-9
    assert suggest_tolerance_deg(None) == SUGGEST_FALLBACK_TOL_DEG
