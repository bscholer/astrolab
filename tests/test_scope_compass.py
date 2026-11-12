"""Compass run: needs dark, stores true altitude and azimuth with each motor reading."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from server import sky
from server.scope_compass import CompassRefused, CompassRun, point_to_radec

LOC = sky.observer_location(44.0, -121.3, 1100.0)
NIGHT = datetime(2026, 11, 8, 8, 0, tzinfo=UTC)
NOON = datetime(2026, 11, 7, 20, 0, tzinfo=UTC)


class Fake:
    def __init__(self, fail_goto_at=None):
        self.calls = []
        self.fail_goto_at = fail_goto_at

    def call(self, op, payload=None, timeout=None):
        self.calls.append(op)
        if op == "gotoTarget" and self.fail_goto_at and self.fail_goto_at in payload["name"]:
            raise RuntimeError("slew refused")
        if op == "getStatus":
            return {"pitch": 100.0, "rotation": 200.0}
        return {}


def _wait(run):
    for _ in range(200):
        if run.snapshot()["state"] != "running":
            return
        time.sleep(0.05)


def test_point_round_trips_through_ra_dec():
    ra, dec = point_to_radec(45.0, 200.0, LOC, NIGHT)
    alt, az = sky.alt_az_now(ra, dec, LOC, NIGHT)
    assert alt == pytest.approx(45.0, abs=0.01)
    assert az == pytest.approx(200.0, abs=0.01)


def test_refuses_before_full_dark():
    with pytest.raises(CompassRefused):
        CompassRun(lambda _p: None).start(Fake(), LOC, when=NOON)


def test_stores_pairs_and_skips_a_refused_slew(monkeypatch):
    # The run slews at the real current time, so keep the real Moon out of the way.
    monkeypatch.setattr("server.scope_compass.moon_separation_deg", lambda *a: 90.0)
    pairs = []
    run = CompassRun(pairs.append)
    run.start(Fake(fail_goto_at="45/280"), LOC, settle_s=0, when=NIGHT)
    _wait(run)
    snap = run.snapshot()
    assert snap["state"] == "done" and snap["failed"] == 1 and snap["done"] == 5
    assert len(pairs) == 4
    assert all({"pitch", "rotation", "alt_deg", "az_deg"} <= set(p) for p in pairs)
    assert pairs[0]["alt_deg"] == pytest.approx(45.0, abs=0.5)
