"""Scan the scope's surroundings: point, grab a wide frame, and tile the frames into a mosaic."""

from __future__ import annotations

import base64
import logging
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from astropy.coordinates import AltAz, EarthLocation, get_sun
from astropy.time import Time
from PIL import Image, ImageDraw

from server.scope_horizon import sky_mask
from server.scope_service import ScopeServiceClient

log = logging.getLogger(__name__)

SUN_MAX_ALT_DEG = -6.0
DEFAULT_PITCHES = (70.0, 100.0, 130.0, 160.0)
DEFAULT_ROTATIONS = tuple(float(r) for r in range(0, 360, 45))
SETTLE_S = 3.0
RETRY_WAIT_S = 5.0
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


def tile_grid(folder: Path) -> tuple[list[float], list[float]]:
    """Pitches and rotations of the tiles in a scan folder, from the file names."""
    pitches, rotations = set(), set()
    for path in folder.glob("p*_r*.jpg"):
        pitch, rot = path.stem[1:].split("_r")
        pitches.add(float(pitch))
        rotations.add(float(rot))
    return sorted(pitches), sorted(rotations)


def build_overlay(folder: Path, excluded: set[str]) -> Path:
    """Mosaic with the sky in magenta. Excluded tiles are dimmed and crossed out."""
    pitches, rotations = tile_grid(folder)
    rows = sorted(pitches, reverse=True)
    sheet = Image.new("RGB", (TILE_W * len(rotations), TILE_H * len(rows)))
    for r, pitch in enumerate(rows):
        for c, rot in enumerate(rotations):
            name = tile_name(pitch, rot)
            if not (folder / name).is_file():
                continue
            tile = Image.open(folder / name).convert("RGB").resize((TILE_W, TILE_H))
            if name in excluded:
                tile = Image.fromarray((np.asarray(tile) * 0.25).astype(np.uint8))
                draw = ImageDraw.Draw(tile)
                draw.line((0, 0, TILE_W, TILE_H), fill=(255, 255, 255), width=2)
                draw.line((0, TILE_H, TILE_W, 0), fill=(255, 255, 255), width=2)
            else:
                a = np.asarray(tile).copy()
                m = sky_mask(tile)
                a[m] = (0.4 * a[m] + 0.6 * np.array([255, 0, 255])).astype(np.uint8)
                tile = Image.fromarray(a)
            sheet.paste(tile, (c * TILE_W, r * TILE_H))
    out = folder / "overlay.jpg"
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

    def folder(self) -> Path | None:
        scan = self.snapshot().get("id")
        path = self.root / scan if scan else None
        return path if path and path.is_dir() else None

    def start(
        self,
        client: ScopeServiceClient,
        loc: EarthLocation,
        pitches=DEFAULT_PITCHES,
        rotations=DEFAULT_ROTATIONS,
        settle_s: float = SETTLE_S,
        when: datetime | None = None,
        tele_covered: bool = False,
    ) -> str:
        alt = sun_altitude_deg(loc, when)
        if alt > SUN_MAX_ALT_DEG and not tele_covered:
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

    def _tile(self, client, folder, pitch, rot, settle_s) -> bool:
        for attempt in (1, 2):
            try:
                client.call("pointScope", {"pitch": pitch, "rotation": rot}, timeout=90)
                time.sleep(settle_s)
                frame = client.call("getFrame", {"camera": "wide"}, timeout=40)
                (folder / tile_name(pitch, rot)).write_bytes(base64.b64decode(frame["data"]))
                return True
            except Exception as exc:
                log.warning("pano tile %s/%s attempt %d failed: %s", pitch, rot, attempt, exc)
                time.sleep(RETRY_WAIT_S)
        return False

    def _run(self, client, scan, pitches, rotations, settle_s) -> None:
        folder = self.root / scan
        folder.mkdir(parents=True, exist_ok=True)
        failed = 0
        try:
            for pitch in pitches:
                for rot in rotations:
                    if not self._tile(client, folder, pitch, rot, settle_s):
                        failed += 1
                    self._set(done=self.snapshot()["done"] + 1, failed=failed)
            build_mosaic(folder, pitches, rotations)
            self._set(state="done")
        except Exception as exc:
            self._set(state="failed", error=str(exc))
