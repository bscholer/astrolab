"""Learn which parts of the sky the scope can see from how its captures turn out.

The scope tags a frame failed when it cannot find stars to register. A good frame proves
that patch of sky was open. A patch that fails night after night and almost never gives a
good frame is behind something: a tree, the house. Clouds come and go, so asking for
failures on several nights keeps a few cloudy nights from marking open sky as blocked.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

CELL_DEG = 5.0
CLEAR_MIN_GOOD = 3
BLOCKED_MIN_FAILED = 8
BLOCKED_MIN_NIGHTS = 3
BLOCKED_MAX_GOOD_SHARE = 0.1
NIGHTS_KEPT = 10


def cell_key(alt: float, az: float) -> str:
    """The patch of sky a pointing falls in, by its lower corner, like "35/120"."""
    a = math.floor(alt / CELL_DEG) * CELL_DEG
    z = (math.floor((az % 360) / CELL_DEG) * CELL_DEG) % 360
    return f"{a:g}/{z:g}"


def night_of(at: datetime) -> str:
    """The date a night started on, in UTC shifted back 12 hours so one night is one date."""
    return (at.astimezone(UTC) - timedelta(hours=12)).date().isoformat()


def verdict(cell: dict) -> str:
    """clear, blocked, or unknown for one patch's counts."""
    good, failed = cell.get("good", 0), cell.get("failed", 0)
    if good >= CLEAR_MIN_GOOD and good > BLOCKED_MAX_GOOD_SHARE * (good + failed):
        return "clear"
    nights = len(cell.get("nights", []))
    if (
        failed >= BLOCKED_MIN_FAILED
        and nights >= BLOCKED_MIN_NIGHTS
        and good <= BLOCKED_MAX_GOOD_SHARE * (good + failed)
    ):
        return "blocked"
    return "unknown"


class SkyMap:
    """Per-patch counts of good and failed frames, kept in a store such as a setting."""

    def __init__(self, load: Callable[[], dict], save: Callable[[dict], None]) -> None:
        self._load = load
        self._save = save
        self._lock = threading.Lock()
        self._cells: dict[str, dict] | None = None

    def cells(self) -> dict[str, dict]:
        with self._lock:
            if self._cells is None:
                self._cells = dict(self._load() or {})
            return {k: dict(v) for k, v in self._cells.items()}

    def record(self, alt: float, az: float, good: int, failed: int, at: datetime) -> None:
        """Add frames shot at this altitude and azimuth. Below the horizon is ignored."""
        if alt < 0 or (good <= 0 and failed <= 0):
            return
        self.cells()
        with self._lock:
            assert self._cells is not None
            cell = self._cells.setdefault(cell_key(alt, az), {"good": 0, "failed": 0, "nights": []})
            cell["good"] += max(good, 0)
            cell["failed"] += max(failed, 0)
            if failed > 0:
                night = night_of(at)
                if night not in cell["nights"]:
                    cell["nights"] = (cell["nights"] + [night])[-NIGHTS_KEPT:]
            self._save(dict(self._cells))

    def blocked(self, alt: float, az: float) -> bool:
        cell = self.cells().get(cell_key(alt, az))
        return cell is not None and verdict(cell) == "blocked"

    def clear(self) -> None:
        with self._lock:
            self._cells = {}
            self._save({})

    def summary(self) -> dict:
        cells = self.cells()
        counts = {"clear": 0, "blocked": 0, "unknown": 0}
        for c in cells.values():
            counts[verdict(c)] += 1
        return {"cell_deg": CELL_DEG, "cells": counts}


def skymap_svg(cells: dict[str, dict], floor_deg: float) -> str:
    """The learned patches on a sky map: zenith at the center, the true horizon at the rim.

    North is up and east is right, like a map of the yard. Blue patches gave good frames,
    orange ones keep failing, gray ones have too little to go on yet.
    """
    size, pad = 360, 26
    c, rim = size / 2, size / 2 - pad

    def xy(alt: float, az: float) -> tuple[float, float]:
        r = rim * (90 - max(min(alt, 90.0), 0.0)) / 90
        return c + r * math.sin(math.radians(az)), c - r * math.cos(math.radians(az))

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        'font-family="system-ui, sans-serif" font-size="11">',
        "<title>What the scope has seen of the sky</title>",
    ]
    ring = (
        '<circle cx="{c}" cy="{c}" r="{r:.1f}" fill="none" stroke="#6f8099" stroke-width="{w}"{d}/>'
    )
    for alt in (0, 30, 60):
        parts.append(ring.format(c=c, r=rim * (90 - alt) / 90, w=1 if alt == 0 else 0.5, d=""))
        if alt:
            x, y = xy(alt, 0)
            parts.append(f'<text x="{x + 3:.1f}" y="{y - 3:.1f}" fill="#6f8099">{alt}°</text>')
    if floor_deg not in (0, 30, 60):
        dash = ' stroke-dasharray="3 3"'
        parts.append(ring.format(c=c, r=rim * (90 - floor_deg) / 90, w=1, d=dash))
    for label, az in (("N", 0), ("E", 90), ("S", 180), ("W", 270)):
        x, y = xy(-8, az)
        parts.append(
            f'<text x="{x:.1f}" y="{y + 4:.1f}" fill="#e9f1ff" text-anchor="middle">{label}</text>'
        )
    colors = {"clear": "#86b6ef", "blocked": "#eb6834", "unknown": "#6f8099"}
    for key, cell in sorted(cells.items()):
        alt0, az0 = (float(v) for v in key.split("/"))
        alt, az = alt0 + CELL_DEG / 2, az0 + CELL_DEG / 2
        x, y = xy(alt, az)
        state = verdict(cell)
        nights = len(cell.get("nights", []))
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{colors[state]}">'
            f"<title>altitude {alt0:g}–{alt0 + CELL_DEG:g}°, azimuth {az0:g}–{az0 + CELL_DEG:g}°: "
            f"{state}, {cell.get('good', 0)} good, {cell.get('failed', 0)} failed "
            f"on {nights} night{'s' if nights != 1 else ''}</title></circle>"
        )
    parts.append("</svg>")
    return "\n".join(parts)
