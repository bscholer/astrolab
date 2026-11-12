"""Choose which target the scope should shoot now."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from astropy.coordinates import EarthLocation, get_body
from astropy.time import Time

from server import sky

MIN_ALT_DEG = 30.0

# True where the scope is known not to see the sky, by altitude and azimuth.
Blocked = Callable[[float, float], bool]
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
    goal_hours: float | None = None


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


# The night window stays the same from when it was worked out until its dawn, so the
# scheduler's once-a-minute check reuses it instead of solving for the Sun each time.
_window_cache: dict[tuple[float, float], tuple[datetime, tuple[datetime, datetime] | None]] = {}
MOON_CACHE_S = 600
_moon_cache: dict[tuple[int, float, float], tuple[float, float]] = {}


def in_window(loc: EarthLocation, now: datetime) -> bool:
    now = sky._ensure_utc(now)
    key = (float(loc.lat.deg), float(loc.lon.deg))
    cached = _window_cache.get(key)
    if cached and cached[1] and cached[0] <= now <= cached[1][1]:
        window = cached[1]
    else:
        window = sky.twilight_window(loc, now)
        _window_cache[key] = (now, window)
    if window is None:
        return False
    dusk, dawn = window
    return dusk <= now <= dawn


def _moon_radec(loc: EarthLocation, at: datetime) -> tuple[float, float]:
    """The Moon's RA and Dec, worked out once per ten minutes. It moves about 0.1 degree."""
    key = (int(at.timestamp() // MOON_CACHE_S), float(loc.lat.deg), float(loc.lon.deg))
    if key not in _moon_cache:
        if len(_moon_cache) > 512:
            _moon_cache.clear()
        t = Time(datetime.fromtimestamp(key[0] * MOON_CACHE_S, UTC), scale="utc")
        # GCRS, as seen from the site. Its axes line up with ICRS, so its RA and Dec compare
        # directly with a target's. Converting the Moon to ICRS itself would move it to the
        # solar system barycentre's point of view, which for something this close is wrong.
        moon = get_body("moon", t, loc)
        _moon_cache[key] = (float(moon.ra.deg), float(moon.dec.deg))
    return _moon_cache[key]


def moon_separation_deg(c: Candidate, loc: EarthLocation, at: datetime) -> float:
    ra, dec = _moon_radec(loc, sky._ensure_utc(at))
    r1, d1, r2, d2 = (math.radians(v) for v in (c.ra_deg, c.dec_deg, ra, dec))
    cos = math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(r1 - r2)
    return math.degrees(math.acos(max(-1.0, min(1.0, cos))))


def _clear(c: Candidate, loc: EarthLocation, at: datetime, min_alt: float, blocked) -> float | None:
    alt, az = sky.alt_az_now(c.ra_deg, c.dec_deg, loc, at)
    if alt < min_alt or (blocked is not None and blocked(alt, az)):
        return None
    return alt


def usable(
    c: Candidate,
    loc: EarthLocation,
    now: datetime,
    min_alt: float = MIN_ALT_DEG,
    blocked: Blocked | None = None,
) -> float | None:
    """Return the altitude, or None when low, behind something, setting soon, or near the moon."""
    alt = _clear(c, loc, now, min_alt, blocked)
    if alt is None:
        return None
    if _clear(c, loc, now + timedelta(minutes=LOOKAHEAD_MIN), min_alt, blocked) is None:
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
    blocked: Blocked | None = None,
) -> Pick | None:
    """Keep the current target while it stays usable. Otherwise take the highest one."""
    usable_now = []
    for c in candidates:
        if c.name in skip:
            continue
        alt = usable(c, loc, now, min_alt, blocked)
        if alt is not None:
            usable_now.append(Pick(c, alt))
    if not usable_now:
        return None
    for p in usable_now:
        if p.target.name == current:
            return p
    return max(usable_now, key=lambda p: p.alt_deg)
