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
_UNSEEN = "<unseen>"

FRAMES_PER_RUN = 200
EXPOSURE_S = 15
FAIL_STREAK = 10
SKIP_AFTER_FAIL_S = 30 * 60
SKIP_AFTER_ERROR_S = 15 * 60
SKY_WAIT_S = 5 * 60
FAULT_WAIT_S = 2 * 60
CLOUD_CODE = "-11504"  # firmware: calibration found no stars to solve
MIN_FREE_GB = 2.0
STUCK_S = 45 * 60
STALL_S = 10 * 60
WEATHER_EVERY_S = 30 * 60
BARREL_OPEN_PITCH = 30.0


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
        self._handled_job: str | None = _UNSEEN
        self._owns_capture = False
        self._fault_alerted: str | None = None
        self._wait_reason = ""
        self._last_frame_at = time.monotonic()
        self._stuck_alerted = False
        self._low_disk_alerted = False
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
            changed = (kw.get("state"), kw.get("reason")) != (
                self.state.get("state"),
                self.state.get("reason"),
            )
            self.state = kw
        if changed:
            log.info("scheduler %s: %s", kw.get("state"), kw.get("reason") or kw.get("target"))

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
            self._check_disk(status)
            capturing = job.get("state") == "running"
            if capturing:
                self._owns_capture = True
            elif job.get("state") == "done":
                self._owns_capture = False
            if status.get("busy_elsewhere"):
                if self._owns_capture:
                    log.warning("scope still shoots a capture that astrolab started, stopping it")
                    self._owns_capture = False
                    client.call("stopCapture")
                    self._set(state="waiting", reason="stopping a capture the relay lost track of")
                    return
                self._set(state="waiting", reason="scope is busy with another capture")
                return
            if not scope_plan.in_window(loc, now):
                if capturing:
                    client.call("stopCapture")
                elif not self._parked:
                    self._park(client)
                self._last_frame_at = self.mono()
                self._set(state="waiting", reason="outside the night window")
                return
            self._parked = False
            self._step(client, loc, now, job, capturing)
            self._watch()
        except ScopeServiceError as exc:
            if self.state.get("state") != "error":
                self._notify("Scope error", str(exc))
            self._set(state="error", reason=str(exc))

    def _check_disk(self, status: dict) -> None:
        """Alert once when the scope SD card runs low. A full card refuses every capture."""
        free = status.get("sd_free_gb")
        if free is None or free >= MIN_FREE_GB:
            self._low_disk_alerted = False
        elif not self._low_disk_alerted:
            self._low_disk_alerted = True
            log.warning("scope SD card nearly full: %.1f GB free", free)
            self._notify("Scope", f"SD card nearly full ({free:.1f} GB free). Captures will fail.")

    def _watch(self) -> None:
        """Alert once when no accepted frame arrives for STUCK_S inside the night window."""
        idle = self.mono() - self._last_frame_at
        if idle > STUCK_S and not self._stuck_alerted:
            self._stuck_alerted = True
            why = self.state.get("reason") or self.state.get("state")
            log.warning("no accepted frames for %d min: %s", idle // 60, why)
            self._notify("Scope", f"No frames for {int(idle // 60)} min. Status: {why}")

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
        self._last_frame_at = self.mono()
        self._stuck_alerted = False
        self._integrated[cand.name] = self._integrated.get(cand.name, 0.0) + frames * exposure
        self._save_progress(dict(self._integrated))

    def _goal_reached(self) -> set[str]:
        return {
            c.name
            for c in self._get_targets()
            if c.goal_hours and self._integrated.get(c.name, 0.0) >= c.goal_hours * 3600
        }

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

    def _wait(self, reason: str, seconds: float) -> None:
        self._sky_wait_until = time.time() + seconds
        self._wait_reason = reason
        log.info("waiting %d min: %s", seconds // 60, reason)

    def _handle_observe_failure(self, target: str | None, obs: dict) -> None:
        error, step = obs.get("error") or "", obs.get("step")
        log.warning("observation of %s failed while %s: %s", target, step, error)
        if step == "calibrating" and CLOUD_CODE in error:
            self._wait("sky not clear: calibration found no stars", SKY_WAIT_S)
            self._notify("Scope", "Sky not clear: calibration failed. Waiting and retrying.")
        elif step in ("preparing", "calibrating"):
            self._wait(f"scope fault: {error}", FAULT_WAIT_S)
            if self._fault_alerted != error:
                self._fault_alerted = error
                self._notify("Scope", f"Scope fault, not cloud: {error}. Retrying soon.")
        else:
            self._skip_target(target, SKIP_AFTER_ERROR_S)

    def _step(self, client, loc, now, job, capturing) -> None:
        obs = self._observer.snapshot()
        current = obs.get("target")
        streak = self._account(current, job) if current else 0
        if obs.get("state") == "running" and obs.get("step") != "capturing":
            self._set(state="observing", target=current, reason=obs.get("step"))
            return
        if self._handled_job == _UNSEEN:
            self._handled_job = job.get("id")
        if job.get("state") == "failed" and job.get("id") != self._handled_job:
            self._handled_job = job.get("id")
            error = job.get("error") or "unknown error"
            log.warning("capture job %s failed: %s", job.get("id"), error)
            self._notify("Scope", f"Capture job failed: {error}")
            client.call("stopCapture")
            self._set(state="waiting", reason=f"capture job failed: {error}")
            return
        if obs.get("state") == "failed" and self._handled_failure != obs.get("error"):
            self._handled_failure = obs.get("error")
            self._handle_observe_failure(current, obs)
        if not capturing and time.time() < self._sky_wait_until:
            self._set(state="waiting", reason=self._wait_reason)
            return
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
        self._sky_wait_until = 0.0
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
