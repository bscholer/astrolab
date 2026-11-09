"""Keep the scope busy all night: pick a visible target, switch when the sky or target fails."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from astropy.coordinates import EarthLocation

from server import scope_plan
from server.scope_observe import Observer
from server.scope_plan import Candidate
from server.scope_service import ScopeServiceClient, ScopeServiceError

log = logging.getLogger(__name__)

FRAMES_PER_RUN = 200
EXPOSURE_S = 15
FAIL_STREAK = 10
SKIP_AFTER_FAIL_S = 30 * 60
SKIP_AFTER_ERROR_S = 15 * 60
SKY_WAIT_S = 5 * 60
BUSY_CODE = "-11501"  # firmware: function busy
BUSY_WAIT_S = 15 * 60
STALL_S = 10 * 60
WEATHER_EVERY_S = 30 * 60
BARREL_OPEN_PITCH = 30.0
PROBE_FRAMES = 4
PROBE_MAX_FAILED = 1
PROBE_TIMEOUT_S = 150


class Scheduler:
    def __init__(
        self,
        get_client: Callable[[], ScopeServiceClient | None],
        get_site: Callable[[], EarthLocation | None],
        get_targets: Callable[[], list[Candidate]],
        observer: Observer,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        interval_s: float = 60.0,
        notify: Callable[[str, str], None] | None = None,
        get_warning: Callable[[], str | None] | None = None,
        load_progress: Callable[[], dict[str, float]] | None = None,
        save_progress: Callable[[dict[str, float]], None] | None = None,
    ) -> None:
        self._get_client = get_client
        self._get_site = get_site
        self._get_targets = get_targets
        self._observer = observer
        self._clock = clock
        self.interval_s = interval_s
        self._notify = notify or (lambda *_: None)
        self._get_warning = get_warning or (lambda: None)
        self._weather_checked = -WEATHER_EVERY_S
        self._weather_alerted = False
        self._last_target: str | None = None
        self.probe_poll_s = 10.0
        self.mono: Callable[[], float] = time.monotonic
        self._progress: tuple[str | None, int, float] = (None, -1, 0.0)
        self._streak: tuple[str | None, int, int, int] = (None, 0, 0, 0)
        self._save_progress = save_progress or (lambda _p: None)
        self._load_progress = load_progress or dict
        self._loaded: dict[str, float] | None = None
        self.enabled = False
        self._lock = threading.Lock()
        self._skip: dict[str, float] = {}
        self._sky_wait_until = 0.0
        self._handled_failure: str | None = None
        self._parked = False
        self.state: dict = {"state": "off"}

    @property
    def _integrated(self) -> dict[str, float]:
        if self._loaded is None:
            self._loaded = dict(self._load_progress())
        return self._loaded

    def snapshot(self) -> dict:
        with self._lock:
            return {
                **self.state,
                "enabled": self.enabled,
                "skipped": sorted(self._active_skips()),
                "integrated_s": dict(self._integrated),
            }

    def _set(self, **kw) -> None:
        with self._lock:
            self.state = kw

    def _active_skips(self) -> set[str]:
        now = time.time()
        return {n for n, until in self._skip.items() if until > now}

    def _skip_target(self, name: str | None, seconds: float) -> None:
        if name:
            self._skip[name] = time.time() + seconds

    def tick(self) -> None:
        client, loc = self._get_client(), self._get_site()
        if not self.enabled:
            self._set(state="off")
            return
        if client is None or loc is None:
            self._set(state="waiting", reason="scope service or site not configured")
            return
        now = self._clock()
        obs = self._observer.snapshot()
        if obs.get("state") == "running" and obs.get("step") != "capturing":
            self._set(state="observing", target=obs.get("target"), reason=obs.get("step"))
            return
        try:
            status = client.call("getStatus") or {}
            job = status.get("job") or {}
            self._check_weather(status)
            if status.get("busy_elsewhere"):
                self._set(state="waiting", reason="scope is busy with another capture")
                return
            capturing = job.get("state") == "running"
            if not scope_plan.in_window(loc, now):
                if capturing:
                    client.call("stopCapture")
                elif not self._parked:
                    self._park(client)
                self._set(state="waiting", reason="outside the night window")
                return
            self._parked = False
            self._step(client, loc, now, job, capturing)
        except ScopeServiceError as exc:
            if self.state.get("state") != "error":
                self._notify("Scope error", str(exc))
            self._set(state="error", reason=str(exc))

    def _check_weather(self, status: dict) -> None:
        """Alert once per warning when rain is forecast and the barrel is open. Never closes it."""
        if self.mono() - self._weather_checked < WEATHER_EVERY_S:
            return
        self._weather_checked = self.mono()
        try:
            warning = self._get_warning()
        except Exception:
            log.warning("weather check failed", exc_info=True)
            return
        if warning is None:
            self._weather_alerted = False
        elif not self._weather_alerted and (status.get("pitch") or 0) > BARREL_OPEN_PITCH:
            self._weather_alerted = True
            self._notify("Scope weather", f"{warning}. Bring the scope in.")

    def _stalled(self, target: str | None, job: dict) -> bool:
        """True when the frame count has not moved for STALL_S."""
        count = job.get("captured") or 0
        name, last, since = self._progress
        if name != target or count != last:
            self._progress = (target, count, self.mono())
            return False
        return self.mono() - since > STALL_S

    def _account(self, target: str | None, job: dict) -> int:
        """Track rejected frames in a row and credit accepted frames as integration time.

        Any accepted frame since the last tick resets the streak. Returns the streak."""
        captured, failed = job.get("captured") or 0, job.get("failed") or 0
        name, last_c, last_f, streak = self._streak
        if name != target or captured < last_c:
            last_c = last_f = streak = 0
        accepted = (captured - last_c) - (failed - last_f)
        if accepted > 0:
            streak = 0
            self._credit(target, accepted)
        else:
            streak += failed - last_f
        self._streak = (target, captured, failed, streak)
        return streak

    def _credit(self, target: str | None, frames: int) -> None:
        cand = next((c for c in self._get_targets() if c.name == target), None)
        if cand is None:
            return
        exposure = scope_plan.capture_settings(cand).exposure_s
        self._integrated[cand.name] = self._integrated.get(cand.name, 0.0) + frames * exposure
        self._save_progress(dict(self._integrated))

    def _goal_reached(self) -> set[str]:
        return {
            c.name
            for c in self._get_targets()
            if c.goal_hours and self._integrated.get(c.name, 0.0) >= c.goal_hours * 3600
        }

    def _probe(self, client) -> bool:
        """Shoot a few frames at the current pointing. Clear sky stacks, cloud does not."""
        self._set(state="probing", reason="checking the sky")
        try:
            client.call("startCapture", {"frames": PROBE_FRAMES, "exposure": str(EXPOSURE_S)})
        except ScopeServiceError as exc:
            if BUSY_CODE not in str(exc):
                raise
            self._sky_wait_until = time.time() + BUSY_WAIT_S
            return False
        end = time.time() + PROBE_TIMEOUT_S
        job: dict = {}
        while time.time() < end:
            time.sleep(self.probe_poll_s)
            job = (client.call("getStatus") or {}).get("job") or {}
            if job.get("state") != "running":
                break
        else:
            client.call("stopCapture")
        captured = job.get("captured") or 0
        failed = job.get("failed") or 0
        return captured >= PROBE_FRAMES and failed <= PROBE_MAX_FAILED

    def _park(self, client) -> None:
        """Close the barrel once per day. A service without the operation stays as it is."""
        try:
            client.call("closeBarrel", timeout=90)
            self._parked = True
        except ScopeServiceError as exc:
            if exc.code == "unsupported":
                self._parked = True
            else:
                raise

    def _step(self, client, loc, now, job, capturing) -> None:
        obs = self._observer.snapshot()
        current = obs.get("target")
        streak = self._account(current, job) if current else 0
        if obs.get("state") == "running" and obs.get("step") != "capturing":
            self._set(state="observing", target=current, reason=obs.get("step"))
            return
        if obs.get("state") == "failed" and self._handled_failure != obs.get("error"):
            self._handled_failure = obs.get("error")
            if obs.get("step") == "calibrating":
                self._sky_wait_until = time.time() + SKY_WAIT_S
                self._notify("Scope", "Sky not clear: calibration failed. Waiting and probing.")
            else:
                self._skip_target(current, SKIP_AFTER_ERROR_S)
        if not capturing and self._sky_wait_until:
            if time.time() < self._sky_wait_until:
                self._set(state="waiting", reason="sky not clear, probing again soon")
                return
            if not self._probe(client):
                self._sky_wait_until = max(self._sky_wait_until, time.time() + SKY_WAIT_S)
                self._set(state="waiting", reason="sky not clear, probing again soon")
                return
            self._sky_wait_until = 0.0
            self._notify("Scope", "Sky is clear again")
        if capturing:
            cand = next((c for c in self._get_targets() if c.name == current), None)
            stalled = self._stalled(current, job)
            rejecting = streak >= FAIL_STREAK
            done = current in self._goal_reached()
            bad = (
                stalled
                or rejecting
                or done
                or cand is None
                or scope_plan.usable(cand, loc, now) is None
            )
            if not bad:
                self._set(state="observing", target=current, reason="capturing")
                return
            if rejecting or stalled:
                self._skip_target(current, SKIP_AFTER_FAIL_S)
                why = "stalled" if stalled else f"{FAIL_STREAK} frames in a row rejected"
                self._notify("Scope", f"Leaving {current}: {why}")
            if done:
                self._notify("Scope", f"Integration goal reached for {current}")
            client.call("stopCapture")
            self._set(state="switching", reason=f"leaving {current}")
            return
        pick = scope_plan.pick_target(
            self._get_targets(),
            loc,
            now,
            skip=frozenset(self._active_skips() | self._goal_reached()),
        )
        if pick is None:
            self._set(state="waiting", reason="no usable target, retrying")
            return
        c = pick.target
        cs = scope_plan.capture_settings(c)
        self._observer.start(
            client, c.name, c.ra_deg, c.dec_deg, FRAMES_PER_RUN, cs.exposure_s, cs.gain, cs.filter
        )
        if c.name != self._last_target:
            self._notify("Scope", f"Now shooting {c.name}")
            self._last_target = c.name
        self._set(state="observing", target=c.name, reason="starting")

    def start(self) -> None:
        def loop() -> None:
            while True:
                try:
                    self.tick()
                except Exception:
                    log.exception("scope scheduler tick failed")
                time.sleep(self.interval_s)

        threading.Thread(target=loop, name="astrolab-scope-scheduler", daemon=True).start()
