"""Position-based target resolution.

The scanner stores OBJECT (the user's chosen name) and RA/Dec/FOV info for
every frame. When the OBJECT doesn't enrich via OpenNGC's name index ("My
weird target 3"), we still want to map the target to a catalog row so the
Tonight planner's captured-only overlay works. This module does the
sky-position fallback: build an envelope from the target's frames, then
pick the nearest OpenNGC entry within a tolerance scoped to the FOV.

Public surface:

  frame_fov_diagonal_deg(focallen_mm, xpixsz_um, ypixsz_um, naxis1, naxis2)
      -> degrees, or None when any input is missing.
  target_envelope(frames)
      -> (centroid_ra_deg, centroid_dec_deg, envelope_deg) or None.
  angular_separation_deg(ra1, dec1, ra2, dec2)
      -> great-circle separation in degrees.
  nearest_match(ra_deg, dec_deg, tolerance_deg, catalog=None)
      -> closest CatalogMatch within tolerance, else None.
  nearby_matches(ra_deg, dec_deg, tolerance_deg, limit=20, catalog=None)
      -> ascending list of CatalogMatch within tolerance, capped at limit.

`nearest_match` is the auto-resolve path (no popularity tie-break: just
distance). `nearby_matches` powers the UI's override dropdown and bumps
named entries within the same arcminute bucket.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .openngc import CatalogEntry, all_entries


@dataclass(frozen=True)
class CatalogMatch:
    """A candidate OpenNGC row within tolerance of a query position.

    `separation_deg` is the great-circle distance between the query and
    the catalog entry; `nearby_matches` sorts by this, and `nearest_match`
    returns the smallest. `object_type` and `common_name` are carried
    here so the UI can render rich dropdown options without a second
    lookup."""

    canonical: str
    common_name: str | None
    object_type: str | None
    separation_deg: float


def frame_fov_diagonal_deg(
    focallen_mm: float | None,
    xpixsz_um: float | None,
    ypixsz_um: float | None,
    naxis1: int | None,
    naxis2: int | None,
) -> float | None:
    """Frame diagonal field of view in degrees, or None when inputs are short.

    Pixel scale = max(xpixsz_um, ypixsz_um) / focallen_mm * 206.265 arcsec.
    Diagonal FOV = hypot(naxis1, naxis2) * pixel_scale arcsec, divided by
    3600 to get degrees. Using the larger of the two pixel sizes biases
    pessimistic (slightly bigger envelope), which is fine here because
    we want the tolerance to comfortably cover the frame.
    """
    if (
        focallen_mm is None
        or xpixsz_um is None
        or ypixsz_um is None
        or naxis1 is None
        or naxis2 is None
    ):
        return None
    try:
        focallen = float(focallen_mm)
        xp = float(xpixsz_um)
        yp = float(ypixsz_um)
        n1 = float(naxis1)
        n2 = float(naxis2)
    except (TypeError, ValueError):
        return None
    if focallen <= 0 or n1 <= 0 or n2 <= 0:
        return None
    pixel_scale_arcsec = (max(xp, yp) / focallen) * 206.265
    diag_arcsec = math.hypot(n1, n2) * pixel_scale_arcsec
    return diag_arcsec / 3600.0


@dataclass(frozen=True)
class FrameSky:
    """Minimal per-frame sky info `target_envelope` consumes.

    `fov_diagonal_deg` may be None when the frame's headers lacked the
    focallen / pixsz / NAXIS triplet. Frames missing RA or Dec are
    silently dropped by `target_envelope`."""

    ra_deg: float | None
    dec_deg: float | None
    fov_diagonal_deg: float | None


_MIN_FRAMES_FOR_ENVELOPE = 3
"""Below this, the envelope isn't trustworthy. Two frames at opposite
ends of a long mosaic legitimately produce a wide envelope, but a single
frame's centroid can be off by half a FOV (which we already account for)
without telling us anything about cross-frame spread. Three is the
smallest count where median-style robustness starts paying off."""


def target_centroid(
    frames: Iterable[FrameSky],
) -> tuple[float, float] | None:
    """Circular-mean RA + median Dec across frames with usable RA/Dec.

    Returns None when fewer than `_MIN_FRAMES_FOR_ENVELOPE` frames have
    RA *and* Dec, or when the RA cos/sin means cancel (frames spread
    symmetrically around the sky, a degenerate case we don't engineer
    for).

    RA averaging happens on the unit circle so a target near the 0/360
    seam doesn't fold to RA=180. Dec uses median for robustness against
    an outlier panel.
    """
    usable: list[FrameSky] = [
        f for f in frames if f.ra_deg is not None and f.dec_deg is not None
    ]
    if len(usable) < _MIN_FRAMES_FOR_ENVELOPE:
        return None
    ra_rad = [math.radians(f.ra_deg) for f in usable]  # type: ignore[arg-type]
    cos_mean = sum(math.cos(r) for r in ra_rad) / len(ra_rad)
    sin_mean = sum(math.sin(r) for r in ra_rad) / len(ra_rad)
    if cos_mean == 0.0 and sin_mean == 0.0:
        return None
    centroid_ra = math.degrees(math.atan2(sin_mean, cos_mean)) % 360.0
    decs = sorted(float(f.dec_deg) for f in usable)  # type: ignore[arg-type]
    n = len(decs)
    centroid_dec = (
        decs[n // 2] if n % 2 == 1 else 0.5 * (decs[n // 2 - 1] + decs[n // 2])
    )
    return centroid_ra, centroid_dec


def target_envelope(
    frames: Iterable[FrameSky],
) -> tuple[float, float, float] | None:
    """Compute (centroid_ra_deg, centroid_dec_deg, envelope_deg) over frames.

    Returns None when fewer than `_MIN_FRAMES_FOR_ENVELOPE` frames have
    usable RA/Dec, or when every frame is missing FOV inputs (we need at
    least one FOV reading to extend the envelope past the centroid by
    half a frame). Callers wanting just a centroid (and willing to fall
    back to a fixed tolerance) should call `target_centroid` directly.
    """
    frames_list = list(frames)
    centroid = target_centroid(frames_list)
    if centroid is None:
        return None
    centroid_ra, centroid_dec = centroid

    # Envelope = max per-frame separation from centroid, plus half the
    # frame diagonal so a single-frame target gets a meaningful radius.
    # When every frame lacks FOV info, return None and let the caller
    # decide whether to use a fixed fallback tolerance.
    usable: list[FrameSky] = [
        f for f in frames_list if f.ra_deg is not None and f.dec_deg is not None
    ]
    max_sep = 0.0
    for f in usable:
        sep = angular_separation_deg(
            centroid_ra,
            centroid_dec,
            float(f.ra_deg),  # type: ignore[arg-type]
            float(f.dec_deg),  # type: ignore[arg-type]
        )
        if sep > max_sep:
            max_sep = sep
    fovs = sorted(
        f.fov_diagonal_deg
        for f in usable
        if f.fov_diagonal_deg is not None and f.fov_diagonal_deg > 0
    )
    if not fovs:
        return None
    fm = fovs[len(fovs) // 2]
    envelope = max_sep + fm / 2.0
    return centroid_ra, centroid_dec, envelope


def angular_separation_deg(
    ra1_deg: float, dec1_deg: float, ra2_deg: float, dec2_deg: float
) -> float:
    """Vincenty great-circle distance in degrees.

    Numerically stable across all separations (small *and* near-pi), and
    handles RA wraparound by virtue of operating on cos/sin of the
    coordinates rather than raw subtraction.
    """
    phi1 = math.radians(dec1_deg)
    phi2 = math.radians(dec2_deg)
    dlam = math.radians(ra2_deg - ra1_deg)
    sin_phi1, cos_phi1 = math.sin(phi1), math.cos(phi1)
    sin_phi2, cos_phi2 = math.sin(phi2), math.cos(phi2)
    sin_dlam, cos_dlam = math.sin(dlam), math.cos(dlam)
    num = math.hypot(
        cos_phi2 * sin_dlam,
        cos_phi1 * sin_phi2 - sin_phi1 * cos_phi2 * cos_dlam,
    )
    den = sin_phi1 * sin_phi2 + cos_phi1 * cos_phi2 * cos_dlam
    return math.degrees(math.atan2(num, den))


def _filter_with_position(
    catalog: Iterable[CatalogEntry] | None,
) -> list[CatalogEntry]:
    """Materialize the catalog and drop rows with no sky position.

    We snap to a list because both nearest_match and nearby_matches walk
    the catalog multiple times in the worst case, and `all_entries()`
    already returns a cached list so this is essentially free.
    """
    entries = list(all_entries() if catalog is None else catalog)
    return [e for e in entries if e.ra_deg is not None and e.dec_deg is not None]


def nearest_match(
    ra_deg: float,
    dec_deg: float,
    tolerance_deg: float,
    catalog: Iterable[CatalogEntry] | None = None,
) -> CatalogMatch | None:
    """Closest catalog entry within tolerance, or None.

    No popularity tie-break: the user wanted the obvious thing. Two
    catalog rows tied within float-rounding is vanishingly rare in
    practice (OpenNGC positions differ at the sub-arcsecond level even
    for adjacent entries) and engineering for it would just bake an
    arbitrary order in.
    """
    if tolerance_deg <= 0:
        return None
    best: CatalogMatch | None = None
    for e in _filter_with_position(catalog):
        sep = angular_separation_deg(ra_deg, dec_deg, e.ra_deg, e.dec_deg)  # type: ignore[arg-type]
        if sep > tolerance_deg:
            continue
        if best is None or sep < best.separation_deg:
            best = CatalogMatch(
                canonical=e.canonical,
                common_name=e.common_name,
                object_type=e.object_type,
                separation_deg=sep,
            )
    return best


def nearby_matches(
    ra_deg: float,
    dec_deg: float,
    tolerance_deg: float,
    limit: int = 20,
    catalog: Iterable[CatalogEntry] | None = None,
) -> list[CatalogMatch]:
    """All catalog entries within tolerance, sorted by ascending separation.

    Within an arcminute "tie bucket" we float entries that have a
    common_name above unnamed ones. The bucket is rounded to the nearest
    arcminute (round(sep_arcmin)) so two rows separated by 12.4 vs 12.6
    arcmin land in the same bucket and the named one wins. Outside ties,
    distance dominates.

    This is used only for the suggestion dropdown; auto-resolve never
    sees this list.
    """
    if tolerance_deg <= 0 or limit <= 0:
        return []
    raw: list[CatalogMatch] = []
    for e in _filter_with_position(catalog):
        sep = angular_separation_deg(ra_deg, dec_deg, e.ra_deg, e.dec_deg)  # type: ignore[arg-type]
        if sep > tolerance_deg:
            continue
        raw.append(
            CatalogMatch(
                canonical=e.canonical,
                common_name=e.common_name,
                object_type=e.object_type,
                separation_deg=sep,
            )
        )
    # Within each arcminute tie-bucket, named entries float above unnamed.
    # Outside the bucket, separation dominates. The bucket key (rounded
    # arcmin) plus (0 for named, 1 for unnamed) plus the raw separation
    # plus the canonical id (stable tie-breaker) is enough.
    def sort_key(m: CatalogMatch) -> tuple[int, int, float, str]:
        bucket = round(m.separation_deg * 60.0)
        named = 0 if m.common_name else 1
        return (bucket, named, m.separation_deg, m.canonical)

    raw.sort(key=sort_key)
    return raw[:limit]


# Tolerance clamps. Auto-match deliberately runs tighter than the
# dropdown suggestions: we only want to auto-claim something the user
# obviously intended, but we want to *show* the user every plausible
# nearby candidate when they're picking by hand.
AUTO_MIN_TOL_DEG = 0.3
AUTO_MAX_TOL_DEG = 3.0
AUTO_FALLBACK_TOL_DEG = 1.0
SUGGEST_MIN_TOL_DEG = 0.5
SUGGEST_MAX_TOL_DEG = 5.0
SUGGEST_FALLBACK_TOL_DEG = 2.0


def auto_tolerance_deg(envelope_deg: float | None) -> float:
    """Pick the auto-match tolerance for a target.

    Clamp envelope * 1.0 into [0.3, 3.0]. When envelope is unknown (no
    FOV headers across the entire target), fall back to 1.0 deg, wide
    enough to catch a Dwarf 3 single-frame, narrow enough that we don't
    accidentally claim a neighboring object."""
    if envelope_deg is None or envelope_deg <= 0:
        return AUTO_FALLBACK_TOL_DEG
    return min(AUTO_MAX_TOL_DEG, max(AUTO_MIN_TOL_DEG, envelope_deg))


def suggest_tolerance_deg(envelope_deg: float | None) -> float:
    """Pick the suggestion-dropdown tolerance for a target.

    Wider than auto (1.5x envelope, clamped to [0.5, 5.0]) so the UI
    can show plausible alternatives the auto-resolver wouldn't pick on
    its own, useful when the target name is wrong AND the user's
    capture drifted near a different obvious DSO."""
    if envelope_deg is None or envelope_deg <= 0:
        return SUGGEST_FALLBACK_TOL_DEG
    return min(SUGGEST_MAX_TOL_DEG, max(SUGGEST_MIN_TOL_DEG, envelope_deg * 1.5))


def frames_to_sky(frames: Sequence[dict]) -> list[FrameSky]:
    """Adapter: lift a list of dict rows (frames table joined with header)
    into FrameSky records. `frames` rows must carry ra, dec, focallen,
    xpixsz, ypixsz, naxis1, naxis2 keys (None where missing). Convenience
    wrapper for the scanner so it doesn't construct FrameSky inline."""
    out: list[FrameSky] = []
    for row in frames:
        fov = frame_fov_diagonal_deg(
            row.get("focallen"),
            row.get("xpixsz"),
            row.get("ypixsz"),
            row.get("naxis1"),
            row.get("naxis2"),
        )
        out.append(
            FrameSky(
                ra_deg=row.get("ra"),
                dec_deg=row.get("dec"),
                fov_diagonal_deg=fov,
            )
        )
    return out


__all__ = [
    "AUTO_FALLBACK_TOL_DEG",
    "AUTO_MAX_TOL_DEG",
    "AUTO_MIN_TOL_DEG",
    "SUGGEST_FALLBACK_TOL_DEG",
    "SUGGEST_MAX_TOL_DEG",
    "SUGGEST_MIN_TOL_DEG",
    "CatalogMatch",
    "FrameSky",
    "angular_separation_deg",
    "auto_tolerance_deg",
    "frame_fov_diagonal_deg",
    "frames_to_sky",
    "nearby_matches",
    "nearest_match",
    "suggest_tolerance_deg",
    "target_centroid",
    "target_envelope",
]
