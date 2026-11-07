"""Scan the scope's surroundings: point, grab a wide frame, and tile the frames into a mosaic."""

from __future__ import annotations

import base64
import logging
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from astropy.coordinates import AltAz, EarthLocation, get_sun
from astropy.time import Time
from PIL import Image

from server.scope_service import ScopeServiceClient

log = logging.getLogger(__name__)

SUN_MAX_ALT_DEG = -6.0
DEFAULT_PITCHES = (40.0, 70.0, 100.0, 130.0, 160.0)
DEFAULT_ROTATIONS = tuple(float(r) for r in range(0, 360, 45))
SETTLE_S = 3.0
TILE_W, TILE_H = 320, 180


class PanoRefused(Exception):
    pass


def sun_altitude_deg(loc: EarthLocation, when: datetime | None = None) -> float:
    t = Time(when or datetime.now(UTC), scale="utc")
    return float(get_sun(t).transform_to(AltAz(obstime=t, location=loc)).alt.deg)


def tile_name(pitch: float, rotation: float) -> str:
    return f"p{pitch:g}_r{rotation:g}.jpg"


def build_mosaic(folder: Path, pitches, rotations) -> Path:
    """Rows are pitch from high to low. Columns are rotation. Missing tiles stay black."""
    rows = sorted(pitches, reverse=True)
    sheet = Image.new("RGB", (TILE_W * len(rotations), TILE_H * len(rows)))
    for r, pitch in enumerate(rows):
        for c, rot in enumerate(rotations):
            path = folder / tile_name(pitch, rot)
            if path.is_file():
                tile = Image.open(path).convert("RGB").resize((TILE_W, TILE_H))
                sheet.paste(tile, (c * TILE_W, r * TILE_H))
    out = folder / "mosaic.jpg"
    sheet.save(out, "JPEG", quality=85)
    return out


class PanoScanner:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()
        self.state: dict = {"state": "idle"}

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.state)

    def _set(self, **kw) -> None:
        with self._lock:
            self.state.update(kw)

    def mosaic_path(self) -> Path | None:
        scan = self.snapshot().get("id")
        path = self.root / scan / "mosaic.jpg" if scan else None
        return path if path and path.is_file() else None

    def start(
        self,
        client: ScopeServiceClient,
        loc: EarthLocation,
        pitches=DEFAULT_PITCHES,
        rotations=DEFAULT_ROTATIONS,
        settle_s: float = SETTLE_S,
        when: datetime | None = None,
    ) -> str:
        alt = sun_altitude_deg(loc, when)
        if alt > SUN_MAX_ALT_DEG:
            raise PanoRefused(
                f"the Sun is {alt:.0f} degrees up, the limit is {SUN_MAX_ALT_DEG:.0f}"
            )
        with self._lock:
            if self.state.get("state") == "running":
                raise PanoRefused("a scan is already running")
            scan = uuid.uuid4().hex[:8]
            total = len(pitches) * len(rotations)
            self.state = {"state": "running", "id": scan, "done": 0, "total": total, "failed": 0}
        threading.Thread(
            target=self._run,
            args=(client, scan, pitches, rotations, settle_s),
            name="astrolab-pano",
            daemon=True,
        ).start()
        return scan

    def _run(self, client, scan, pitches, rotations, settle_s) -> None:
        folder = self.root / scan
        folder.mkdir(parents=True, exist_ok=True)
        failed = 0
        try:
            for pitch in pitches:
                for rot in rotations:
                    try:
                        client.call("pointScope", {"pitch": pitch, "rotation": rot}, timeout=90)
                        time.sleep(settle_s)
                        frame = client.call("getFrame", {"camera": "wide"}, timeout=40)
                        (folder / tile_name(pitch, rot)).write_bytes(
                            base64.b64decode(frame["data"])
                        )
                    except Exception as exc:
                        failed += 1
                        log.warning("pano tile %s/%s failed: %s", pitch, rot, exc)
                    self._set(done=self.snapshot()["done"] + 1, failed=failed)
            build_mosaic(folder, pitches, rotations)
            self._set(state="done")
        except Exception as exc:
            self._set(state="failed", error=str(exc))
