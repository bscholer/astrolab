"""Planner: altitude, setting soon, moon limit, skip list, and hysteresis."""

from __future__ import annotations

from datetime import UTC, datetime

import astropy.units as u
import pytest
from astropy.coordinates import SkyCoord, get_body
from astropy.time import Time

from server import scope_plan, sky
from server.scope_plan import Candidate, in_window, moon_separation_deg, pick_target

LOC = sky.observer_location(37.0, -122.0, 100.0)
NOW = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)  # 22:00 local, night


def _high(name: str, dec: float = 37.0) -> Candidate:
    """A target near the meridian: RA equals local sidereal time."""
    lst = Time(NOW, scale="utc").sidereal_time("apparent", longitude=LOC.lon).deg
    return Candidate(name, float(lst), dec)


def test_low_target_rejected():
    south = Candidate("south", 0.0, -80.0)
    assert pick_target([south], LOC, NOW) is None


def test_zenith_target_picked_and_skip_respected():
    a = _high("a")
    assert pick_target([a], LOC, NOW).target.name == "a"
    assert pick_target([a], LOC, NOW, skip=frozenset({"a"})) is None


def test_current_target_kept_over_higher_one():
    a, b = _high("a", 20.0), _high("b", 37.0)
    assert pick_target([a, b], LOC, NOW).target.name == "b"
    assert pick_target([a, b], LOC, NOW, current="a").target.name == "a"


def test_daytime_is_outside_window():
    assert not in_window(LOC, datetime(2026, 11, 7, 20, 0, tzinfo=UTC))
    assert in_window(LOC, NOW)


def test_moon_limit_rejects_close_target(monkeypatch):
    a = _high("a")
    monkeypatch.setattr(scope_plan, "moon_separation_deg", lambda *_: 5.0)
    assert pick_target([a], LOC, NOW) is None
    monkeypatch.setattr(scope_plan, "moon_separation_deg", lambda *_: 15.0)
    assert pick_target([a], LOC, NOW).target.name == "a"


def test_moon_separation_is_zero_at_the_moon():
    # The Moon's RA and Dec as seen from the site (GCRS). Its ICRS position is from the
    # solar system barycentre, nowhere near where the Moon appears in the sky.
    moon = get_body("moon", Time(NOW, scale="utc"), LOC)
    at_moon = Candidate("m", float(moon.ra.deg), float(moon.dec.deg))
    assert moon_separation_deg(at_moon, LOC, NOW) < 0.5


def test_target_in_a_blocked_patch_is_skipped():
    a = _high("a")
    assert pick_target([a], LOC, NOW, blocked=lambda _alt, _az: False).target.name == "a"
    assert pick_target([a], LOC, NOW, blocked=lambda _alt, _az: True) is None


def test_moon_separation_is_seen_from_the_site():
    """Comparing in the Moon's own frame. Converting it to ICRS (barycentric) is far off."""
    t = Time(NOW, scale="utc")
    moon = get_body("moon", t, LOC)
    for ra, dec in ((10, 41), (98, 5), (270, 66)):
        right = moon.separation(SkyCoord(ra=ra * u.deg, dec=dec * u.deg)).deg
        assert moon_separation_deg(Candidate("x", ra, dec), LOC, NOW) == pytest.approx(right, abs=0.2)


def test_time_goes_where_it_helps_most() -> None:
    from server.scope_plan import hour_gain

    a = Candidate("a", 0.0, 89.0)  # always high from this site
    b = Candidate("b", 180.0, 89.5)
    both = [a, b]
    # Equal data: the higher one, as before.
    higher = pick_target(both, LOC, NOW).target.name
    assert pick_target(both, LOC, NOW, integrated={}).target.name == higher
    lower = "a" if higher == "b" else "b"
    # Hours on the higher one, nothing on the lower: the lower one gains more.
    assert pick_target(both, LOC, NOW, integrated={higher: 5 * 3600}).target.name == lower
    # The current target is kept while usable, whatever the gains.
    assert pick_target(both, LOC, NOW, current=higher,
                       integrated={higher: 9 * 3600}).target.name == higher
    assert hour_gain(0) == 1.0
    assert hour_gain(3600) == pytest.approx(1 - 0.5 ** 0.5)


def test_settings_fit_the_connected_model():
    from server.scope_plan import Candidate, capture_options, capture_settings

    nebula = Candidate("NGC 7000", 314.7, 44.3, "H II Region", exposure_s=30)
    dwarf2 = capture_options({"capture": {"filters": ["vis", "astro"], "exposures": ["1", "10", "13", "15"]}})
    cs = capture_settings(nebula, dwarf2)
    assert (cs.filter, cs.exposure_s) == ("astro", 15), "no duo-band, and 15 s is its longest"

    mini = capture_options({"capture": {"filters": ["astro", "duo"], "exposures": ["15", "30", "60", "180"]}})
    galaxy = Candidate("M 31", 10.7, 41.3, "G", filter="vis", exposure_s=180)
    cs = capture_settings(galaxy, mini)
    assert (cs.filter, cs.exposure_s) == ("astro", 180), "no vis on a mini, and 180 s is there"


def test_an_older_service_keeps_the_dwarf3_choices():
    from server.scope_plan import Candidate, capture_options, capture_settings

    options = capture_options({"spec_version": "0.1", "capabilities": ["capture"]})
    assert options == {"filters": ["vis", "astro", "duo"], "exposures": ["15", "30", "60"]}
    cs = capture_settings(Candidate("NGC 7000", 314.7, 44.3, "H II Region", exposure_s=60), options)
    assert (cs.filter, cs.exposure_s) == ("duo", 60)
