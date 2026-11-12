"""Run astrolab's real scheduler against the fake scope, many nights in a few seconds.

The scheduler, observer and sky map are the production classes. Only the clock and the
scope are fake: time moves when the loop or a slow scope operation says so, so a night of
one-minute ticks with realistic calibration and slew times runs without waiting.

    report = run_nights(Scene(seed=4), targets, start=datetime(...), nights=5)
"""

from __future__ import annotations

import contextlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from server import scope_observe, scope_scheduler, sky
from server.fakescope.client import InProcessClient
from server.fakescope.core import FakeClock, FakeScope
from server.fakescope.scene import Scene
from server.scope_observe import Observer
from server.scope_plan import Candidate
from server.scope_scheduler import Scheduler
from server.scope_skymap import CELL_DEG, SkyMap, cell_key, night_of, verdict


class _TimeShim:
    """Stands in for the time module inside the scheduler and observer."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock

    def time(self) -> float:
        return self._clock.time()

    def monotonic(self) -> float:
        return self._clock.monotonic()

    def sleep(self, seconds: float) -> None:
        self._clock.advance(seconds)


@contextlib.contextmanager
def _fake_time(clock: FakeClock):
    shim = _TimeShim(clock)
    saved = (scope_scheduler.time, scope_observe.time)
    scope_scheduler.time = shim
    scope_observe.time = shim
    try:
        yield
    finally:
        scope_scheduler.time, scope_observe.time = saved


@dataclass
class NightReport:
    night: str
    kind: str
    good: int = 0
    failed: int = 0
    by_target: Counter = field(default_factory=Counter)
    states: Counter = field(default_factory=Counter)


@dataclass
class SimReport:
    nights: list[NightReport]
    scope: FakeScope
    skymap: SkyMap

    def skymap_accuracy(self) -> dict:
        """How the learned patches compare with the yard: right and wrong calls of each kind."""
        out = Counter()
        for key, cell in self.skymap.cells().items():
            v = verdict(cell)
            if v == "unknown":
                out["unknown"] += 1
                continue
            alt0, az0 = (float(x) for x in key.split("/"))
            truly_blocked = self.scope.scene.blocked(alt0 + CELL_DEG / 2, az0 + CELL_DEG / 2)
            out[f"{v}_{'right' if (v == 'blocked') == truly_blocked else 'wrong'}"] += 1
        return dict(out)


def run_nights(
    scene: Scene,
    targets: list[Candidate],
    start: datetime,
    nights: int = 1,
    tick_s: float = 60.0,
    skymap: SkyMap | None = None,
) -> SimReport:
    """Run Run all night from start for the given number of nights. start should be daytime."""
    clock = FakeClock(start)
    scope = FakeScope(scene, clock=clock, latency_scale=1.0)
    client = InProcessClient(scope)
    loc = sky.observer_location(scene.lat, scene.lon, scene.elevation_m)
    store: dict = {}
    skymap = skymap or SkyMap(lambda: store.get("cells", {}), lambda c: store.update(cells=c))
    observer = Observer()
    with _fake_time(clock):
        sched = Scheduler(
            lambda: client,
            lambda: loc,
            lambda: targets,
            observer,
            clock=clock.now,
            interval_s=tick_s,
            skymap=skymap,
        )
        sched.mono = clock.monotonic
        sched.enabled = True
        end = start + timedelta(days=nights)
        reports: dict[str, NightReport] = {}
        while clock.now() < end:
            sched.tick()
            observer.join()
            now = clock.now()
            night = night_of(now, scene.lon)
            rep = reports.setdefault(night, NightReport(night, scene.night_kind(now)))
            rep.states[sched.snapshot().get("state", "?")] += 1
            clock.advance(tick_s)
    for shots in scope.sessions.values():
        for shot in shots:
            night = night_of(shot.at, scene.lon)
            rep = reports.setdefault(night, NightReport(night, scene.night_kind(shot.at)))
            rep.good += shot.ok
            rep.failed += not shot.ok
            rep.by_target[shot.obj] += shot.ok
    return SimReport(sorted(reports.values(), key=lambda r: r.night), scope, skymap)


def default_start() -> datetime:
    """Noon in Bend on a November day, before the first night."""
    return datetime(2026, 11, 12, 20, 0, tzinfo=UTC)


def patch_key(alt: float, az: float) -> str:
    return cell_key(alt, az)
