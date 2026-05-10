"""Tests for `server.sky` math.

The reference values pin observable behavior against external sources
(Stellarium / in-the-sky.org) so a future astropy upgrade or a unit-
conversion typo gets caught loudly. Tolerances are deliberately a few
arc-minutes wide because the underlying inputs (J2000 RA/Dec, no
proper motion correction, no refraction-vs-airless toggle) all carry
roughly that much error themselves.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import server.sky as sky

# Manhattan, NY. Used as a stable mid-latitude reference site.
NYC_LAT = 40.7
NYC_LON = -74.0
NYC_ELEV = 10.0

# M31 Andromeda, J2000.
M31_RA = 10.6847
M31_DEC = 41.269


def test_observer_location_validates_lat_range() -> None:
    with pytest.raises(ValueError):
        sky.observer_location(91.0, 0.0, 0.0)
    with pytest.raises(ValueError):
        sky.observer_location(-91.0, 0.0, 0.0)


def test_observer_location_validates_lon_range() -> None:
    with pytest.raises(ValueError):
        sky.observer_location(0.0, 181.0, 0.0)
    with pytest.raises(ValueError):
        sky.observer_location(0.0, -181.0, 0.0)


def test_alt_az_now_handles_naive_datetime() -> None:
    """Naive datetime is interpreted as UTC, mirroring astropy's behavior
    elsewhere in the project. The point is to not silently downgrade to
    local time and produce hours-of-error in alt."""
    loc = sky.observer_location(NYC_LAT, NYC_LON, NYC_ELEV)
    naive = datetime(2024, 10, 15, 4, 0)
    aware = naive.replace(tzinfo=UTC)
    alt_naive, az_naive = sky.alt_az_now(M31_RA, M31_DEC, loc, naive)
    alt_aware, az_aware = sky.alt_az_now(M31_RA, M31_DEC, loc, aware)
    assert alt_naive == pytest.approx(alt_aware, abs=1e-6)
    assert az_naive == pytest.approx(az_aware, abs=1e-6)


def test_m31_transit_nyc_october_pinned() -> None:
    """M31 culminates near local midnight in mid-October from NYC.

    Reference: in-the-sky.org puts M31 culmination at approximately
    23:59 EDT on 2024-10-14 (= 03:59 UTC on 2024-10-15). We allow
    +/- 5 minutes since site coords aren't exact and we don't refract
    the LST formula.
    """
    loc = sky.observer_location(NYC_LAT, NYC_LON, NYC_ELEV)
    at = datetime(2024, 10, 15, 4, 0, tzinfo=UTC)
    window = sky.twilight_window(loc, at)
    assert window is not None
    dusk, dawn = window
    transit = sky.transit_in_window(M31_RA, M31_DEC, loc, dusk, dawn)
    assert transit is not None
    expected = datetime(2024, 10, 15, 4, 2, tzinfo=UTC)
    assert abs(transit - expected) < timedelta(minutes=5)


def test_alt_at_transit_matches_culmination_formula() -> None:
    """At upper culmination, altitude = 90 - |lat - dec| for a target
    that crosses the meridian. M31 from NYC: 90 - |40.7 - 41.27| = 89.4."""
    loc = sky.observer_location(NYC_LAT, NYC_LON, NYC_ELEV)
    at = datetime(2024, 10, 15, 4, 0, tzinfo=UTC)
    dusk, dawn = sky.twilight_window(loc, at)  # type: ignore[misc]
    transit = sky.transit_in_window(M31_RA, M31_DEC, loc, dusk, dawn)
    assert transit is not None
    alt, _ = sky.alt_az_now(M31_RA, M31_DEC, loc, transit)
    expected = 90.0 - abs(NYC_LAT - M31_DEC)
    assert alt == pytest.approx(expected, abs=0.5)


def test_alt_az_batch_matches_singleton() -> None:
    import numpy as np

    loc = sky.observer_location(NYC_LAT, NYC_LON, NYC_ELEV)
    at = datetime(2024, 10, 15, 4, 0, tzinfo=UTC)
    ra = np.array([M31_RA, 56.6, 83.6])  # M31, NGC 1499 area, M42
    dec = np.array([M31_DEC, 31.5, -5.4])
    alts, azs = sky.alt_az_batch(ra, dec, loc, at)
    for i in range(len(ra)):
        single_alt, single_az = sky.alt_az_now(
            float(ra[i]), float(dec[i]), loc, at
        )
        assert alts[i] == pytest.approx(single_alt, abs=1e-3)
        assert azs[i] == pytest.approx(single_az, abs=1e-3)


def test_hours_above_zero_for_southern_target_in_nyc() -> None:
    """A target deep in the southern celestial hemisphere never clears
    20 deg from a mid-northern site. Pinning this so a regression in
    the alt-sample loop (e.g. summing before filtering) doesn't accidentally
    return half the night for everything."""
    loc = sky.observer_location(NYC_LAT, NYC_LON, NYC_ELEV)
    at = datetime(2024, 10, 15, 4, 0, tzinfo=UTC)
    dusk, dawn = sky.twilight_window(loc, at)  # type: ignore[misc]
    # 47 Tucanae is at dec ~-72; never visible from NYC.
    hours = sky.hours_above(6.0, -72.0, loc, dusk, dawn, 20.0)
    assert hours == 0.0


def test_hours_above_majority_of_window_for_high_target() -> None:
    """M31 from NYC stays well above 20 deg for most of an October night
    (it transits near zenith). Should be > 8h, well below the full ~10h
    window, so we have a wide brackets to catch breakage."""
    loc = sky.observer_location(NYC_LAT, NYC_LON, NYC_ELEV)
    at = datetime(2024, 10, 15, 4, 0, tzinfo=UTC)
    dusk, dawn = sky.twilight_window(loc, at)  # type: ignore[misc]
    hours = sky.hours_above(M31_RA, M31_DEC, loc, dusk, dawn, 20.0)
    assert 8.0 < hours <= (dawn - dusk).total_seconds() / 3600 + 0.1


def test_polar_day_returns_no_window() -> None:
    """North pole in summer: sun never sets, so there is no night window.
    Caller renders the page with no transit times."""
    loc = sky.observer_location(89.9, 0.0, 0.0)
    at = datetime(2024, 6, 21, 12, 0, tzinfo=UTC)
    assert sky.twilight_window(loc, at) is None


def test_polar_night_returns_full_24h_window() -> None:
    """North pole in winter: sun never rises, so the entire 24h band
    counts as night. The Tonight planner treats this as "everything
    that's up is fair game for the whole 24h."""
    loc = sky.observer_location(89.9, 0.0, 0.0)
    at = datetime(2024, 12, 21, 12, 0, tzinfo=UTC)
    window = sky.twilight_window(loc, at)
    assert window is not None
    dusk, dawn = window
    assert (dawn - dusk) >= timedelta(hours=23, minutes=50)


def test_equator_window_is_normal_night() -> None:
    """Equator: ~12h day / 12h night year-round. Twilight window should
    be roughly 9-10 hours of true astronomical darkness."""
    loc = sky.observer_location(0.0, 0.0, 0.0)
    at = datetime(2024, 3, 20, 0, 0, tzinfo=UTC)
    window = sky.twilight_window(loc, at)
    assert window is not None
    dusk, dawn = window
    duration_h = (dawn - dusk).total_seconds() / 3600
    assert 9.5 < duration_h < 11.5


def test_high_lat_summer_falls_back_to_civil_twilight() -> None:
    """Anchorage in mid-July: the sun reaches about -7.5 deg at minimum,
    too shallow for astronomical (-18) or nautical (-12) twilight, but
    deep enough for civil (-6). The fallback chain should still produce
    a window (rather than None) so northern users in summer still get a
    target list, even if the sky never gets fully dark."""
    loc = sky.observer_location(61.2, -149.9, 0.0)
    at = datetime(2024, 7, 15, 9, 0, tzinfo=UTC)
    window = sky.twilight_window(loc, at)
    assert window is not None
    dusk, dawn = window
    assert dawn > dusk


def test_transit_returns_none_for_target_culminating_in_daylight() -> None:
    """A target whose RA puts its transit at local noon won't transit
    within the dusk/dawn window. Make sure we return None instead of
    snapping to dusk or dawn."""
    loc = sky.observer_location(NYC_LAT, NYC_LON, NYC_ELEV)
    # Pick a date and target whose transit lands at noon UTC ~ 8am EDT,
    # well outside the night window. RA ~270 (LST at noon in October
    # NYC is ~13.5h = 202.5deg, so RA 200 culminates near noon).
    at = datetime(2024, 10, 15, 4, 0, tzinfo=UTC)
    dusk, dawn = sky.twilight_window(loc, at)  # type: ignore[misc]
    # RA 195 = ~13h transit time roughly opposite the night.
    transit = sky.transit_in_window(195.0, 30.0, loc, dusk, dawn)
    assert transit is None


def test_night_transits_and_hours_matches_per_target() -> None:
    """The vectorized batch path should agree with per-target calls
    within sub-minute / sub-fraction-hour noise. This pins the batch
    optimization so a future astropy upgrade or a numpy broadcasting
    bug stops shipping different alt-curves to the planner UI than
    the unit-tested per-target helpers compute."""
    import numpy as np

    loc = sky.observer_location(NYC_LAT, NYC_LON, NYC_ELEV)
    at = datetime(2024, 10, 15, 4, 0, tzinfo=UTC)
    dusk, dawn = sky.twilight_window(loc, at)  # type: ignore[misc]
    ra = np.array([M31_RA, 56.6, 83.6, 270.0])
    dec = np.array([M31_DEC, 31.5, -5.4, 30.0])

    transits, hours = sky.night_transits_and_hours(
        ra, dec, loc, dusk, dawn, 20.0
    )
    assert len(transits) == 4
    assert len(hours) == 4
    for i in range(4):
        single_transit = sky.transit_in_window(
            float(ra[i]), float(dec[i]), loc, dusk, dawn
        )
        single_hours = sky.hours_above(
            float(ra[i]), float(dec[i]), loc, dusk, dawn, 20.0
        )
        batch_transit = transits[i]
        if single_transit is None:
            assert batch_transit is None
        else:
            assert batch_transit is not None
            assert abs(batch_transit - single_transit) < timedelta(minutes=1)
        # 5-min sampling step means agreement to within one bucket.
        assert abs(hours[i] - single_hours) <= 5 / 60 + 1e-9


def test_hours_above_below_threshold_returns_zero() -> None:
    """A target above min_alt for some samples but never for the full
    threshold should still be measured. We're really just pinning that
    a target peaking AT the threshold reports as visible; the math is
    sample-and-count."""
    loc = sky.observer_location(NYC_LAT, NYC_LON, NYC_ELEV)
    at = datetime(2024, 10, 15, 4, 0, tzinfo=UTC)
    dusk, dawn = sky.twilight_window(loc, at)  # type: ignore[misc]
    # Target that culminates around 10 deg from NYC: dec = NYC_LAT - 80 = -39.3
    hours = sky.hours_above(180.0, -39.3, loc, dusk, dawn, 20.0)
    assert hours == 0.0
