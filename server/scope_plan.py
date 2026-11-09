"""Choose which target the scope should shoot now."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import astropy.units as u
from astropy.coordinates import EarthLocation, SkyCoord, get_body
from astropy.time import Time

from server import sky

MIN_ALT_DEG = 30.0
MOON_LIMIT_DEG = 10.0
LOOKAHEAD_MIN = 20


@dataclass(frozen=True)
class Candidate:
    name: str
    ra_deg: float
    dec_deg: float
    object_type: str | None = None
    filter: str | None = None
    exposure_s: int | None = None
    gain: int | None = None


DEFAULT_EXPOSURE_S = 15
DEFAULT_GAIN = 60
DUO_BAND_TYPES = frozenset(
    {
        "Emission Nebula",
        "H II Region",
        "Nebula",
        "Cluster with Nebula",
        "Planetary Nebula",
        "Supernova Remnant",
    }
)


@dataclass(frozen=True)
class CaptureSettings:
    filter: str
    exposure_s: int
    gain: int


def capture_settings(c: Candidate) -> CaptureSettings:
    """Per-target overrides win. Gas nebulae default to duo-band, everything else to astro."""
    default_filter = "duo" if c.object_type in DUO_BAND_TYPES else "astro"
    return CaptureSettings(
        c.filter or default_filter,
        c.exposure_s or DEFAULT_EXPOSURE_S,
        c.gain if c.gain is not None else DEFAULT_GAIN,
    )


@dataclass(frozen=True)
class Pick:
    target: Candidate
    alt_deg: float


def in_window(loc: EarthLocation, now: datetime) -> bool:
    window = sky.twilight_window(loc, now)
    if window is None:
        return False
    dusk, dawn = window
    return dusk <= sky._ensure_utc(now) <= dawn


def moon_separation_deg(c: Candidate, loc: EarthLocation, at: datetime) -> float:
    t = Time(sky._ensure_utc(at), scale="utc")
    moon = get_body("moon", t, loc)
    target = SkyCoord(ra=c.ra_deg * u.deg, dec=c.dec_deg * u.deg, frame="icrs")
    return float(target.separation(moon).deg)


def _alt(c: Candidate, loc: EarthLocation, at: datetime) -> float:
    return sky.alt_az_now(c.ra_deg, c.dec_deg, loc, at)[0]


def usable(
    c: Candidate, loc: EarthLocation, now: datetime, min_alt: float = MIN_ALT_DEG
) -> float | None:
    """Return the altitude, or None when low, setting soon, or near the moon."""
    alt = _alt(c, loc, now)
    if alt < min_alt:
        return None
    if _alt(c, loc, now + timedelta(minutes=LOOKAHEAD_MIN)) < min_alt:
        return None
    if moon_separation_deg(c, loc, now) < MOON_LIMIT_DEG:
        return None
    return alt


def pick_target(
    candidates: list[Candidate],
    loc: EarthLocation,
    now: datetime,
    current: str | None = None,
    skip: frozenset[str] = frozenset(),
    min_alt: float = MIN_ALT_DEG,
) -> Pick | None:
    """Keep the current target while it stays usable. Otherwise take the highest one."""
    usable_now = []
    for c in candidates:
        if c.name in skip:
            continue
        alt = usable(c, loc, now, min_alt)
        if alt is not None:
            usable_now.append(Pick(c, alt))
    if not usable_now:
        return None
    for p in usable_now:
        if p.target.name == current:
            return p
    return max(usable_now, key=lambda p: p.alt_deg)
