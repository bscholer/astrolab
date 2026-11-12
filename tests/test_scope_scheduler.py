"""Scheduler decisions: start, keep, leave on failure, wait outside the night."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from astropy.time import Time

from server import sky
from server.scope_plan import Candidate
from server.scope_scheduler import Scheduler
from server.scope_service import ScopeServiceError

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

    def start(self, client, name, *args):
        self.started.append(name)
        self.args = args


def _sched(client, observer, now, targets):
    s = Scheduler(lambda: client, lambda: LOC, lambda: targets, observer, clock=lambda: now)
    s.enabled = True
    return s


def test_streak_counts_rejections_since_the_last_accepted_frame():
    s = _sched(FakeClient(), FakeObserver(), NIGHT, [_target("a")])
    assert s._account("a", {"captured": 6, "failed": 6}) == 6
    assert s._account("a", {"captured": 10, "failed": 10}) == 10
    assert s._account("a", {"captured": 12, "failed": 10}) == 0
    assert s._account("b", {"captured": 3, "failed": 3}) == 3


def test_accepted_frames_add_integration_time_at_the_target_exposure():
    saved = []
    s = Scheduler(
        lambda: FakeClient(),
        lambda: LOC,
        lambda: [Candidate("a", 0, 0, exposure_s=30)],
        FakeObserver(),
        save_progress=saved.append,
    )
    s._account("a", {"captured": 6, "failed": 2})
    s._account("a", {"captured": 10, "failed": 2})
    assert s._integrated["a"] == 8 * 30 and saved[-1] == {"a": 240}


def test_target_at_its_goal_is_stopped_and_not_picked_again():
    client = FakeClient({"state": "running", "captured": 4, "failed": 0})
    obs = FakeObserver({"state": "done", "step": "capturing", "target": "a"})
    a = Candidate(_target("a").name, _target("a").ra_deg, _target("a").dec_deg, goal_hours=0.01)
    s = _sched(client, obs, NIGHT, [a, _target("b")])
    s.tick()
    assert "stopCapture" in client.calls
    client.job = {}
    s.tick()
    assert obs.started == ["b"]


def test_nebula_defaults_to_duo_and_overrides_win():
    from server.scope_plan import capture_settings

    assert capture_settings(Candidate("m42", 0, 0, "Emission Nebula")).filter == "duo"
    assert capture_settings(Candidate("m31", 0, 0, "Galaxy")).filter == "astro"
    assert capture_settings(Candidate("m31", 0, 0)).gain == 60
    c = Candidate("x", 0, 0, "Emission Nebula", "astro", 30, 80)
    assert (c.filter, capture_settings(c).exposure_s, capture_settings(c).gain) == ("astro", 30, 80)


def test_idle_scope_starts_a_target():
    obs = FakeObserver()
    _sched(FakeClient(), obs, NIGHT, [_target("a")]).tick()
    assert obs.started == ["a"]


def test_daytime_stops_capture_and_starts_nothing():
    client, obs = FakeClient({"state": "running"}), FakeObserver()
    _sched(client, obs, DAY, [_target("a")]).tick()
    assert "stopCapture" in client.calls and not obs.started


def test_failing_target_is_stopped_then_skipped_for_the_next_pick():
    client = FakeClient({"state": "running", "captured": 10, "failed": 10})
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


def test_calibration_failure_waits_for_the_sky_instead_of_trying_every_target():
    client = FakeClient()
    obs = FakeObserver(
        {"state": "failed", "step": "calibrating", "target": "a", "error": "code -11504"}
    )
    s = _sched(client, obs, NIGHT, [_target("a"), _target("b")])
    s.tick()
    s.tick()
    assert not obs.started
    assert s.snapshot()["state"] == "waiting"


def test_barrel_closes_once_when_the_night_ends():
    client, obs = FakeClient(), FakeObserver()
    s = _sched(client, obs, DAY, [_target("a")])
    s.tick()
    s.tick()
    assert client.calls.count("closeBarrel") == 1


def test_barrel_closes_again_after_the_next_night():
    client, obs = FakeClient(), FakeObserver()
    now = {"t": DAY}
    s = Scheduler(lambda: client, lambda: LOC, lambda: [_target("a")], obs, clock=lambda: now["t"])
    s.enabled = True
    s.tick()
    now["t"] = NIGHT
    s.tick()
    now["t"] = DAY
    s.tick()
    assert client.calls.count("closeBarrel") == 2


def test_stalled_capture_is_stopped_and_the_target_skipped():
    client = FakeClient({"state": "running", "captured": 10, "failed": 0})
    obs = FakeObserver({"state": "done", "step": "capturing", "target": "a"})
    clock = {"t": 0.0}
    s = _sched(client, obs, NIGHT, [_target("a"), _target("b")])
    s.mono = lambda: clock["t"]
    s.tick()
    assert "stopCapture" not in client.calls
    clock["t"] = 11 * 60
    s.tick()
    assert "stopCapture" in client.calls
    assert "a" in s.snapshot()["skipped"]


def test_progress_resets_the_stall_timer():
    client = FakeClient({"state": "running", "captured": 10, "failed": 0})
    obs = FakeObserver({"state": "done", "step": "capturing", "target": "a"})
    clock = {"t": 0.0}
    s = _sched(client, obs, NIGHT, [_target("a")])
    s.mono = lambda: clock["t"]
    s.tick()
    clock["t"] = 9 * 60
    client.job = {"state": "running", "captured": 11, "failed": 0}
    s.tick()
    clock["t"] = 18 * 60
    s.tick()
    assert "stopCapture" not in client.calls


def test_alerts_fire_on_new_target_and_failing_target_only_once_per_change():
    msgs = []
    client = FakeClient()
    obs = FakeObserver()
    s = Scheduler(
        lambda: client,
        lambda: LOC,
        lambda: [_target("a"), _target("b")],
        obs,
        clock=lambda: NIGHT,
        notify=lambda t, m: msgs.append(m),
    )
    s.enabled = True
    s.tick()
    assert len(msgs) == 1 and msgs[0].startswith("Now shooting")
    obs.state = {"state": "done", "step": "capturing", "target": msgs[0].split()[-1]}
    client.job = {"state": "running", "captured": 10, "failed": 10}
    s.tick()
    assert any(m.startswith("Leaving") for m in msgs)


def test_rain_alert_fires_once_while_open_and_never_closes_the_barrel():
    msgs = []
    client = FakeClient()
    client.call = lambda op, payload=None, timeout=None: (
        client.calls.append(op) or ({"pitch": 140.0} if op == "getStatus" else {})
    )
    clock = {"t": 0.0}
    s = Scheduler(
        lambda: client,
        lambda: LOC,
        lambda: [],
        FakeObserver(),
        clock=lambda: NIGHT,
        notify=lambda t, m: msgs.append(m),
        get_warning=lambda: "rain forecast (3.0 mm)",
    )
    s.mono = lambda: clock["t"]
    s.enabled = True
    s.tick()
    clock["t"] = 3600
    s.tick()
    assert [m for m in msgs if "Bring the scope in" in m] == [
        "rain forecast (3.0 mm). Bring the scope in."
    ]
    assert "closeBarrel" not in client.calls


def test_no_rain_alert_when_barrel_is_closed():
    msgs = []
    client = FakeClient()
    client.call = lambda op, payload=None, timeout=None: (
        client.calls.append(op) or ({"pitch": 12.5} if op == "getStatus" else {})
    )
    s = Scheduler(
        lambda: client,
        lambda: LOC,
        lambda: [],
        FakeObserver(),
        clock=lambda: NIGHT,
        notify=lambda t, m: msgs.append(m),
        get_warning=lambda: "snow forecast (1.0 cm)",
    )
    s.enabled = True
    s.tick()
    assert not any("Bring" in m for m in msgs)


class BusyClient(FakeClient):
    def __init__(self, status, fail_capture=False):
        super().__init__()
        self.status = status
        self.fail_capture = fail_capture

    def call(self, op, payload=None, timeout=None):
        self.calls.append(op)
        if op == "getStatus":
            return self.status
        if op == "startCapture" and self.fail_capture:
            raise ScopeServiceError("capture failed, code -11501")
        return {}


def test_scope_busy_elsewhere_means_no_motor_commands_and_no_park():
    client = BusyClient({"busy_elsewhere": True, "pitch": 140.0})
    obs = FakeObserver()
    s = _sched(client, obs, DAY, [_target("a")])
    s.tick()
    assert client.calls == ["getStatus"]
    assert not obs.started
    assert "busy" in s.snapshot()["reason"]


def _failed(step, error, target="a"):
    return {"state": "failed", "step": step, "target": target, "error": error}


def test_cloud_calibration_waits_then_retries_the_full_observation():
    obs = FakeObserver(_failed("calibrating", "calibrate failed, code -11504"))
    s = _sched(FakeClient(), obs, NIGHT, [_target("a")])
    s.tick()
    assert "sky not clear" in s.snapshot()["reason"]
    assert not obs.started
    s._sky_wait_until = time.time() - 1
    obs.state = {"state": "idle"}
    s.tick()
    assert obs.started == ["a"]


def test_calibration_that_never_solves_counts_as_cloud_not_as_a_fault():
    obs = FakeObserver(_failed("calibrating", "calibrating: calibration timed out"))
    s = _sched(FakeClient(), obs, NIGHT, [_target("a")])
    s.tick()
    assert "sky not clear" in s.snapshot()["reason"]


def test_other_calibration_errors_are_a_fault_alerted_once_and_retried_sooner_than_cloud():
    msgs = []
    obs = FakeObserver(_failed("calibrating", "scope closed the connection"))
    s = _sched(FakeClient(), obs, NIGHT, [_target("a")])
    s._notify = lambda t, m: msgs.append(m)
    s.tick()
    s.tick()
    assert s.snapshot()["reason"].startswith("scope fault")
    assert len([m for m in msgs if "not cloud" in m]) == 1
    assert 60 < s._sky_wait_until - time.time() <= 2 * 60


def test_failed_capture_job_stops_the_scope_and_pauses_one_tick_before_a_new_target():
    client = FakeClient({"id": "j1", "state": "done"})
    obs = FakeObserver({"state": "done", "step": "capturing", "target": "a"})
    s = _sched(client, obs, NIGHT, [_target("a")])
    s.tick()
    obs.started.clear()
    client.job = {"id": "j2", "state": "failed", "error": "scope closed the connection"}
    s.tick()
    assert client.calls.count("stopCapture") == 1
    assert not obs.started
    assert "capture job failed" in s.snapshot()["reason"]
    s.tick()
    assert client.calls.count("stopCapture") == 1
    assert obs.started == ["a"]


def test_a_failed_job_left_from_before_a_restart_is_not_acted_on():
    client = FakeClient({"id": "old", "state": "failed", "error": "x"})
    obs = FakeObserver()
    s = _sched(client, obs, NIGHT, [_target("a")])
    s.tick()
    assert "stopCapture" not in client.calls
    assert obs.started == ["a"]


def test_stuck_alert_fires_once_after_no_frames_and_rearms_when_frames_arrive():
    msgs = []
    clock = {"t": 0.0}
    client = FakeClient({"id": "j", "state": "running", "captured": 0, "failed": 0})
    obs = FakeObserver({"state": "done", "step": "capturing", "target": "a"})
    s = _sched(client, obs, NIGHT, [_target("a")])
    s.mono = lambda: clock["t"]
    s._last_frame_at = 0.0
    s._notify = lambda t, m: msgs.append(m)
    clock["t"] = 44 * 60
    s.tick()
    assert not any("No frames" in m for m in msgs)
    clock["t"] = 46 * 60
    s.tick()
    s.tick()
    assert len([m for m in msgs if "No frames" in m]) == 1
    client.job = {"id": "j", "state": "running", "captured": 3, "failed": 0}
    s.tick()
    assert s._stuck_alerted is False


def test_low_disk_alerts_once_and_rearms_after_space_is_freed():
    msgs = []
    client = BusyClient({"sd_free_gb": 0.0})
    s = _sched(client, FakeObserver(), NIGHT, [_target("a")])
    s._notify = lambda t, m: msgs.append(m)
    s.tick()
    s.tick()
    assert len([m for m in msgs if "SD card" in m]) == 1
    client.status = {"sd_free_gb": 50.0}
    s.tick()
    client.status = {"sd_free_gb": 0.5}
    s.tick()
    assert len([m for m in msgs if "SD card" in m]) == 2


def test_capture_astrolab_started_but_the_relay_lost_is_stopped_while_a_foreign_one_is_left():
    client = BusyClient({"job": {"id": "j", "state": "running"}})
    obs = FakeObserver({"state": "done", "step": "capturing", "target": "a"})
    s = _sched(client, obs, NIGHT, [_target("a")])
    s.tick()
    client.status = {"job": None, "busy_elsewhere": True}
    s.tick()
    assert client.calls.count("stopCapture") == 1
    s.tick()
    assert client.calls.count("stopCapture") == 1
    assert "busy" in s.snapshot()["reason"]


def test_frames_are_noted_on_the_sky_map_where_the_target_is():
    from server import sky
    from server.scope_skymap import SkyMap, cell_key

    store: dict = {}
    skymap = SkyMap(lambda: store.get("c", {}), lambda c: store.update(c=c))
    target = _target("a")
    s = Scheduler(
        lambda: FakeClient(), lambda: LOC, lambda: [target], FakeObserver(), clock=lambda: NIGHT,
        skymap=skymap,
    )
    s._account("a", {"captured": 6, "failed": 2})
    alt, az = sky.alt_az_now(target.ra_deg, target.dec_deg, LOC, NIGHT)
    cell = skymap.cells()[cell_key(alt, az)]
    assert (cell["good"], cell["failed"]) == (4, 2)
    s._account("a", {"captured": 6, "failed": 2})
    assert skymap.cells()[cell_key(alt, az)]["good"] == 4
