"""Triage of frames the scope rejected: star counting and rescue decisions."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
from astropy.io import fits

from server.catalog.db import open_db
from server.catalog.triage import (
    StarStats,
    is_rescued,
    measure,
    rescued_paths,
    triage_pending,
)


def _frame(n_stars: int, *, seed: int = 0, hot: int = 300, sigma: float = 1.8) -> np.ndarray:
    """A raw-like CFA frame: pedestal, read noise, hot pixels, Gaussian stars
    (sigma 1.8 px is a FWHM of about 4.2 px, what Siril measures on Dwarf frames)."""
    rng = np.random.default_rng(seed)
    h, w = 540, 960
    img = 200 + rng.normal(0, 6, (h, w))
    yy, xx = np.mgrid[0:h, 0:w]
    for _ in range(n_stars):
        y, x = rng.uniform(30, h - 30), rng.uniform(30, w - 30)
        img += rng.uniform(800, 3000) * np.exp(-((yy - y) ** 2 + (xx - x) ** 2) / (2 * sigma**2))
    hy, hx = rng.integers(0, h, hot), rng.integers(0, w, hot)
    img[hy, hx] = 4000  # hot pixels: one pixel each, never stars
    return np.clip(img, 0, 65535).astype(np.uint16)


def test_measure_counts_stars_not_hot_pixels() -> None:
    assert measure(_frame(0)).stars == 0
    s = measure(_frame(60, seed=1))
    assert 50 <= s.stars <= 75  # best effort: bright stars sometimes split in two
    assert s.fwhm is not None and 1.0 < s.fwhm < 4.0


def test_measure_sees_bloated_stars_as_bigger() -> None:
    sharp = measure(_frame(40, seed=2, sigma=1.2)).fwhm
    soft = measure(_frame(40, seed=2, sigma=3.0)).fwhm
    assert sharp is not None and soft is not None and soft > 1.5 * sharp


def test_rescue_rule() -> None:
    assert is_rescued(StarStats(60, 2.0), ref_stars=100, ref_fwhm=2.0)
    assert not is_rescued(StarStats(40, 2.0), ref_stars=100, ref_fwhm=2.0)  # too few
    assert not is_rescued(StarStats(90, 3.0), ref_stars=100, ref_fwhm=2.0)  # bloated
    assert not is_rescued(StarStats(8, 2.0), ref_stars=10, ref_fwhm=2.0)  # under the floor
    assert not is_rescued(StarStats(500, 2.0), ref_stars=float("inf"), ref_fwhm=None)


def _write(path: Path, data: np.ndarray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.PrimaryHDU(data).writeto(path)
    os.utime(path, (1, 1))
    return path


def _seed(db, folder: Path, frames: list[tuple[str, str, np.ndarray]]) -> None:
    with db:
        db.execute("INSERT INTO targets (id, name) VALUES (1, 'M 33')")
        db.execute(
            "INSERT INTO sessions (id, scope_id, target_id, instrument, exptime, gain,"
            " binning, frame_count, failed_count) VALUES (1, 'dwarf3', 1, 'DWARF 3', 30, 60, 1, ?, 0)",
            (len(frames),),
        )
        for i, (name, quality, data) in enumerate(frames):
            p = _write(folder / name, data)
            cur = db.execute(
                "INSERT INTO frames (path, image_type, quality, mtime, date_obs)"
                " VALUES (?, 'LIGHT', ?, 1, ?)",
                (str(p), quality, f"2026-11-13T20:{i:02d}:00"),
            )
            db.execute(
                "INSERT INTO session_frames (session_id, frame_id) VALUES (1, ?)",
                (cur.lastrowid,),
            )


def test_triage_rescues_starry_failed_frames_only(tmp_path: Path) -> None:
    folder = tmp_path / "captures" / "M33"
    good = [(f"g{i}.fits", "ok", _frame(60, seed=10 + i)) for i in range(4)]
    failed = [
        ("failed_clear.fits", "failed", _frame(55, seed=20)),
        ("failed_cloud.fits", "failed", _frame(3, seed=21)),
        ("failed_soft.fits", "failed", _frame(60, seed=22, sigma=4.0)),
    ]
    with open_db(tmp_path / "c.sqlite") as db:
        _seed(db, folder, good + failed)

        assert triage_pending(db) == {"sessions": 1, "rescued": 1, "junk": 2, "unreadable": 0}
        assert rescued_paths(db) == {str(folder / "failed_clear.fits")}
        rows = dict(db.execute("SELECT path, rescued FROM frames").fetchall())
        assert rows[str(folder / "g0.fits")] is None  # good frames are measured, not judged
        # Done once: a second pass has nothing left to look at.
        assert triage_pending(db)["sessions"] == 0


def test_triage_rescues_nothing_without_good_frames(tmp_path: Path) -> None:
    folder = tmp_path / "captures" / "M33"
    failed = [(f"failed_{i}.fits", "failed", _frame(60, seed=30 + i)) for i in range(4)]
    with open_db(tmp_path / "c.sqlite") as db:
        _seed(db, folder, failed)
        assert triage_pending(db)["rescued"] == 0


def test_triage_leaves_fresh_frames_for_later(tmp_path: Path) -> None:
    folder = tmp_path / "captures" / "M33"
    good = [(f"g{i}.fits", "ok", _frame(60, seed=40 + i)) for i in range(3)]
    with open_db(tmp_path / "c.sqlite") as db:
        _seed(db, folder, [*good, ("failed_new.fits", "failed", _frame(60, seed=50))])
        db.execute("UPDATE frames SET mtime = ? WHERE path LIKE '%failed_new%'", (2e9,))
        db.commit()
        assert triage_pending(db, now=2e9 + 5)["rescued"] == 0
        assert triage_pending(db, now=2e9 + 3600)["rescued"] == 1


def test_triage_marks_unreadable_frames_once(tmp_path: Path) -> None:
    folder = tmp_path / "captures" / "M33"
    good = [(f"g{i}.fits", "ok", _frame(60, seed=60 + i)) for i in range(3)]
    with open_db(tmp_path / "c.sqlite") as db:
        _seed(db, folder, [*good, ("failed_cut.fits", "failed", _frame(60, seed=70))])
        cut = folder / "failed_cut.fits"
        cut.write_bytes(cut.read_bytes()[:20000])
        os.utime(cut, (1, 1))
        assert triage_pending(db)["unreadable"] == 1
        assert triage_pending(db)["sessions"] == 0
