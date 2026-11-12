"""A pretend DWARF 3 behind the relay's operations, driven by a clock.

Captures are worked out from the clock when someone looks, not by a background thread:
a job knows when it started, so a status read counts the frames since then and decides
each one from where the scope pointed at that moment. That makes the same code run at
real time in the cluster and many nights a second in tests.

Mount: level alt-az. Pitch 0 is the closed barrel and 172.5 is straight up, as on the real
scope, so altitude is pitch - 82.5. Rotation is azimuth plus a fixed offset.
"""

from __future__ import annotations

import io
import math
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import astropy.units as u
import numpy as np
from astropy.coordinates import AltAz, EarthLocation, get_sun
from astropy.io import fits
from astropy.time import Time

from server.fakescope.scene import Scene

SPEC_VERSION = "0.1"
CAPABILITIES = ["status", "setup", "goto", "capture", "files", "delete", "preview", "pointing"]
PITCH_ZENITH = 172.5
PITCH_CLOSED = 12.5  # where the real firmware settles after closing
ROTATION_OFFSET = 37.0
FRAME_GAP_S = 2.0  # readout and save between exposures
FILTER_LABELS = {"vis": "VIS", "astro": "Astro", "duo": "Duo-Band"}
FITS_SHAPE = (270, 480)
SUN_LIMIT_DEG = 30.0
# Seconds each slow operation takes at real time. Tests run with a scale of 0.
LATENCY_S = {
    "prepare": 20,
    "calibrate": 45,
    "goto": 25,
    "point": 5,
    "close": 8,
    "wide": 2,
    "tele": 8,
}


class ScopeError(Exception):
    """An error the relay would return: a message, a code, and an HTTP status."""

    def __init__(self, message: str, code: str = "scope_error", status: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class RealClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class FakeClock:
    """Time that only moves when told. sleep() moves it, so nothing waits for real."""

    def __init__(self, start: datetime) -> None:
        self.t = start.astimezone(UTC)

    def now(self) -> datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)

    def time(self) -> float:
        return self.t.timestamp()

    def monotonic(self) -> float:
        return self.t.timestamp()


@dataclass
class Shot:
    name: str
    at: datetime
    ok: bool
    obj: str
    ra_deg: float
    dec_deg: float
    exposure: int
    gain: int
    filter_label: str


def alt_az_of(
    ra_deg: float, dec_deg: float, loc: EarthLocation, at: datetime
) -> tuple[float, float]:
    """Altitude and azimuth from sidereal time. Good to about 0.5 degree, plenty for a fake.

    No precession or refraction, so it is much faster than an astropy transform.
    """
    jd = at.timestamp() / 86400 + 2440587.5
    gmst = (280.46061837 + 360.98564736629 * (jd - 2451545.0)) % 360
    ha = math.radians((gmst + float(loc.lon.deg) - ra_deg) % 360)
    lat, dec = math.radians(float(loc.lat.deg)), math.radians(dec_deg)
    sin_alt = math.sin(dec) * math.sin(lat) + math.cos(dec) * math.cos(lat) * math.cos(ha)
    alt = math.asin(max(-1.0, min(1.0, sin_alt)))
    az = math.atan2(
        -math.sin(ha) * math.cos(dec),
        math.sin(dec) * math.cos(lat) - math.cos(dec) * math.sin(lat) * math.cos(ha),
    )
    return math.degrees(alt), math.degrees(az) % 360


def sun_alt_az(loc: EarthLocation, at: datetime) -> tuple[float, float]:
    t = Time(at)
    aa = get_sun(t).transform_to(AltAz(obstime=t, location=loc))
    return float(aa.alt.deg), float(aa.az.deg)


def _seed(*parts: object) -> int:
    import hashlib

    return int.from_bytes(hashlib.sha256(repr(parts).encode()).digest()[:4], "big")


def separation_deg(alt1: float, az1: float, alt2: float, az2: float) -> float:
    a1, a2, d = math.radians(alt1), math.radians(alt2), math.radians(az1 - az2)
    c = math.sin(a1) * math.sin(a2) + math.cos(a1) * math.cos(a2) * math.cos(d)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


class FakeScope:
    def __init__(self, scene: Scene | None = None, clock=None, latency_scale: float = 1.0) -> None:
        self.scene = scene or Scene()
        self.clock = clock or RealClock()
        self.latency_scale = latency_scale
        self.loc = EarthLocation(
            lat=self.scene.lat * u.deg,
            lon=self.scene.lon * u.deg,
            height=self.scene.elevation_m * u.m,
        )
        self.lock = threading.RLock()
        self.pitch = PITCH_CLOSED
        self.rotation = 180.0
        self.target: tuple[str, float, float] | None = None  # name, ra, dec while tracking
        self.calibrated = False
        self.job: dict | None = None
        self.sessions: dict[str, list[Shot]] = {}
        self._fits_cache: dict[str, bytes] = {}
        self.calls: list[str] = []

    # ---- pointing ---------------------------------------------------------

    def _wait(self, what: str) -> None:
        self.clock.sleep(LATENCY_S[what] * self.latency_scale)

    def pointing(self, at: datetime | None = None) -> tuple[float, float]:
        """Altitude and azimuth the scope points at, tracking a target if it has one."""
        at = at or self.clock.now()
        if self.target is not None:
            return alt_az_of(self.target[1], self.target[2], self.loc, at)
        alt = self.pitch - 82.5
        az = (self.rotation - ROTATION_OFFSET) % 360
        if alt > 90:  # tipped past straight up
            alt, az = 180 - alt, (az + 180) % 360
        return alt, az

    def _set_motors(self, alt: float, az: float) -> None:
        self.pitch = alt + 82.5
        self.rotation = (az + ROTATION_OFFSET) % 360

    # ---- operations, named as in the relay's API -------------------------

    def capabilities(self) -> dict:
        return {
            "spec_version": SPEC_VERSION,
            "service": {"name": "fakescope", "vendor": "astrolab", "model": "DWARF 3 (simulated)"},
            "capabilities": CAPABILITIES,
        }

    def status(self) -> dict:
        with self.lock:
            self.calls.append("getStatus")
            self._advance()
            if self.target is not None:
                self._set_motors(*self.pointing())
            return {
                "app_connected": False,
                "busy_elsewhere": False,
                "job": self._job_out(),
                "rotation": round(self.rotation, 4),
                "pitch": round(self.pitch, 4),
                "tele_darks": 18,
                "wide_darks": 3,
                "sd_free_gb": round(100.0 - sum(len(s) for s in self.sessions.values()) * 0.016, 2),
            }

    def prepare(self) -> dict:
        with self.lock:
            self.calls.append("prepareScope")
            self._refuse_while_capturing()
            self._wait("prepare")
            self.target = None
            self.pitch, self.rotation = PITCH_ZENITH, 180.0
            return {"message": "ready, pointing up"}

    def calibrate(self) -> dict:
        with self.lock:
            self.calls.append("calibrateScope")
            self._refuse_while_capturing()
            self._wait("calibrate")
            now = self.clock.now()
            alt, az = self.pointing(now)
            if sun_alt_az(self.loc, now)[0] > -6:
                raise ScopeError("calibration failed, code -11504, the sky is too bright")
            if not self.scene.frame_ok(alt, az, now, -1):
                raise ScopeError(f"calibration failed, code -11504, pitch {self.pitch:.1f}")
            self.calibrated = True
            return {"message": "calibrated"}

    def goto(self, name: str, ra_hours: float, dec_degrees: float) -> dict:
        with self.lock:
            self.calls.append("gotoTarget")
            self._refuse_while_capturing()
            now = self.clock.now()
            ra = ra_hours * 15
            alt, az = alt_az_of(ra, dec_degrees, self.loc, now)
            sun = sun_alt_az(self.loc, now)
            if separation_deg(alt, az, *sun) < SUN_LIMIT_DEG:
                raise ScopeError("target is too close to the Sun", "sun_too_close", 422)
            if alt < 0:
                raise ScopeError(f"{name} is below the horizon")
            self._wait("goto")
            self.target = (name, ra, dec_degrees)
            self._set_motors(*self.pointing())
            return {"message": f"tracking {name}"}

    def point(self, pitch: float, rotation: float | None) -> dict:
        with self.lock:
            self.calls.append("pointScope")
            if not 0 <= pitch <= 175 or (rotation is not None and not 0 <= rotation <= 360):
                raise ScopeError(
                    "pitch must be 0 to 175, rotation 0 to 360", "invalid_request", 422
                )
            self._refuse_while_capturing()
            self._wait("point")
            self.target = None
            self.pitch = pitch
            if rotation is not None:
                self.rotation = rotation
            return {"message": "pointed"}

    def close_barrel(self) -> dict:
        with self.lock:
            self.calls.append("closeBarrel")
            self._refuse_while_capturing()
            self._wait("close")
            self.target = None
            self.calibrated = False
            self.pitch = PITCH_CLOSED
            return {"message": "closed"}

    def start_capture(self, frames: int, exposure: str, gain: int, filter: str) -> dict:
        with self.lock:
            self.calls.append("startCapture")
            if (
                not 0 < frames <= 500
                or exposure not in ("15", "30", "60")
                or filter not in FILTER_LABELS
            ):
                raise ScopeError(
                    "frames must be 1 to 500, exposure 15, 30 or 60", "invalid_request", 422
                )
            self._advance()
            if self.job and self.job["state"] == "running":
                raise ScopeError("a capture job is already running", "busy", 409)
            if self.target is None:
                raise ScopeError("no target, go to one first")
            now = self.clock.now()
            name = self.target[0]
            stamp = f"{now:%Y-%m-%d-%H-%M-%S}-{now.microsecond // 1000:03d}"
            session = f"DWARF_RAW_TELE_{name}_EXP_{exposure}_GAIN_{gain}_{stamp}"
            self.sessions[session] = []
            self.job = {
                "id": uuid.uuid4().hex[:8],
                "state": "running",
                "frames": frames,
                "captured": 0,
                "failed": 0,
                "session": session,
                "error": None,
                "_start": now,
                "_exposure": int(exposure),
                "_gain": gain,
                "_filter": FILTER_LABELS[filter],
                "_target": self.target,
            }
            return self._job_out()

    def stop_capture(self) -> dict:
        with self.lock:
            self.calls.append("stopCapture")
            self._advance()
            if self.job and self.job["state"] == "running":
                self.job["state"] = "done"
                return {"message": "stop requested"}
            return {"message": "nothing to stop"}

    def list_files(self, path: str = "") -> dict:
        with self.lock:
            self._advance()
            if not path:
                return {
                    "entries": [{"name": s, "size": 0, "dir": True} for s in sorted(self.sessions)]
                }
            if path not in self.sessions:
                raise ScopeError(f"no such folder: {path}", "invalid_request", 422)
            size = len(self._fits_for(self.sessions[path][0])) if self.sessions[path] else 0
            return {
                "entries": [
                    {"name": s.name, "size": size, "dir": False} for s in self.sessions[path]
                ]
            }

    def read_chunk(self, path: str, offset: int, length: int) -> dict:
        import base64

        with self.lock:
            session, _, name = path.rpartition("/")
            shot = next((s for s in self.sessions.get(session, []) if s.name == name), None)
            if shot is None:
                raise ScopeError(f"no such file: {path}", "invalid_request", 422)
            data = self._fits_for(shot)
            part = data[offset : offset + length]
            return {
                "offset": offset,
                "size": len(data),
                "data": base64.b64encode(part).decode(),
                "eof": offset + len(part) >= len(data),
            }

    def delete_session(self, session: str) -> dict:
        with self.lock:
            self.calls.append("deleteSession")
            self._advance()
            if self.job and self.job["state"] == "running":
                raise ScopeError("a capture is running, stop it before deleting", "busy", 409)
            if self.sessions.pop(session, None) is None:
                raise ScopeError(f"no such session: {session}", "invalid_request", 422)
            return {"message": f"deleted {session}"}

    def frame(self, camera: str) -> dict:
        import base64

        from server.fakescope.render import render_frame

        if camera not in ("tele", "wide"):
            raise ScopeError(f"camera must be tele or wide, not {camera}", "invalid_request", 422)
        self._wait(camera)
        with self.lock:
            now = self.clock.now()
            alt, az = self.pointing(now)
        jpeg = render_frame(self.scene, camera, alt, az, now, sun_alt_az(self.loc, now))
        return {"camera": camera, "data": base64.b64encode(jpeg).decode()}

    # ---- captures -----------------------------------------------------------

    def _refuse_while_capturing(self) -> None:
        self._advance()
        if self.job and self.job["state"] == "running":
            raise ScopeError("the scope is busy with a capture", "busy", 409)

    def _job_out(self) -> dict | None:
        if self.job is None:
            return None
        return {k: v for k, v in self.job.items() if not k.startswith("_")}

    def _advance(self) -> None:
        """Count the frames a running job has shot by now."""
        job = self.job
        if not job or job["state"] != "running":
            return
        step = job["_exposure"] + FRAME_GAP_S
        due = int((self.clock.now() - job["_start"]).total_seconds() // step)
        due = min(due, job["frames"])
        name, ra, dec = job["_target"]
        shots = self.sessions[job["session"]]
        while job["captured"] < due:
            i = job["captured"]
            at = job["_start"] + timedelta(seconds=(i + 1) * step)
            alt, az = alt_az_of(ra, dec, self.loc, at)
            ok = self.scene.frame_ok(alt, az, at, i)
            stamp = f"{at:%Y%m%d-%H%M%S}{at.microsecond // 1000:03d}"
            fname = f"{name}_{job['_exposure']}s{job['_gain']}_{job['_filter']}_{stamp}_14C.fits"
            shots.append(
                Shot(
                    name=fname if ok else f"failed_{fname}",
                    at=at,
                    ok=ok,
                    obj=name,
                    ra_deg=ra,
                    dec_deg=dec,
                    exposure=job["_exposure"],
                    gain=job["_gain"],
                    filter_label=job["_filter"],
                )
            )
            job["captured"] += 1
            job["failed"] += 0 if ok else 1
        if job["captured"] >= job["frames"]:
            job["state"] = "done"

    def _fits_for(self, shot: Shot) -> bytes:
        """A small FITS file with the headers a real light carries."""
        cached = self._fits_cache.get(shot.name)
        if cached is not None:
            return cached
        rng = np.random.default_rng(_seed(shot.obj, round(shot.ra_deg, 3)))
        h, w = FITS_SHAPE
        img = rng.normal(300, 12, (h, w))
        if shot.ok:
            ys, xs = rng.uniform(0, h, 60), rng.uniform(0, w, 60)
            amps = rng.uniform(300, 3500, 60)
            jitter = np.random.default_rng(_seed(shot.name)).normal(0, 0.7, 2)
            yy, xx = np.mgrid[0:h, 0:w]
            for y, x, a in zip(ys + jitter[0], xs + jitter[1], amps, strict=True):
                img += a * np.exp(-((yy - y) ** 2 + (xx - x) ** 2) / (2 * 1.4**2))
        else:
            img = rng.normal(600, 40, (h, w))  # cloud or a tree: no stars to find
        data = np.clip(img, 0, 4095).astype(np.uint16)
        hdu = fits.PrimaryHDU(data)
        hdr = hdu.header
        hdr["DATE-OBS"] = (
            shot.at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{shot.at.microsecond // 1000:03d}",
            "Time end of exposure",
        )
        hdr["EXPTIME"] = (float(shot.exposure), "[s] Exposure Time")
        hdr["GAIN"] = (shot.gain, "Gain")
        hdr["FILTER"] = (shot.filter_label, "selected filter")
        hdr["CAMERA"] = ("TELE", "shooting camera")
        hdr["XPIXSZ"] = (2.0, "[um] pixel size in microns (with binning)")
        hdr["YPIXSZ"] = (2.0, "[um] pixel size in microns (with binning)")
        hdr["FOCALLEN"] = (150.0, "[mm] Focal length of telescope in mm")
        hdr["DET-TEMP"] = (14, "Detector temperature in C")
        hdr["RA"] = (round(shot.ra_deg, 4), "[deg] Observe object RA coordinate (J2000)")
        hdr["DEC"] = (round(shot.dec_deg, 5), "[deg] Observe object DEC coordinate (J2000)")
        hdr["OBJECT"] = (shot.obj, "Observe object")
        hdr["BAYERPAT"] = ("RGGB", "Bayer pattern")
        hdr["TELESCOP"] = ("DWARF 3", "DWARFLAB telescope")
        hdr["INSTRUME"] = ("DWARF 3", "DWARFLAB instrument")
        hdr["ORIGIN"] = ("DWARFLAB", "DWARFLAB TEAM")
        buf = io.BytesIO()
        hdu.writeto(buf)
        out = buf.getvalue()
        if len(self._fits_cache) > 400:
            self._fits_cache.clear()
        self._fits_cache[shot.name] = out
        return out
