"""Weather: light drizzle passes, real rain or any snow warns, window starts at now."""

from __future__ import annotations

from server.weather import outlook


def _forecast(cloud=0, rain=None, snow=None):
    times = [f"2026-11-07T{h:02d}:00" for h in range(24)]
    return {
        "hourly": {
            "time": times,
            "cloud_cover": [cloud] * 24,
            "precipitation": rain or [0] * 24,
            "snowfall": snow or [0] * 24,
        }
    }


NOW = "2026-11-07T06:30:00"


def test_light_drizzle_does_not_warn():
    rain = [0.1] * 24
    o = outlook(37.0, -122.0, NOW, fetch=lambda _: _forecast(rain=rain))
    assert not o.warn and o.precip_mm == 1.2


def test_heavy_hour_warns():
    rain = [0] * 24
    rain[10] = 2.0
    o = outlook(37.0, -122.0, NOW, fetch=lambda _: _forecast(rain=rain))
    assert o.warn and "rain" in o.reason


def test_snow_warns_even_when_light():
    snow = [0] * 24
    snow[8] = 0.2
    assert outlook(37.0, -122.0, NOW, fetch=lambda _: _forecast(snow=snow)).warn


def test_rain_before_now_is_ignored():
    rain = [0] * 24
    rain[2] = 5.0
    o = outlook(37.0, -122.0, NOW, fetch=lambda _: _forecast(rain=rain))
    assert not o.warn


def test_rain_past_the_window_is_ignored():
    rain = [0] * 24
    rain[22] = 5.0
    assert not outlook(37.0, -122.0, NOW, fetch=lambda _: _forecast(rain=rain)).warn
