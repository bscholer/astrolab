"""Walk the tele along the skyline the panorama traced, to check it end to end.

Each point on the skyline (altitude per degree of azimuth, true north with a compass) is
turned back into motor angles with the geometry the stitch fitted for this mount, so this
also checks that geometry: if the tele frame follows the rooflines and treetops in the live
view, the panorama, the compass and the horizon all agree with the scope.

The wide camera's fitted geometry stands in for the tele's. The two sit side by side in the
head; on the fence the wide looked 172.9 - pitch degrees from the axis, the tele by the
relay's own numbers 172.5 - pitch. Close enough for a rough trace.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from astropy.coordinates import AltAz, EarthLocation, get_sun
from astropy.time import Time

from server import pano_sky
from server.pano_stitch import Lens, motor_rotation
from server.scope_service import ScopeServiceClient
from server.telemetry import fail, note, traced

log = logging.getLogger(__name__)

STEP_AZ_DEG = 10.0
LIFT_DEG = 1.0  # aim just above the edge, so the frame shows the edge and a little sky
DWELL_S = 4.0
POINT_TIMEOUT_S = 120
SUN_MAX_ALT_DEG = -6.0
SUN_AVOID_DEG = 30.0  # never point the tele this close to the Sun while it is up
PITCH_MAX = 175.0


class TraceRefused(Exception):
    pass


@dataclass(frozen=True)
class TracePoint:
    az: float
    alt: float
    pitch: float
    rotation: float


def _world_dir(alt: float, az: float) -> np.ndarray:
    a, z = math.radians(alt), math.radians(az)
    return np.array([math.cos(a) * math.sin(z), math.cos(a) * math.cos(z), math.sin(a)])


def motors_for(
    alt: float, az: float, lens: Lens, frame: np.ndarray, mirrored: bool = False
) -> tuple[float, float]:
    """Pitch and rotation that point the camera at this altitude and azimuth.

    frame holds east, north and up as mount-frame rows. The first guess ignores the small
    trims of the camera in its mount; a few Newton steps through the full model add them.
    Every direction can also be reached past the axis, with the rotation turned half way
    round; mirrored asks for that one.
    """
    want = frame.T @ _world_dir(alt, az)  # mount frame
    t = math.degrees(math.acos(max(-1.0, min(1.0, want[2]))))
    rotation = math.degrees(math.atan2(want[1], want[0])) % 360
    if mirrored:
        t, rotation = -t, (rotation + 180) % 360
    pitch = lens.axis_offset - t / lens.pitch_scale
    x = np.array([pitch, rotation])
    for _ in range(6):
        f0 = motor_rotation(x[0], x[1], lens)[:, 2]
        r = f0 - want
        jac = np.zeros((3, 2))
        for k in range(2):
            d = np.zeros(2)
            d[k] = 1e-3
            jac[:, k] = (motor_rotation(*(x + d), lens)[:, 2] - f0) / 1e-3
        step, *_ = np.linalg.lstsq(jac, -r, rcond=None)
        x = x + step
        if np.abs(step).max() < 1e-4:
            break
    return float(x[0]), float(x[1]) % 360


def plan_trace(
    folder: Path,
    sun_alt_az: tuple[float, float] | None = None,
    step_az: float = STEP_AZ_DEG,
) -> list[TracePoint]:
    """Points along the scan's skyline the motors can reach, in one sweep along it."""
    fit = json.loads((folder / "fit.json").read_text())
    lens = Lens(**fit["lens"])
    frame = np.array(fit["world_rows_enu"])
    horizon = pano_sky.load_horizon(folder) or {}
    if not horizon.get("compass"):
        raise TraceRefused("the panorama has no compass, so its azimuths are not the sky's")
    alts = horizon["alt"]
    manifest = json.loads((folder / "manifest.json").read_text())
    lo, hi = manifest.get("rotation_limits") or (0.0, 360.0)
    points = []
    n = len(alts)
    for k in range(int(round(360 / step_az))):
        az = k * step_az
        top = alts[int(az / 360 * n) % n]
        if top is None:
            continue
        alt = max(top, 0.0) + LIFT_DEG
        if sun_alt_az and sun_alt_az[0] > SUN_MAX_ALT_DEG:
            sep = math.degrees(
                math.acos(float(np.clip(_world_dir(alt, az) @ _world_dir(*sun_alt_az), -1, 1)))
            )
            if sep < SUN_AVOID_DEG:
                continue
        for mirrored in (False, True):
            pitch, rotation = motors_for(alt, az, lens, frame, mirrored)
            if 0.0 <= pitch <= PITCH_MAX and lo <= rotation <= hi:
                points.append(TracePoint(az, round(alt, 2), round(pitch, 2), round(rotation, 2)))
                break
    return _along_the_skyline(points)


def _along_the_skyline(points: list[TracePoint]) -> list[TracePoint]:
    """In azimuth order, starting just after the widest gap, so the tele follows the skyline
    in one sweep and never has to cross the part the motors cannot reach."""
    if len(points) < 2:
        return points
    points = sorted(points, key=lambda p: p.az)
    gaps = [(points[(k + 1) % len(points)].az - p.az) % 360 for k, p in enumerate(points)]
    start = (int(np.argmax(gaps)) + 1) % len(points)
    return points[start:] + points[:start]


def sun_alt_az(loc: EarthLocation, when: datetime | None = None) -> tuple[float, float]:
    t = Time(when or datetime.now(UTC), scale="utc")
    s = get_sun(t).transform_to(AltAz(obstime=t, location=loc))
    return float(s.alt.deg), float(s.az.deg)


class HorizonTrace:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.state: dict = {"state": "idle"}

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.state)

    def _set(self, **kw) -> None:
        with self._lock:
            self.state.update(kw)

    def busy(self) -> bool:
        return self.snapshot().get("state") == "running"

    def start(
        self,
        client: ScopeServiceClient,
        folder: Path,
        loc: EarthLocation,
        tele_covered: bool = False,
        when: datetime | None = None,
        dwell_s: float = DWELL_S,
    ) -> int:
        sun = sun_alt_az(loc, when)
        if sun[0] > SUN_MAX_ALT_DEG and not tele_covered:
            raise TraceRefused(
                f"the Sun is {sun[0]:.0f} degrees up: cover the tele lens, or wait for dark"
            )
        points = plan_trace(folder, sun)
        if not points:
            raise TraceRefused("no point on the skyline is within the scope's reach")
        with self._lock:
            if self.state.get("state") == "running":
                raise TraceRefused("a trace is already running")
            self._stop.clear()
            self.state = {"state": "running", "scan": folder.name, "done": 0, "total": len(points)}
        threading.Thread(
            target=self._run, args=(client, points, dwell_s), name="astrolab-trace", daemon=True
        ).start()
        return len(points)

    def stop(self) -> None:
        self._stop.set()

    @traced("scope.trace")
    def _run(self, client, points: list[TracePoint], dwell_s: float) -> None:
        note(points=len(points))
        try:
            for k, p in enumerate(points):
                if self._stop.is_set():
                    self._set(state="stopped")
                    return
                self._set(az=p.az, alt=p.alt, pitch=p.pitch, rotation=p.rotation)
                client.call(
                    "pointScope",
                    {"pitch": p.pitch, "rotation": p.rotation},
                    timeout=POINT_TIMEOUT_S,
                )
                self._stop.wait(dwell_s)
                self._set(done=k + 1)
            self._set(state="done")
        except Exception as exc:
            fail(str(exc))
            self._set(state="failed", error=str(exc))
