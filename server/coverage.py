"""Where on the sky each session's frames landed, and which sessions overlap.

A session's footprint is the box its frames cover: centred on the position
the scope was sent to (the RA/DEC the scope writes in every frame), and as
large as the frame, from the focal length, pixel size and image size in the
headers. Plate solves of the stacks show the DWARF 3 frames come out north
up with the long side east-west, within a few degrees, so the box is aligned
with RA and Dec.

Masters group sessions by overlap rather than by target name: a mosaic panel
that covers part of the Heart Nebula belongs in the Heart's stack, and a
panel between the Heart and the Soul joins both into one image.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import statistics
from dataclasses import dataclass

# DWARF 3 tele: 3856 x 2180 px of 2 um behind 150 mm.
TELE_W_DEG = 2.95
TELE_H_DEG = 1.67
MIN_OVERLAP = 0.1
"""Two frames must share at least this share of their extent along each axis
to count as overlapping, enough for the combine to plate solve them onto one
grid and match their backgrounds."""
PANEL_MARK = " panel "
"""Separates a mosaic's name from the panel number in a panel's target name.
The scope puts the name in folder and file names, split by underscores, so
panel names stay plain ASCII without '/' or '_'."""


@dataclass(frozen=True)
class Footprint:
    ra_deg: float
    dec_deg: float
    width_deg: float
    """East-west extent."""
    height_deg: float
    """North-south extent."""


def _frame_size_deg(headers: dict) -> tuple[float, float] | None:
    try:
        focal = float(headers["FOCALLEN"])
        px = float(headers.get("XPIXSZ") or headers["YPIXSZ"])
        n1, n2 = int(headers["NAXIS1"]), int(headers["NAXIS2"])
        binning = int(headers.get("XBINNING") or 1)
    except (KeyError, TypeError, ValueError):
        return None
    if focal <= 0 or px <= 0 or n1 <= 0 or n2 <= 0:
        return None
    # Frames saved binned still report the unbinned pixel size.
    scale = math.degrees(px * binning / 1000 / focal)
    if n1 * scale > 20:  # the wide camera or a bad header; not ours to box
        return None
    return n1 * scale, n2 * scale


def footprints(conn: sqlite3.Connection) -> dict[int, Footprint]:
    """Footprint of every session whose frames carry a position."""
    positions: dict[int, list[tuple[float, float]]] = {}
    first_frame: dict[int, int] = {}
    for sid, fid, ra, dec in conn.execute(
        """
        SELECT sf.session_id, f.id, f.ra, f.dec
        FROM session_frames sf JOIN frames f ON f.id = sf.frame_id
        WHERE f.image_type = 'LIGHT'
        """
    ):
        first_frame.setdefault(sid, fid)
        # Frames of the Moon and planets carry 0, 0.
        if ra is None or dec is None or (ra == 0 and dec == 0):
            continue
        positions.setdefault(sid, []).append((float(ra), float(dec)))
    sizes: dict[int, tuple[float, float]] = {}
    ids = [first_frame[s] for s in positions]
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        marks = ",".join("?" * len(chunk))
        for fid, raw in conn.execute(
            f"SELECT id, fits_headers FROM frames WHERE id IN ({marks})", chunk
        ):
            try:
                headers = json.loads(raw) if raw else {}
            except (TypeError, ValueError):
                headers = {}
            size = _frame_size_deg(headers) if isinstance(headers, dict) else None
            if size:
                sizes[fid] = size
    out: dict[int, Footprint] = {}
    for sid, pts in positions.items():
        ra = _median_ra([p[0] for p in pts])
        dec = statistics.median(p[1] for p in pts)
        w, h = sizes.get(first_frame[sid], (TELE_W_DEG, TELE_H_DEG))
        out[sid] = Footprint(ra, dec, w, h)
    return out


def _median_ra(ras: list[float]) -> float:
    """Median RA that survives the 0/360 wrap."""
    ref = ras[0]
    offsets = [((r - ref + 180) % 360) - 180 for r in ras]
    return (ref + statistics.median(offsets)) % 360


def offset_deg(a: Footprint, b: Footprint) -> tuple[float, float]:
    """East-west and north-south distance from a to b, in degrees on the sky."""
    dra = ((b.ra_deg - a.ra_deg + 180) % 360) - 180
    mid = math.radians((a.dec_deg + b.dec_deg) / 2)
    return dra * math.cos(mid), b.dec_deg - a.dec_deg


def overlaps(a: Footprint, b: Footprint, min_share: float = MIN_OVERLAP) -> bool:
    """True when the two boxes share at least `min_share` of their extent on both axes."""
    if abs(a.dec_deg - b.dec_deg) > 30:
        return False
    dx, dy = offset_deg(a, b)
    return abs(dx) <= (1 - min_share) * (a.width_deg + b.width_deg) / 2 and abs(dy) <= (
        1 - min_share
    ) * (a.height_deg + b.height_deg) / 2


def contains(f: Footprint, ra_deg: float, dec_deg: float) -> bool:
    """True when the point falls inside the box."""
    if abs(f.dec_deg - dec_deg) > f.height_deg:
        return False
    dx, dy = offset_deg(f, Footprint(ra_deg, dec_deg, 0.0, 0.0))
    return abs(dx) <= f.width_deg / 2 and abs(dy) <= f.height_deg / 2


@dataclass(frozen=True)
class ShotSession:
    footprint: Footprint
    target: str
    usable_s: float


def kept_sessions(conn: sqlite3.Connection) -> list[ShotSession]:
    """Every kept session with a position, with its kept exposure time."""
    feet = footprints(conn)
    if not feet:
        return []
    out = []
    for sid, target, usable in conn.execute(
        """
        SELECT s.id, t.name, v.usable_s
        FROM sessions s
        JOIN targets t ON t.id = s.target_id
        JOIN session_verdicts v ON v.session_id = s.id AND v.verdict = 'keep'
        """
    ):
        if sid in feet and usable:
            out.append(ShotSession(feet[sid], target, float(usable)))
    return out


def shot_at(
    sessions: list[ShotSession], ra_deg: float, dec_deg: float, skip_target: str | None = None
) -> float:
    """Kept seconds of every session whose frames cover this point, whatever
    target they were shot under: a Heart and Soul mosaic panel counts for the
    Heart if it covers the Heart. `skip_target` leaves out one target's own
    sessions, for callers that count those another way."""
    return sum(
        s.usable_s
        for s in sessions
        if s.target != skip_target and contains(s.footprint, ra_deg, dec_deg)
    )


def base_name(target: str) -> str:
    """The mosaic's name for a panel target, or the name itself."""
    return target.split(PANEL_MARK, 1)[0]


def safe_name(name: str) -> str:
    """A name the scope can put in a folder name: ASCII letters, digits, spaces and + . -"""
    cleaned = re.sub(r"[^A-Za-z0-9 +.\-]+", " ", name)
    return " ".join(cleaned.split())


def panel_name(mosaic: str, index: int, count: int) -> str:
    return f"{safe_name(mosaic)}{PANEL_MARK}{index} of {count}"


@dataclass(frozen=True)
class Panel:
    index: int
    ra_deg: float
    dec_deg: float


def plan_mosaic(
    ra_deg: float,
    dec_deg: float,
    cols: int,
    rows: int,
    overlap: float = 0.2,
    frame_w_deg: float = TELE_W_DEG,
    frame_h_deg: float = TELE_H_DEG,
) -> list[Panel]:
    """Panel centres of a cols x rows grid around a centre, row by row from the
    north-east corner. Neighbours share `overlap` of the frame.

    The frames come out north up, so the grid follows RA and Dec: each row
    keeps one declination and its columns step by the frame width at that
    declination."""
    if cols < 1 or rows < 1:
        raise ValueError("a mosaic needs at least one row and one column")
    if not 0 <= overlap < 0.9:
        raise ValueError("overlap must be in 0..0.9")
    step_x = frame_w_deg * (1 - overlap)
    step_y = frame_h_deg * (1 - overlap)
    top = dec_deg + (rows - 1) / 2 * step_y
    if abs(top) > 89 or abs(dec_deg - (rows - 1) / 2 * step_y) > 89:
        raise ValueError("the mosaic reaches the pole")
    panels = []
    for r in range(rows):
        dec = top - r * step_y
        per_deg = 1 / max(math.cos(math.radians(dec)), 1e-6)
        for c in range(cols):
            # Column 0 is the eastern edge: east is higher RA.
            ra = (ra_deg + ((cols - 1) / 2 - c) * step_x * per_deg) % 360
            panels.append(Panel(r * cols + c + 1, ra, dec))
    return panels
