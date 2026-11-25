"""Scan the scope's surroundings with the wide camera and stitch the frames into a panorama.

The scan finds the rotation motor's stops, then turns through rings of pitch that overlap
enough to stitch, saving each frame with the motor positions the scope reports (not the ones
asked for: asking for rotation 0 lands at 12 to 16). server.pano_stitch does the rest, in a
child process so the API stays responsive.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import psutil
from astropy.coordinates import AltAz, EarthLocation, get_sun
from astropy.time import Time

from server import pano_sky
from server.scope_service import ScopeServiceClient
from server.telemetry import fail, note, traced

log = logging.getLogger(__name__)

SUN_MAX_ALT_DEG = -6.0
SETTLE_S = 2.0
RETRY_WAIT_S = 5.0
POINT_TIMEOUT_S = 120
FRAME_TIMEOUT_S = 60
STITCH_TIMEOUT_S = 1800
# A stitch of 80 to 100 frames peaks near 1.7 GB of real memory. One that runs away is
# stopped at this, so it fails alone instead of starving the API and the pipeline jobs.
# (A cap on address space does not work: OpenCV reserves far more than it uses, one malloc
# arena per thread, and the box has 32 cores.)
STITCH_MEMORY_BYTES = 8 * 1024**3
STITCH_THREADS = 8
WATCH_S = 0.5

# The wide frame is 46 x 27 degrees. Pitch 175 is as close to the rotation axis as the wide
# camera gets, and below pitch 50 it looks into the scope's own base.
HFOV_DEG, VFOV_DEG = 46.0, 27.0
PLAN_MAX_PITCH, PLAN_MIN_PITCH = 175.0, 50.0
OVERLAP = 0.35
# Where the camera is square to the axis depends on the setup (about pitch 81 on a level
# tripod, 105 on the equatorial mount). The plan only uses it to space frames in a ring, so
# the tripod value is fine for both: it errs toward more frames.
SQUARE_PITCH = 81.0


class PanoRefused(Exception):
    pass


def sun_altitude_deg(loc: EarthLocation, when: datetime | None = None) -> float:
    t = Time(when or datetime.now(UTC), scale="utc")
    return float(get_sun(t).transform_to(AltAz(obstime=t, location=loc)).alt.deg)


def plan_stops(
    rot_min: float,
    rot_max: float,
    max_pitch: float = PLAN_MAX_PITCH,
    min_pitch: float = PLAN_MIN_PITCH,
    overlap: float = OVERLAP,
) -> list[tuple[float, float]]:
    """Pitch and rotation of every frame: rings of pitch, each from one rotation stop to the other.

    Rings near the axis need fewer frames, since a frame there spans more of the turn.
    Alternate rings run backwards so the scope never swings all the way back between them.
    """
    step = VFOV_DEG * (1 - overlap)
    rings = int(math.ceil((max_pitch - min_pitch) / step)) + 1
    pitches = np.linspace(max_pitch, min_pitch, rings)
    half_w = math.radians(HFOV_DEG / 2)
    reach = rot_max - rot_min
    stops = []
    for k, pitch in enumerate(pitches):
        t = 90.0 + (SQUARE_PITCH - pitch)  # degrees from the axis
        lo, hi = t - VFOV_DEG / 2, t + VFOV_DEG / 2
        widest = 90.0 if lo <= 90 <= hi else (hi if hi < 90 else lo)  # edge nearest square
        s = math.sin(math.radians(min(max(widest, 1.0), 179.0)))
        # Near the axis a frame spans the whole turn; elsewhere the arc its width covers.
        full = s <= math.sin(half_w)
        span = 180.0 if full else 2 * math.degrees(math.asin(math.sin(half_w) / s))
        n = max(2, math.ceil(reach / (span * (1 - overlap))) + 1)
        rots = [rot_min + i * reach / (n - 1) for i in range(n)]
        if k % 2:
            rots.reverse()
        stops += [(round(float(pitch), 2), round(r, 2)) for r in rots]
    return stops


def latest_scan(root: Path) -> str | None:
    """The newest scan folder with a manifest, so a restart still finds the last scan."""
    if not root.is_dir():
        return None
    scans = [p for p in root.iterdir() if (p / "manifest.json").is_file()]
    return max(scans, key=lambda p: p.stat().st_mtime).name if scans else None


def read_result(folder: Path) -> dict | None:
    path = folder / "pano.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def run_stitch(folder: Path, latitude: float | None) -> dict:
    """Stitch in a child process, watched for memory. Returns pano.json."""
    cmd = [sys.executable, "-m", "server.pano_stitch", str(folder)]
    if latitude is not None:
        cmd += ["--latitude", str(latitude)]
    threads = str(STITCH_THREADS)
    env = dict(
        os.environ,
        OMP_NUM_THREADS=threads,
        OPENBLAS_NUM_THREADS=threads,
        ASTROLAB_STITCH_THREADS=threads,
    )
    with tempfile.TemporaryFile("w+") as out:
        proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, text=True, env=env)
        reason = _watch(proc)
        out.seek(0)
        lines = out.read().strip().splitlines()
    if reason:
        raise RuntimeError(f"stitching failed: {reason}")
    if proc.returncode != 0:
        last = next((x for x in reversed(lines) if x.strip()), "no output")
        log.warning("pano stitch failed (%s):\n%s", proc.returncode, "\n".join(lines[-20:]))
        raise RuntimeError(f"stitching failed: {last}")
    result = read_result(folder)
    if result is None:
        raise RuntimeError("stitching wrote no result")
    return result


def _watch(proc: subprocess.Popen) -> str | None:
    """Wait for the stitch. Stop it when it runs too long or uses too much memory."""
    deadline = time.monotonic() + STITCH_TIMEOUT_S
    try:
        child = psutil.Process(proc.pid)
    except psutil.Error:
        child = None
    while proc.poll() is None:
        if time.monotonic() > deadline:
            proc.kill()
            proc.wait()
            return f"took longer than {STITCH_TIMEOUT_S // 60} minutes"
        try:
            rss = child.memory_info().rss if child else 0
        except psutil.Error:
            rss = 0
        if rss > STITCH_MEMORY_BYTES:
            proc.kill()
            proc.wait()
            return f"used more than {STITCH_MEMORY_BYTES // 1024**3} GB of memory"
        time.sleep(WATCH_S)
    return None


class PanoScanner:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()
        scan = latest_scan(root)
        self.state: dict = {"state": "done", "id": scan} if scan else {"state": "idle"}
        if scan:
            self.state["result"] = read_result(root / scan)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.state)

    def _set(self, **kw) -> None:
        with self._lock:
            self.state.update(kw)

    def folder(self) -> Path | None:
        scan = self.snapshot().get("id")
        path = self.root / scan if scan else None
        return path if path and path.is_dir() else None

    def pano_path(self) -> Path | None:
        folder = self.folder()
        path = folder / "pano.jpg" if folder else None
        return path if path and path.is_file() else None

    def _claim(self, **state) -> None:
        with self._lock:
            if self.state.get("state") == "running":
                raise PanoRefused("a scan is already running")
            self.state = {"state": "running", **state}

    def start(
        self,
        client: ScopeServiceClient,
        loc: EarthLocation,
        latitude: float | None = None,
        when: datetime | None = None,
        tele_covered: bool = False,
        settle_s: float = SETTLE_S,
    ) -> str:
        alt = sun_altitude_deg(loc, when)
        if alt > SUN_MAX_ALT_DEG and not tele_covered:
            raise PanoRefused(
                f"the Sun is {alt:.0f} degrees up, the limit is {SUN_MAX_ALT_DEG:.0f}"
            )
        scan = uuid.uuid4().hex[:8]
        self._claim(id=scan, step="finding the rotation limits", done=0, total=0, failed=0)
        threading.Thread(
            target=self._run,
            args=(client, scan, latitude, settle_s),
            name="astrolab-pano",
            daemon=True,
        ).start()
        return scan

    def restitch(self, latitude: float | None = None) -> str:
        """Stitch the last scan again, after a change to the stitcher, say."""
        folder = self.folder()
        if folder is None or not (folder / "manifest.json").is_file():
            raise PanoRefused("no scan to stitch")
        self._claim(id=folder.name, step="stitching")
        threading.Thread(
            target=self._stitch, args=(folder, latitude), name="astrolab-pano-stitch", daemon=True
        ).start()
        return folder.name

    def _position(self, client) -> tuple[float | None, float | None]:
        """Motor angles as the scope reports them. The relay sometimes has none to give."""
        status = client.call("getStatus") or {}
        pitch, rotation = status.get("pitch"), status.get("rotation")
        return (
            float(pitch) if pitch is not None else None,
            float(rotation) if rotation is not None else None,
        )

    def find_rotation_stops(self, client, pitch: float) -> tuple[float, float]:
        """Ask for rotation 0 and 360 and see where the motor stops."""
        ends = []
        for want in (0.0, 360.0):
            client.call("pointScope", {"pitch": pitch, "rotation": want}, timeout=POINT_TIMEOUT_S)
            got = None
            for _ in range(3):
                got = self._position(client)[1]
                if got is not None:
                    break
                time.sleep(1.0)
            if got is None:
                raise RuntimeError("the scope does not report where the rotation motor is")
            ends.append(got)
        lo, hi = min(ends), max(ends)
        if hi - lo < 90:
            raise RuntimeError(f"the rotation motor only reaches {lo:.0f} to {hi:.0f}")
        return lo, hi

    def _frame(self, client, folder: Path, name: str, pitch, rot, settle_s) -> dict | None:
        for attempt in (1, 2):
            try:
                client.call(
                    "pointScope", {"pitch": pitch, "rotation": rot}, timeout=POINT_TIMEOUT_S
                )
                time.sleep(settle_s)
                got_pitch, got_rot = self._position(client)
                frame = client.call("getFrame", {"camera": "wide"}, timeout=FRAME_TIMEOUT_S)
                (folder / name).write_bytes(base64.b64decode(frame["data"]))
                # Within the probed stops the motors land where asked, so the asked angles
                # stand in when the scope reports none.
                return {
                    "name": name,
                    "pitch": got_pitch if got_pitch is not None else pitch,
                    "rotation": got_rot if got_rot is not None else rot,
                    "asked_pitch": pitch,
                    "asked_rotation": rot,
                    "reported": got_pitch is not None and got_rot is not None,
                    "t": datetime.now(UTC).isoformat(),
                }
            except Exception as exc:
                log.warning("pano frame %s/%s attempt %d failed: %s", pitch, rot, attempt, exc)
                time.sleep(RETRY_WAIT_S)
        return None

    @traced("scope.pano")
    def _run(self, client, scan: str, latitude: float | None, settle_s: float) -> None:
        note(scan=scan)
        folder = self.root / scan
        folder.mkdir(parents=True, exist_ok=True)
        try:
            lo, hi = self.find_rotation_stops(client, PLAN_MAX_PITCH)
            stops = plan_stops(lo, hi)
            self._set(step="scanning", total=len(stops), rotation_limits=[lo, hi])
            tiles, failed = [], 0
            manifest = {"rotation_limits": [lo, hi], "started": datetime.now(UTC).isoformat()}
            for k, (pitch, rot) in enumerate(stops):
                tile = self._frame(client, folder, f"t{k:03d}.jpg", pitch, rot, settle_s)
                if tile is None:
                    failed += 1
                else:
                    tiles.append(tile)
                manifest["tiles"] = tiles
                (folder / "manifest.json").write_text(json.dumps(manifest, indent=1))
                self._set(done=k + 1, failed=failed)
            if not tiles:
                raise RuntimeError("no frames came back")
        except Exception as exc:
            fail(str(exc))
            self._set(state="failed", error=str(exc))
            return
        self._stitch(folder, latitude)

    def _stitch(self, folder: Path, latitude: float | None) -> None:
        self._set(step="stitching")
        try:
            result = run_stitch(folder, latitude)
            self._set(step="finding the sky")
            try:
                pano_sky.detect(folder, float(result["alt_top"]))
                pano_sky.save_horizon(folder, result)
            except Exception:  # the panorama is still worth having without it
                log.exception("pano sky detection failed")
            self._set(state="done", step=None, result=result)
        except Exception as exc:
            fail(str(exc))
            self._set(state="failed", error=str(exc))
