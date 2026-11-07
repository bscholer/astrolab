"""Planner: altitude, setting soon, moon limit, skip list, and hysteresis."""

from __future__ import annotations

from datetime import UTC, datetime

from astropy.coordinates import get_body
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
    moon = get_body("moon", Time(NOW, scale="utc"), LOC).icrs
    at_moon = Candidate("m", float(moon.ra.deg), float(moon.dec.deg))
    assert moon_separation_deg(at_moon, LOC, NOW) < 0.5
