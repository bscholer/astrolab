"""Trace the horizon: skyline points become motor angles that aim there, in one sweep along
the skyline, never near the Sun, and the run can be stopped."""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime

import numpy as np
import pytest

from server import sky
from server.pano_stitch import Lens, motor_rotation
from server.scope_trace import HorizonTrace, TraceRefused, motors_for, plan_trace

LOC = sky.observer_location(44.0, -121.3, 1100.0)
NIGHT = datetime(2026, 11, 26, 8, 0, tzinfo=UTC)
NOON = datetime(2026, 11, 26, 20, 0, tzinfo=UTC)
LENS = replace(Lens(), axis_offset=172.9, pitch_scale=1.0)
# On the wedge at latitude 44: up is 46 degrees from the axis, which leans north.
UP = np.array([0.0, math.sin(math.radians(46)), math.cos(math.radians(46))])
NORTH = np.array([0.0, math.cos(math.radians(46)), -math.sin(math.radians(46))])
FRAME = np.stack([np.cross(NORTH, UP), NORTH, UP])  # east, north, up rows


def _scan(tmp_path, compass=True, horizon=None, limits=(12.3, 303.2)):
    (tmp_path / "fit.json").write_text(
        json.dumps({"lens": asdict(LENS), "world_rows_enu": FRAME.tolist(), "tiles": []})
    )
    alt = horizon if horizon is not None else [20.0] * 360
    (tmp_path / "horizon.json").write_text(json.dumps({"alt": alt, "compass": compass}))
    (tmp_path / "manifest.json").write_text(json.dumps({"rotation_limits": list(limits)}))
    return tmp_path


def _aim(alt, az):
    a, z = math.radians(alt), math.radians(az)
    return np.array([math.cos(a) * math.sin(z), math.cos(a) * math.cos(z), math.sin(a)])


@pytest.mark.parametrize("alt,az", [(10.0, 30.0), (25.0, 180.0), (5.0, 270.0), (60.0, 120.0)])
def test_motor_angles_aim_where_asked(alt, az):
    for mirrored in (False, True):
        pitch, rotation = motors_for(alt, az, LENS, FRAME, mirrored)
        got = FRAME @ motor_rotation(pitch, rotation, LENS)[:, 2]
        assert math.degrees(math.acos(min(1.0, got @ _aim(alt, az)))) < 0.01


def test_the_trace_follows_the_skyline_within_reach(tmp_path):
    points = plan_trace(_scan(tmp_path))
    assert len(points) > 10
    for p in points:
        assert 12.3 <= p.rotation <= 303.2 and 0 <= p.pitch <= 175
        assert p.alt == pytest.approx(21.0)  # the skyline plus a degree, to show the edge
    # One sweep: azimuth steps forward by 10 degrees, with a single jump across the gap.
    steps = [(b.az - a.az) % 360 for a, b in zip(points, points[1:], strict=False)]
    assert all(s == pytest.approx(10.0) for s in steps)


def test_unseen_azimuths_and_the_sun_are_left_out(tmp_path):
    horizon = [None if 90 <= az < 120 else 15.0 for az in range(360)]
    points = plan_trace(_scan(tmp_path, horizon=horizon), sun_alt_az=(20.0, 180.0))
    azs = {p.az for p in points}
    assert not azs & {90.0, 100.0, 110.0}
    for p in points:
        sep = math.degrees(math.acos(min(1.0, _aim(p.alt, p.az) @ _aim(20.0, 180.0))))
        assert sep >= 30


def test_no_compass_no_trace(tmp_path):
    with pytest.raises(TraceRefused, match="compass"):
        plan_trace(_scan(tmp_path, compass=False))


class Scope:
    def __init__(self):
        self.points = []

    def call(self, op, payload=None, timeout=None):
        self.points.append((payload["pitch"], payload["rotation"]))
        return {"message": "pointed"}


def _wait(trace, until=("done", "stopped", "failed")):
    for _ in range(300):
        if trace.snapshot()["state"] in until:
            return trace.snapshot()
        time.sleep(0.02)
    raise AssertionError("trace did not finish")


def test_the_trace_visits_every_point(tmp_path):
    scope, trace = Scope(), HorizonTrace()
    n = trace.start(scope, _scan(tmp_path), LOC, when=NIGHT, dwell_s=0)
    snap = _wait(trace)
    assert snap["state"] == "done" and snap["done"] == n == len(scope.points)


def test_the_trace_stops_when_asked(tmp_path):
    scope, trace = Scope(), HorizonTrace()
    trace.start(scope, _scan(tmp_path), LOC, when=NIGHT, dwell_s=0.2)
    time.sleep(0.05)
    trace.stop()
    snap = _wait(trace)
    assert snap["state"] == "stopped" and len(scope.points) < snap["total"]


def test_daylight_needs_the_tele_covered(tmp_path):
    with pytest.raises(TraceRefused, match="Sun"):
        HorizonTrace().start(Scope(), _scan(tmp_path), LOC, when=NOON)
    trace = HorizonTrace()
    trace.start(Scope(), _scan(tmp_path), LOC, when=NOON, tele_covered=True, dwell_s=0)
    assert _wait(trace)["state"] == "done"
