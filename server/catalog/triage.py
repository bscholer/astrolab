"""Second look at frames the scope rejected (failed_*.fits).

The Dwarf 3 saves a sub it could not register stars in as failed_*.fits. Most
are cloud or an obstruction with no stars at all, but some are as good as the
frames it kept (a spike over 1,000 of them: 67% had no stars, 15% matched the
session's good frames). This module counts stars in each failed frame and
compares it with a sample of the same session's good frames:

    rescued = stars >= RESCUE_STAR_RATIO * good median stars
              and star size <= RESCUE_FWHM_RATIO * good median size
              and stars >= MIN_STARS

Rescued frames are stacked and backed up like good ones (job_builder,
nas_backup); the rest stay out. Results live in frames.stars, frames.star_fwhm
and frames.rescued (NULL = not looked at yet, 0 = junk, 1 = rescued).

The detector is deliberately simple numpy (no scipy in the image): bin the CFA
2x2, subtract a tiled background, keep local maxima above 20 sigma with at
least 3 pixels over half their peak. Raw frames are full of hot pixels, which
a lower threshold counts by the thousand; at 20 sigma the counts track Siril's
(about 145 against 165 on an M 33 frame). Both sides of the comparison use this
detector, so only ratios matter.
"""

from __future__ import annotations

import logging
import sqlite3
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from astropy.io import fits

log = logging.getLogger(__name__)

RESCUE_STAR_RATIO = 0.5
RESCUE_FWHM_RATIO = 1.25
MIN_STARS = 10
REFERENCE_FRAMES = 12
"""Good frames measured per session to set the bar."""
MIN_REFERENCE = 3
"""Sessions with fewer good frames than this rescue nothing: there is no bar."""
SETTLE_S = 600
"""Leave frames younger than this alone; the mirror may still be copying them."""

_TILE = 64
_SIGMA = 20.0
_EDGE = 8


@dataclass(frozen=True)
class StarStats:
    stars: int
    fwhm: float | None
    """Median star diameter at half maximum, in binned (2x2) pixels."""


def _luminance(data: np.ndarray) -> np.ndarray:
    """Sum each 2x2 Bayer cell: one value per cell, colour-blind but star-shaped."""
    h, w = (data.shape[0] // 2) * 2, (data.shape[1] // 2) * 2
    d = data[:h, :w].astype(np.float32)
    return d[0::2, 0::2] + d[0::2, 1::2] + d[1::2, 0::2] + d[1::2, 1::2]


def _background(img: np.ndarray) -> np.ndarray:
    h, w = (img.shape[0] // _TILE) * _TILE, (img.shape[1] // _TILE) * _TILE
    tiles = img[:h, :w].reshape(h // _TILE, _TILE, w // _TILE, _TILE)
    med = np.median(tiles, axis=(1, 3))
    bg = np.repeat(np.repeat(med, _TILE, axis=0), _TILE, axis=1)
    out = np.empty_like(img)
    out[:h, :w] = bg
    # Pad the ragged right/bottom strip with the nearest tile row/column.
    out[h:, :w] = bg[-1:, :]
    out[:, w:] = out[:, w - 1 : w]
    return out


def _local_max(img: np.ndarray, r: int) -> np.ndarray:
    """img == max over a (2r+1)^2 window (edges excluded by the caller)."""
    m = img.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dy or dx:
                np.maximum(m, np.roll(np.roll(img, dy, axis=0), dx, axis=1), out=m)
    return img >= m


def measure(data: np.ndarray) -> StarStats:
    """Count stars in a raw CFA frame and estimate their size."""
    img = _luminance(data)
    resid = img - _background(img)
    mad = float(np.median(np.abs(resid - np.median(resid))))
    sigma = 1.4826 * mad if mad > 0 else float(resid.std()) or 1.0
    peaks = (resid > _SIGMA * sigma) & _local_max(resid, 2)
    peaks[:_EDGE, :] = peaks[-_EDGE:, :] = False
    peaks[:, :_EDGE] = peaks[:, -_EDGE:] = False
    ys, xs = np.nonzero(peaks)
    sizes: list[float] = []
    for y, x in zip(ys, xs, strict=True):
        cut = resid[y - 3 : y + 4, x - 3 : x + 4]
        area = int((cut >= 0.5 * resid[y, x]).sum())
        if area < 3:  # hot pixel or cosmic ray: nothing around the peak
            continue
        sizes.append(2.0 * float(np.sqrt(area / np.pi)))
    return StarStats(len(sizes), float(np.median(sizes)) if sizes else None)


def measure_file(path: Path) -> StarStats:
    with fits.open(path, memmap=False) as hdul:
        return measure(np.asarray(hdul[0].data))


def is_rescued(frame: StarStats, ref_stars: float, ref_fwhm: float | None) -> bool:
    if frame.stars < MIN_STARS or frame.stars < RESCUE_STAR_RATIO * ref_stars:
        return False
    if ref_fwhm is None or frame.fwhm is None:
        return True
    return frame.fwhm <= RESCUE_FWHM_RATIO * ref_fwhm


def _spread(rows: list, n: int) -> list:
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    return [rows[int(i * step)] for i in range(n)]


def _session_frames(conn: sqlite3.Connection, session_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT f.id, f.path, f.quality, f.mtime, f.stars, f.star_fwhm, f.rescued
        FROM frames f JOIN session_frames sf ON sf.frame_id = f.id
        WHERE sf.session_id = ? AND f.image_type = 'LIGHT'
        ORDER BY f.date_obs, f.path
        """,
        (session_id,),
    ).fetchall()


def _store(conn: sqlite3.Connection, frame_id: int, s: StarStats, rescued: int | None) -> None:
    # Commit per frame: measuring takes a while, and an open write transaction
    # would block the worker and the scanner for the whole session.
    conn.execute(
        "UPDATE frames SET stars = ?, star_fwhm = ?, rescued = ? WHERE id = ?",
        (s.stars, s.fwhm, rescued, frame_id),
    )
    conn.commit()


def triage_session(
    conn: sqlite3.Connection, session_id: int, *, now: float | None = None,
    deadline: float | None = None,
) -> dict[str, int]:
    """Measure and judge the session's untriaged failed frames. Returns counts."""
    now = time.time() if now is None else now
    rows = _session_frames(conn, session_id)
    pending = [
        r for r in rows
        if r["quality"] == "failed" and r["rescued"] is None and now - (r["mtime"] or 0) >= SETTLE_S
    ]
    out = {"rescued": 0, "junk": 0, "unreadable": 0}
    if not pending:
        return out

    good = [r for r in rows if r["quality"] != "failed"]
    ref: list[StarStats] = []
    for r in _spread(good, REFERENCE_FRAMES):
        if r["stars"] is not None:
            ref.append(StarStats(r["stars"], r["star_fwhm"]))
            continue
        try:
            s = measure_file(Path(r["path"]))
        except Exception as exc:  # a missing or truncated file must not stop the rest
            log.warning("triage: cannot read %s: %s", r["path"], exc)
            continue
        _store(conn, r["id"], s, None)
        ref.append(s)
    ref = [s for s in ref if s.stars > 0]
    if len(ref) >= MIN_REFERENCE:
        ref_stars = statistics.median(s.stars for s in ref)
        sizes = [s.fwhm for s in ref if s.fwhm is not None]
        ref_fwhm = statistics.median(sizes) if sizes else None
    else:
        ref_stars, ref_fwhm = float("inf"), None  # no bar to clear: rescue nothing

    for r in pending:
        if deadline is not None and time.time() > deadline:
            break
        try:
            s = measure_file(Path(r["path"]))
        except Exception as exc:
            # Truncated or missing (it has settled, so not mid-copy): junk, and
            # not retried every pass.
            log.warning("triage: cannot read %s: %s", r["path"], exc)
            conn.execute("UPDATE frames SET rescued = 0 WHERE id = ?", (r["id"],))
            conn.commit()
            out["unreadable"] += 1
            continue
        keep = is_rescued(s, ref_stars, ref_fwhm)
        _store(conn, r["id"], s, 1 if keep else 0)
        out["rescued" if keep else "junk"] += 1
    return out


def triage_pending(
    conn: sqlite3.Connection, *, budget_s: float | None = None, now: float | None = None,
) -> dict[str, int]:
    """Triage every session with untriaged failed frames, oldest first, within budget_s."""
    deadline = None if budget_s is None else time.time() + budget_s
    sessions = [
        r[0] for r in conn.execute(
            """
            SELECT DISTINCT sf.session_id FROM frames f
            JOIN session_frames sf ON sf.frame_id = f.id
            WHERE f.image_type = 'LIGHT' AND f.quality = 'failed' AND f.rescued IS NULL
            ORDER BY sf.session_id
            """
        ).fetchall()
    ]
    total = {"sessions": 0, "rescued": 0, "junk": 0, "unreadable": 0}
    for sid in sessions:
        if deadline is not None and time.time() > deadline:
            break
        counts = triage_session(conn, sid, now=now, deadline=deadline)
        total["sessions"] += 1
        for k, v in counts.items():
            total[k] += v
        if counts["rescued"] or counts["junk"]:
            log.info("triage: session %s %s", sid, counts)
    return total


def rescued_paths(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT path FROM frames WHERE rescued = 1")}


if __name__ == "__main__":  # python -m server.catalog.triage: backfill everything now
    import json

    from server.catalog.db import connect, default_db_path

    logging.basicConfig(level=logging.INFO)
    c = connect(default_db_path())
    print(json.dumps(triage_pending(c)))
