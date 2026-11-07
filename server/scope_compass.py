"""Learn how the scope's motor angles map to the sky: slew to spread-out points and note both."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

import astropy.units as u
from astropy.coordinates import AltAz, EarthLocation, SkyCoord
from astropy.time import Time

from server import sky
from server.scope_pano import sun_altitude_deg
from server.scope_plan import Candidate, moon_separation_deg
from server.scope_service import SLEW_TIMEOUT_S, ScopeServiceClient

log = logging.getLogger(__name__)

SUN_MAX_ALT_DEG = -12.0
MOON_MIN_SEP_DEG = 20.0
PREPARE_TIMEOUT_S = 90
CALIBRATE_TIMEOUT_S = 330
SETTLE_S = 5.0
SKY_POINTS = ((45.0, 120.0), (45.0, 200.0), (45.0, 280.0), (45.0, 20.0), (70.0, 200.0))


class CompassRefused(Exception):
    pass


def point_to_radec(alt: float, az: float, loc: EarthLocation, at: datetime) -> tuple[float, float]:
    t = Time(sky._ensure_utc(at), scale="utc")
    frame = AltAz(obstime=t, location=loc)
    icrs = SkyCoord(alt=alt * u.deg, az=az * u.deg, frame=frame).icrs
    return float(icrs.ra.deg), float(icrs.dec.deg)


class CompassRun:
    def __init__(self, store: Callable[[dict], None]) -> None:
        self._store = store
        self._lock = threading.Lock()
        self.state: dict = {"state": "idle"}

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.state)

    def _set(self, **kw) -> None:
        with self._lock:
            self.state.update(kw)

    def start(
        self,
        client: ScopeServiceClient,
        loc: EarthLocation,
        points=SKY_POINTS,
        settle_s: float = SETTLE_S,
        when: datetime | None = None,
    ) -> None:
        sun = sun_altitude_deg(loc, when)
        if sun > SUN_MAX_ALT_DEG:
            limit = f"the limit is {SUN_MAX_ALT_DEG:.0f}"
            raise CompassRefused(f"the Sun is {sun:.0f} degrees up, {limit}")
        with self._lock:
            if self.state.get("state") == "running":
                raise CompassRefused("a run is already going")
            self.state = {
                "state": "running",
                "step": "preparing",
                "done": 0,
                "total": len(points),
                "failed": 0,
                "error": None,
            }
        threading.Thread(
            target=self._run,
            args=(client, loc, points, settle_s),
            name="astrolab-compass",
            daemon=True,
        ).start()

    def _run(self, client, loc, points, settle_s) -> None:
        try:
            client.call("prepareScope", timeout=PREPARE_TIMEOUT_S)
            self._set(step="calibrating")
            client.call("calibrateScope", timeout=CALIBRATE_TIMEOUT_S)
            failed = 0
            for alt, az in points:
                self._set(step=f"slewing to altitude {alt:.0f}, azimuth {az:.0f}")
                if not self._slew(client, loc, alt, az, settle_s):
                    failed += 1
                self._set(done=self.snapshot()["done"] + 1, failed=failed)
            self._set(state="done", step="done")
        except Exception as exc:
            self._set(state="failed", error=f"{self.snapshot().get('step')}: {exc}")

    def _slew(self, client, loc, alt, az, settle_s) -> bool:
        try:
            now = datetime.now(UTC)
            ra, dec = point_to_radec(alt, az, loc, now)
            if moon_separation_deg(Candidate("p", ra, dec), loc, now) < MOON_MIN_SEP_DEG:
                return False
            name = f"compass {alt:.0f}/{az:.0f}"
            client.call(
                "gotoTarget",
                {"name": name, "ra_hours": ra / 15, "dec_degrees": dec},
                timeout=SLEW_TIMEOUT_S,
            )
            time.sleep(settle_s)
            status = client.call("getStatus") or {}
            at = datetime.now(UTC)
            true_alt, true_az = sky.alt_az_now(ra, dec, loc, at)
            self._store(
                {
                    "target": name,
                    "ra_deg": ra,
                    "dec_deg": dec,
                    "pitch": status["pitch"],
                    "rotation": status["rotation"],
                    "alt_deg": true_alt,
                    "az_deg": true_az,
                    "t": at.timestamp(),
                }
            )
            return True
        except Exception as exc:
            log.warning("compass point %s/%s failed: %s", alt, az, exc)
            return False
