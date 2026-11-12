"""Sky map: good frames prove a patch open, failures on several nights mark it blocked."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from server.scope_skymap import SkyMap, cell_key, night_of, skymap_svg, verdict

NIGHT1 = datetime(2026, 11, 12, 6, 0, tzinfo=UTC)


def _map():
    store: dict = {}
    return SkyMap(lambda: store.get("cells", {}), lambda c: store.update(cells=c)), store


def test_cells_and_nights():
    assert cell_key(37.2, 121.9) == "35/120"
    assert cell_key(10.0, 359.9) == "10/355"
    assert cell_key(10.0, -0.1) == "10/355"
    # In Oregon, dusk at 01:00 UTC and dawn at 13:30 UTC are one night; the next dusk is not.
    bend = -121.3
    assert night_of(datetime(2026, 11, 13, 1, 0, tzinfo=UTC), bend) == "2026-11-12"
    assert night_of(datetime(2026, 11, 13, 13, 30, tzinfo=UTC), bend) == "2026-11-12"
    assert night_of(datetime(2026, 11, 14, 1, 0, tzinfo=UTC), bend) == "2026-11-13"


def test_good_frames_mark_a_patch_clear():
    m, store = _map()
    m.record(40, 100, 3, 1, NIGHT1)
    assert verdict(m.cells()["40/100"]) == "clear"
    assert not m.blocked(41, 101)
    assert store["cells"]["40/100"]["good"] == 3


def test_failures_on_one_cloudy_night_stay_unknown():
    m, _ = _map()
    m.record(40, 100, 0, 30, NIGHT1)
    assert verdict(m.cells()["40/100"]) == "unknown"
    assert not m.blocked(40, 100)


def test_failures_on_three_nights_mark_it_blocked_until_good_frames_show():
    m, _ = _map()
    for n in range(3):
        m.record(40, 100, 0, 4, NIGHT1 + timedelta(days=n))
    assert m.blocked(42, 103)
    m.record(40, 100, 5, 0, NIGHT1 + timedelta(days=4))
    assert not m.blocked(42, 103)


def test_below_the_horizon_and_empty_records_are_ignored():
    m, _ = _map()
    m.record(-5, 100, 0, 9, NIGHT1)
    m.record(30, 100, 0, 0, NIGHT1)
    assert m.cells() == {}


def test_svg_shows_each_patch():
    m, _ = _map()
    m.record(40, 100, 3, 0, NIGHT1)
    m.record(60, 200, 0, 2, NIGHT1)
    svg = skymap_svg(m.cells(), 30.0)
    assert svg.count("<circle cx") == 2 + 3  # two patches and the three rings
    assert "clear, 3 good" in svg
