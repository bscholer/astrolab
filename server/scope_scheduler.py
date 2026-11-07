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
FAIL_MIN_FRAMES = 6
FAIL_RATIO = 0.5
SKIP_AFTER_FAIL_S = 30 * 60
SKIP_AFTER_ERROR_S = 15 * 60


def failing(job: dict) -> bool:
    """True when most frames of a run were rejected, after a minimum sample."""
    captured, failed = job.get("captured") or 0, job.get("failed") or 0
    return captured >= FAIL_MIN_FRAMES and failed / captured > FAIL_RATIO


class Scheduler:
    def __init__(
        self,
        get_client: Callable[[], ScopeServiceClient | None],
        get_site: Callable[[], EarthLocation | None],
        get_targets: Callable[[], list[Candidate]],
        observer: Observer,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        interval_s: float = 60.0,
    ) -> None:
        self._get_client = get_client
        self._get_site = get_site
        self._get_targets = get_targets
        self._observer = observer
        self._clock = clock
        self.interval_s = interval_s
        self.enabled = False
        self._lock = threading.Lock()
        self._skip: dict[str, float] = {}
        self.state: dict = {"state": "off"}

    def snapshot(self) -> dict:
        with self._lock:
            return {**self.state, "enabled": self.enabled, "skipped": sorted(self._active_skips())}

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
        try:
            job = (client.call("getStatus") or {}).get("job") or {}
            capturing = job.get("state") == "running"
            if not scope_plan.in_window(loc, now):
                if capturing:
                    client.call("stopCapture")
                self._set(state="waiting", reason="outside the night window")
                return
            self._step(client, loc, now, job, capturing)
        except ScopeServiceError as exc:
            self._set(state="error", reason=str(exc))

    def _step(self, client, loc, now, job, capturing) -> None:
        obs = self._observer.snapshot()
        current = obs.get("target")
        if obs.get("state") == "running" and obs.get("step") != "capturing":
            self._set(state="observing", target=current, reason=obs.get("step"))
            return
        if obs.get("state") == "failed":
            self._skip_target(current, SKIP_AFTER_ERROR_S)
        if capturing:
            cand = next((c for c in self._get_targets() if c.name == current), None)
            bad = failing(job) or cand is None or scope_plan.usable(cand, loc, now) is None
            if not bad:
                self._set(state="observing", target=current, reason="capturing")
                return
            if failing(job):
                self._skip_target(current, SKIP_AFTER_FAIL_S)
            client.call("stopCapture")
            self._set(state="switching", reason=f"leaving {current}")
            return
        pick = scope_plan.pick_target(
            self._get_targets(), loc, now, skip=frozenset(self._active_skips())
        )
        if pick is None:
            self._set(state="waiting", reason="no usable target, retrying")
            return
        c = pick.target
        self._observer.start(client, c.name, c.ra_deg, c.dec_deg, FRAMES_PER_RUN, EXPOSURE_S)
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
