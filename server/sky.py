"""Sky math for the Tonight planner.

Pure astropy. No network calls, no IERS auto-downloads; we suppress the
'recent leap second / IERS too old' tracker globally because the
planner's accuracy budget is comfortably wider than the worst-case error
from an out-of-date IERS table (sub-arcsecond). The module is import-
order safe: importing it does not touch the network even on a fresh
machine.

Public surface, in the order callers reach for it:

  observer_location(lat, lon, elev) -> EarthLocation
  twilight_window(loc, at) -> (dusk_utc, dawn_utc)
  alt_az_now(ra, dec, loc, at) -> (alt_deg, az_deg)
  transit_in_window(ra, dec, loc, dusk, dawn) -> datetime | None
  hours_above(ra, dec, loc, dusk, dawn, min_alt) -> float

`at` accepts a tz-aware or naive datetime; naive is treated as UTC. All
returned datetimes are UTC, tz-aware. Coordinates are J2000 / ICRS,
which matches what OpenNGC stores; we don't re-precess to JNow because
the planner's resolution (degrees) is two orders of magnitude wider
than the difference.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
from astropy import units as u
from astropy.coordinates import AltAz, EarthLocation, SkyCoord, get_sun
from astropy.time import Time
from astropy.utils import iers

# Astropy nags about out-of-date IERS tables and tries to download fresh
# data. We don't need the precision (sub-arcsecond corrections), and
# we don't want a dev-laptop offline to fail the whole import. Use the
# bundled IERS table, no network.
iers.conf.auto_download = False
iers.conf.iers_auto_url = ""
iers.conf.auto_max_age = None


# Solar altitude that defines astronomical twilight. The Tonight window
# uses astronomical (-18 deg) when achievable, falling back to nautical
# (-12 deg) and civil (-6 deg) at high latitudes where the sun never
# sinks 18 deg below the horizon in summer. Picking the deepest darkness
# the night supports keeps the visible-target list honest at extreme
# latitudes instead of silently returning an empty window.
_TWILIGHT_THRESHOLDS_DEG = (-18.0, -12.0, -6.0)


@dataclass(frozen=True)
class TonightVisibility:
    """Per-target output of the Tonight computation.

    `transit_utc` is None when the target doesn't transit during the
    night window (circumpolar lower-culmination, or it transits during
    daylight). In that case the UI just shows `alt_now_deg` and the
    user decides whether the current altitude is good enough."""

    alt_now_deg: float
    az_now_deg: float
    transit_utc: datetime | None
    hours_above_min_alt: float


def observer_location(lat_deg: float, lon_deg: float, elev_m: float) -> EarthLocation:
    """Build an `EarthLocation` from the user's site config.

    Validates the bounds the API layer should also have caught; raising
    ValueError here is defense in depth so a buggy caller doesn't sail
    past with lat=200 and silently produce nonsense alt values."""
    if not -90.0 <= lat_deg <= 90.0:
        raise ValueError(f"latitude {lat_deg} out of range (-90..90)")
    if not -180.0 <= lon_deg <= 180.0:
        raise ValueError(f"longitude {lon_deg} out of range (-180..180)")
    return EarthLocation(
        lat=lat_deg * u.deg,
        lon=lon_deg * u.deg,
        height=elev_m * u.m,
    )


def _ensure_utc(at: datetime) -> datetime:
    if at.tzinfo is None:
        return at.replace(tzinfo=UTC)
    return at.astimezone(UTC)


def _local_midnight_utc(loc: EarthLocation, at: datetime) -> datetime:
    """Return the UTC instant of the local midnight closest to `at`.

    "Closest" not "previous": when the user opens Tonight at 22:00
    local, they mean the upcoming night, not last night. Anchoring at
    nearest local midnight makes both 22:00-Tuesday and 02:00-Wednesday
    pick the same Tuesday-night window.

    Approximation: longitude / 15 hours offset from UTC (no DST, no
    political tz). Good enough; the search routines below are robust to
    a couple-hour anchor slop."""
    at_utc = _ensure_utc(at)
    offset_hours = float(loc.lon.to(u.deg).value) / 15.0
    local = at_utc + timedelta(hours=offset_hours)
    floor_midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    ceil_midnight = floor_midnight + timedelta(days=1)
    nearest_local = (
        floor_midnight
        if abs(local - floor_midnight) <= abs(ceil_midnight - local)
        else ceil_midnight
    )
    return nearest_local - timedelta(hours=offset_hours)


def _sun_alt(loc: EarthLocation, times: Time) -> np.ndarray:
    """Sun altitude in degrees at each Time sample, vectorized.

    We sample at low cadence (5 min) over a 24h window, so this is the
    one allocation-heavy step in twilight detection. Pulled out for
    profiling clarity."""
    altaz_frame = AltAz(obstime=times, location=loc)
    sun = get_sun(times).transform_to(altaz_frame)  # type: ignore[union-attr]
    return np.asarray(sun.alt.to(u.deg).value)  # type: ignore[union-attr]


def twilight_window(
    loc: EarthLocation, at: datetime
) -> tuple[datetime, datetime] | None:
    """Find the night window centered on the local night for `at`.

    Returns (dusk_utc, dawn_utc) where the sun crosses the deepest
    achievable twilight threshold. Falls back from astronomical to
    nautical to civil twilight when the deeper threshold is never
    reached (high-latitude summer). Returns None when even civil
    twilight isn't reached, i.e. true polar day, so the caller can
    render the page with no transit times.

    Sampled at 5-minute resolution then linearly interpolated for the
    crossing, accurate to ~30s, well under the planner's needs."""
    midnight = _local_midnight_utc(loc, at)
    # 24h window centered on local midnight: search before for dusk and
    # after for dawn. 5-minute step keeps the linear interpolation honest.
    start = midnight - timedelta(hours=12)
    n_samples = int(24 * 60 / 5) + 1
    times = Time(
        [start + timedelta(minutes=5 * i) for i in range(n_samples)],
        scale="utc",
    )
    sun_alt = _sun_alt(loc, times)

    midnight_idx = n_samples // 2

    for threshold in _TWILIGHT_THRESHOLDS_DEG:
        # Dusk: latest time before midnight where the sun crosses below threshold.
        dusk: datetime | None = None
        for i in range(midnight_idx, 0, -1):
            if sun_alt[i] <= threshold and sun_alt[i - 1] > threshold:
                # Linear interp inside the [i-1, i] bracket.
                frac = (sun_alt[i - 1] - threshold) / (
                    sun_alt[i - 1] - sun_alt[i]
                )
                dusk = (
                    times[i - 1].to_datetime(timezone=UTC)  # type: ignore[union-attr]
                    + timedelta(minutes=5) * float(frac)
                )
                break

        # Dawn: earliest time after midnight where the sun crosses back above threshold.
        dawn: datetime | None = None
        for i in range(midnight_idx, n_samples - 1):
            if sun_alt[i] <= threshold and sun_alt[i + 1] > threshold:
                frac = (threshold - sun_alt[i]) / (sun_alt[i + 1] - sun_alt[i])
                dawn = (
                    times[i].to_datetime(timezone=UTC)  # type: ignore[union-attr]
                    + timedelta(minutes=5) * float(frac)
                )
                break

        if dusk is not None and dawn is not None and dawn > dusk:
            return dusk, dawn

    # Pure polar night (sun stays below civil twilight all 24h) is also
    # a valid "night" that we should return as a 24h window so targets
    # remain visible in the planner. Detect by sun never exceeding the
    # civil threshold.
    if float(np.max(sun_alt)) <= -6.0:
        return (
            times[0].to_datetime(timezone=UTC),  # type: ignore[union-attr]
            times[-1].to_datetime(timezone=UTC),  # type: ignore[union-attr]
        )

    return None


def alt_az_now(
    ra_deg: float, dec_deg: float, loc: EarthLocation, at: datetime
) -> tuple[float, float]:
    """Single-shot alt/az for a target, in degrees. ICRS -> AltAz at `at`.

    Azimuth uses the astronomical convention (0 = north, 90 = east)
    that astropy returns by default; consumers downstream of the API
    just display it, so we don't normalize."""
    at_utc = _ensure_utc(at)
    t = Time(at_utc, scale="utc")
    target = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame="icrs")
    altaz = target.transform_to(AltAz(obstime=t, location=loc))  # type: ignore[union-attr]
    return (
        float(altaz.alt.to(u.deg).value),  # type: ignore[union-attr]
        float(altaz.az.to(u.deg).value),  # type: ignore[union-attr]
    )


def alt_az_batch(
    ra_deg: np.ndarray,
    dec_deg: np.ndarray,
    loc: EarthLocation,
    at: datetime,
) -> tuple[np.ndarray, np.ndarray]:
    """Vectorized alt/az for many targets at one instant.

    Order of magnitude faster than calling `alt_az_now` per target on
    the full OpenNGC catalog (~14k entries). Returns parallel arrays of
    alt and az in degrees, matching the input ordering."""
    at_utc = _ensure_utc(at)
    t = Time(at_utc, scale="utc")
    targets = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame="icrs")
    altaz = targets.transform_to(AltAz(obstime=t, location=loc))  # type: ignore[union-attr]
    return (
        np.asarray(altaz.alt.to(u.deg).value),  # type: ignore[union-attr]
        np.asarray(altaz.az.to(u.deg).value),  # type: ignore[union-attr]
    )


def transit_in_window(
    ra_deg: float,
    dec_deg: float,
    loc: EarthLocation,
    dusk_utc: datetime,
    dawn_utc: datetime,
) -> datetime | None:
    """Return the target's transit (upper culmination) inside the night
    window, or None if it doesn't transit between dusk and dawn.

    Transit math: the hour angle is zero at upper culmination, so we
    sample LST across the window and pick the moment closest to RA.
    This handles RA wrap (LST sweeps mod 360) implicitly; we look at
    the angular delta as a signed quantity in (-180, 180] and find the
    sign change. Sampling at 1-minute resolution is plenty: transits
    are smooth, the alt second-derivative is tiny near culmination."""
    dusk = _ensure_utc(dusk_utc)
    dawn = _ensure_utc(dawn_utc)
    if dawn <= dusk:
        return None

    minutes = int(math.ceil((dawn - dusk).total_seconds() / 60))
    times = Time(
        [dusk + timedelta(minutes=i) for i in range(minutes + 1)],
        scale="utc",
    )
    # LST at the site, in degrees. Subtract RA so a sign change marks transit.
    lst_deg = np.asarray(times.sidereal_time("apparent", longitude=loc.lon).to(u.deg).value)
    delta = ((lst_deg - ra_deg + 540.0) % 360.0) - 180.0  # signed (-180, 180]
    # Find the first index where delta goes from negative to positive
    # (or hits zero exactly). Using a forward sweep gives us the FIRST
    # transit in the window if the target somehow culminates twice
    # (sidereal year vs civil day mismatch over a >23h window).
    sign = np.sign(delta)
    for i in range(len(sign) - 1):
        if sign[i] < 0 <= sign[i + 1]:
            # Linear interp between the bracketing samples for sub-minute
            # precision.
            d0, d1 = delta[i], delta[i + 1]
            frac = -d0 / (d1 - d0) if d1 != d0 else 0.0
            return dusk + timedelta(minutes=i) + timedelta(minutes=1) * float(frac)
        if delta[i] == 0.0:
            return dusk + timedelta(minutes=i)
    return None


def hours_above(
    ra_deg: float,
    dec_deg: float,
    loc: EarthLocation,
    dusk_utc: datetime,
    dawn_utc: datetime,
    min_alt_deg: float,
) -> float:
    """Hours during the night window where the target is above `min_alt_deg`.

    Sampled at 5-minute resolution (~0.083 hr). Plenty for a 'is this
    target up tonight' display; finer resolution wouldn't show in the
    UI. Returns 0.0 when the target never clears the threshold."""
    dusk = _ensure_utc(dusk_utc)
    dawn = _ensure_utc(dawn_utc)
    if dawn <= dusk:
        return 0.0
    step_min = 5
    n = int(math.ceil((dawn - dusk).total_seconds() / 60 / step_min)) + 1
    times = Time(
        [dusk + timedelta(minutes=step_min * i) for i in range(n)],
        scale="utc",
    )
    target = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame="icrs")
    altaz = target.transform_to(AltAz(obstime=times, location=loc))  # type: ignore[union-attr]
    alts = np.asarray(altaz.alt.to(u.deg).value)  # type: ignore[union-attr]
    above = int(np.sum(alts >= min_alt_deg))
    return above * (step_min / 60.0)


# Sparkline cadence for the alt curve we hand to the UI. The internal
# transit/hours math is at 5 min; we hand back every 6th sample (30 min)
# because a sparkline rendered into ~120px of horizontal space can't
# show finer detail anyway, and the JSON shrinks 6x.
SPARKLINE_STEP_MIN = 30
_INTERNAL_STEP_MIN = 5
_SPARKLINE_STRIDE = SPARKLINE_STEP_MIN // _INTERNAL_STEP_MIN


def night_transits_and_hours(
    ra_deg: np.ndarray,
    dec_deg: np.ndarray,
    loc: EarthLocation,
    dusk_utc: datetime,
    dawn_utc: datetime,
    min_alt_deg: float,
) -> tuple[list[datetime | None], np.ndarray, np.ndarray]:
    """Vectorized transit + hours-above + alt curve for many targets.

    The Tonight planner walks ~1500 candidates per request; calling the
    per-target helpers in a loop costs ~10ms each (the AltAz transform
    re-runs astropy's setup machinery every time). This routine runs
    the alt-sample transform once for all targets across all sample
    times, then folds out per-target metrics with cheap numpy slicing.

    Returns parallel structures:
      - list of transit datetimes (None when the target doesn't culminate
        inside the window),
      - ndarray of hours-above-min-alt per target,
      - ndarray (n_targets, n_sparkline_samples) of altitudes in degrees,
        sampled at SPARKLINE_STEP_MIN cadence from dusk to dawn. Empty
        last axis when the window is degenerate.

    Internal sampling cadence (5 min) and transit interpolation match
    the per-target helpers, so a switch from this batch path to the
    per-target one is observationally identical to within sub-minute
    noise.
    """
    dusk = _ensure_utc(dusk_utc)
    dawn = _ensure_utc(dawn_utc)
    n_targets = len(ra_deg)
    if n_targets == 0:
        return [], np.zeros(0), np.zeros((0, 0))
    if dawn <= dusk:
        return [None] * n_targets, np.zeros(n_targets), np.zeros((n_targets, 0))

    step_min = 5
    n_samples = int(math.ceil((dawn - dusk).total_seconds() / 60 / step_min)) + 1
    times = Time(
        [dusk + timedelta(minutes=step_min * i) for i in range(n_samples)],
        scale="utc",
    )
    targets = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame="icrs")
    # Broadcast: (n_targets, n_samples) altitude grid. Astropy treats a
    # column-shaped SkyCoord plus a row-shaped Time as a Cartesian
    # product across the obstime grid.
    altaz_frame = AltAz(obstime=times, location=loc)
    alts = np.asarray(
        targets[:, None].transform_to(altaz_frame).alt.to(u.deg).value  # type: ignore[union-attr]
    )
    above_counts = (alts >= min_alt_deg).sum(axis=1)
    hours = above_counts.astype(float) * (step_min / 60.0)
    # Stride-slice the alt grid for the per-row sparkline so the UI gets
    # an even sample spacing without paying for a second AltAz transform.
    alt_curves = alts[:, ::_SPARKLINE_STRIDE]

    # Transit detection: sample LST once (cheap; location-only), find the
    # zero crossing of (LST - RA) wrapped to (-180, 180] per target.
    lst_deg = np.asarray(
        times.sidereal_time("apparent", longitude=loc.lon).to(u.deg).value
    )
    delta = ((lst_deg[None, :] - ra_deg[:, None] + 540.0) % 360.0) - 180.0
    transits: list[datetime | None] = []
    for i in range(n_targets):
        d = delta[i]
        sign = np.sign(d)
        found_idx = -1
        exact_zero = False
        for j in range(len(sign) - 1):
            if d[j] == 0.0:
                found_idx = j
                exact_zero = True
                break
            if sign[j] < 0 <= sign[j + 1]:
                found_idx = j
                break
        if found_idx == -1:
            transits.append(None)
        elif exact_zero:
            transits.append(dusk + timedelta(minutes=step_min) * found_idx)
        else:
            d0, d1 = d[found_idx], d[found_idx + 1]
            frac = -d0 / (d1 - d0) if d1 != d0 else 0.0
            transits.append(
                dusk + timedelta(minutes=step_min) * (found_idx + float(frac))
            )
    return transits, hours, alt_curves
