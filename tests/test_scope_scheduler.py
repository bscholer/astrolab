"""Scheduler decisions: start, keep, leave on failure, wait outside the night."""

from __future__ import annotations

from datetime import UTC, datetime

from astropy.time import Time

from server import sky
from server.scope_plan import Candidate
from server.scope_scheduler import Scheduler, failing

LOC = sky.observer_location(37.0, -122.0, 100.0)
NIGHT = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
DAY = datetime(2026, 11, 7, 20, 0, tzinfo=UTC)


def _target(name: str) -> Candidate:
    lst = Time(NIGHT, scale="utc").sidereal_time("apparent", longitude=LOC.lon).deg
    return Candidate(name, float(lst), 37.0)


class FakeClient:
    def __init__(self, job=None):
        self.job = job or {}
        self.calls: list[str] = []

    def call(self, op, payload=None, timeout=None):
        self.calls.append(op)
        return {"job": self.job} if op == "getStatus" else {}


class FakeObserver:
    def __init__(self, state=None):
        self.state = state or {"state": "idle"}
        self.started: list[str] = []

    def snapshot(self):
        return dict(self.state)

    def start(self, client, name, *_):
        self.started.append(name)


def _sched(client, observer, now, targets):
    s = Scheduler(lambda: client, lambda: LOC, lambda: targets, observer, clock=lambda: now)
    s.enabled = True
    return s


def test_failing_needs_a_sample():
    assert not failing({"captured": 3, "failed": 3})
    assert failing({"captured": 10, "failed": 6})
    assert not failing({"captured": 10, "failed": 5})


def test_idle_scope_starts_a_target():
    obs = FakeObserver()
    _sched(FakeClient(), obs, NIGHT, [_target("a")]).tick()
    assert obs.started == ["a"]


def test_daytime_stops_capture_and_starts_nothing():
    client, obs = FakeClient({"state": "running"}), FakeObserver()
    _sched(client, obs, DAY, [_target("a")]).tick()
    assert "stopCapture" in client.calls and not obs.started


def test_failing_target_is_stopped_then_skipped_for_the_next_pick():
    client = FakeClient({"state": "running", "captured": 10, "failed": 8})
    obs = FakeObserver({"state": "done", "step": "capturing", "target": "a"})
    s = _sched(client, obs, NIGHT, [_target("a"), _target("b")])
    s.tick()
    assert "stopCapture" in client.calls
    client.job = {}
    s.tick()
    assert obs.started == ["b"]


def test_healthy_capture_is_left_alone():
    client = FakeClient({"state": "running", "captured": 10, "failed": 1})
    obs = FakeObserver({"state": "done", "step": "capturing", "target": "a"})
    _sched(client, obs, NIGHT, [_target("a")]).tick()
    assert client.calls == ["getStatus"] and not obs.started


def test_off_by_default_does_nothing():
    client, obs = FakeClient(), FakeObserver()
    s = _sched(client, obs, NIGHT, [_target("a")])
    s.enabled = False
    s.tick()
    assert not client.calls
