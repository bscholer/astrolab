"""ensure_disk_headroom + per-node estimator tests.

Covers the per-node disk preflight: estimator returns expected bytes for
a sample sequence, ensure_disk_headroom triggers sweep when needed,
raises InsufficientStorageError when impossible.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from astropy.io import fits

from nodes._storage_estimate import (
    estimate_sequence_output_bytes,
    measure_actual_bytes,
)
from server.cache import ContentCache
from server.catalog.db import connect as open_catalog_db
from server.storage import (
    InsufficientStorageError,
    ensure_disk_headroom,
)


def _make_fits(path: Path, *, rx: int = 100, ry: int = 100, bitpix: int = 16) -> None:
    """Write a FITS file of given dims/bitpix so estimators have something
    to inspect. Pixel content is zero — we only care about file size."""
    dtype = {8: np.uint8, 16: np.uint16, 32: np.float32, -32: np.float32, -64: np.float64}[bitpix]
    data = np.zeros((ry, rx), dtype=dtype)
    hdu = fits.PrimaryHDU(data)
    hdu.header["BITPIX"] = bitpix
    hdu.writeto(path, overwrite=True)


def test_estimate_scales_with_frame_count(tmp_path: Path) -> None:
    """Twice as many input frames -> twice the estimated output."""
    a = tmp_path / "one"
    a.mkdir()
    _make_fits(a / "f1.fit", rx=100, ry=100, bitpix=16)

    b = tmp_path / "two"
    b.mkdir()
    _make_fits(b / "f1.fit", rx=100, ry=100, bitpix=16)
    _make_fits(b / "f2.fit", rx=100, ry=100, bitpix=16)

    est_a = estimate_sequence_output_bytes(a)
    est_b = estimate_sequence_output_bytes(b)
    assert est_a is not None and est_b is not None
    # Per-file FITS overhead means it's not exactly 2x, but within tight bounds.
    ratio = est_b / est_a
    assert 1.9 < ratio < 2.1, f"expected ~2x, got {ratio:.2f}"


def test_estimate_doubles_for_uint16_to_float32(tmp_path: Path) -> None:
    """Default output is float32 (bps=4); uint16 input has bps=2, so output
    estimate should be ~2x the input bytes."""
    seq = tmp_path / "seq"
    seq.mkdir()
    _make_fits(seq / "f1.fit", rx=200, ry=200, bitpix=16)
    input_total = sum(f.stat().st_size for f in seq.iterdir())
    est = estimate_sequence_output_bytes(seq)
    assert est is not None
    ratio = est / input_total
    # Pixel data dominates the file; ratio approaches output_bps/input_bps = 2.
    assert 1.7 < ratio < 2.2, f"expected ~2.0, got {ratio:.2f}"


def test_estimate_scales_with_spatial_scale(tmp_path: Path) -> None:
    """Drizzle scale=2 quadruples the per-frame output area."""
    seq = tmp_path / "seq"
    seq.mkdir()
    _make_fits(seq / "f1.fit", rx=200, ry=200, bitpix=32)
    est_1x = estimate_sequence_output_bytes(seq, scale=1.0)
    est_2x = estimate_sequence_output_bytes(seq, scale=2.0)
    assert est_1x is not None and est_2x is not None
    ratio = est_2x / est_1x
    assert 3.9 < ratio < 4.1, f"scale=2 -> 4x, got {ratio:.2f}"


def test_estimate_returns_none_for_empty_dir(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert estimate_sequence_output_bytes(empty) is None


def test_estimate_returns_none_for_missing_dir(tmp_path: Path) -> None:
    assert estimate_sequence_output_bytes(tmp_path / "nope") is None


def test_measure_actual_walks_recursively(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x").write_bytes(b"x" * 100)
    (tmp_path / "b.txt").write_bytes(b"y" * 50)
    assert measure_actual_bytes(tmp_path) == 150


def _seed_cache_with(cache: ContentCache, hashes_and_sizes: list[tuple[str, int]]) -> None:
    for h, size in hashes_and_sizes:
        d = cache.reserve(h)
        (d / "data.bin").write_bytes(b"x" * size)
        (d / "_done").touch()
        (d / "_outputs.json").write_text("{}")


def test_ensure_headroom_noop_when_disk_has_room(tmp_path: Path) -> None:
    cache = ContentCache(root=tmp_path / "cache")
    db_path = tmp_path / "catalog.sqlite"
    open_catalog_db(db_path).close()
    _seed_cache_with(cache, [("h1", 1000)])

    # Patch _disk_usage to report enormous free space.
    fake_disk = type("D", (), {"total_bytes": 10**12, "used_bytes": 0, "free_bytes": 10**12})
    with patch("server.storage._disk_usage", return_value=fake_disk):
        result = ensure_disk_headroom(
            cache, need_bytes=1_000_000_000,
            cache_max_bytes=10**12, db_path=db_path,
        )
    assert result is None  # no sweep happened
    assert cache.entry_dir("h1").exists()


def test_ensure_headroom_sweeps_when_disk_tight(tmp_path: Path) -> None:
    cache = ContentCache(root=tmp_path / "cache")
    db_path = tmp_path / "catalog.sqlite"
    open_catalog_db(db_path).close()
    _seed_cache_with(cache, [("h1", 10_000), ("h2", 10_000)])

    # Pretend disk has only 5 KB free; we need 15 KB. Sweep must reclaim
    # ~10 KB so the next-pass disk_free reaches 15 KB. We can't actually
    # change disk_free between calls in this unit, so just assert eviction
    # ran and the InsufficientStorageError is NOT raised when post-sweep
    # disk reports enough free.
    free_seq = iter([
        type("D", (), {"total_bytes": 10**6, "used_bytes": 0, "free_bytes": 5_000}),
        type("D", (), {"total_bytes": 10**6, "used_bytes": 0, "free_bytes": 20_000}),
    ])
    with patch("server.storage._disk_usage", side_effect=lambda _: next(free_seq)):
        result = ensure_disk_headroom(
            cache, need_bytes=15_000,
            cache_max_bytes=10**6, db_path=db_path,
        )
    assert result is not None
    assert result.evicted_count >= 1


def test_ensure_headroom_raises_when_impossible(tmp_path: Path) -> None:
    cache = ContentCache(root=tmp_path / "cache")
    db_path = tmp_path / "catalog.sqlite"
    open_catalog_db(db_path).close()
    _seed_cache_with(cache, [("h1", 10_000)])

    # Disk reports 5 KB before AND after the sweep; sweep can't help.
    fake_disk = type("D", (), {"total_bytes": 10**6, "used_bytes": 0, "free_bytes": 5_000})
    with patch("server.storage._disk_usage", return_value=fake_disk), \
         pytest.raises(InsufficientStorageError) as excinfo:
        ensure_disk_headroom(
            cache, need_bytes=100_000,
            cache_max_bytes=10**6, db_path=db_path,
        )
    err = excinfo.value
    assert err.need_bytes == 100_000
    assert err.free_bytes == 5_000
    assert "not enough disk space" in str(err)


def test_ensure_headroom_respects_budget_ceiling(tmp_path: Path) -> None:
    """Even with infinite disk, hitting cache_max_bytes triggers a sweep.

    User sets a tight cache cap of 15 KB. Cache already has 12 KB. A node
    that needs 10 KB would push the cache to 22 KB, breaching the cap;
    the sweep must reclaim ~7 KB first.
    """
    cache = ContentCache(root=tmp_path / "cache")
    db_path = tmp_path / "catalog.sqlite"
    open_catalog_db(db_path).close()
    _seed_cache_with(cache, [("h1", 6_000), ("h2", 6_000)])

    # Plenty of physical disk; the budget is the binding constraint.
    fake_disk = type("D", (), {"total_bytes": 10**12, "used_bytes": 0, "free_bytes": 10**12})
    with patch("server.storage._disk_usage", return_value=fake_disk):
        result = ensure_disk_headroom(
            cache, need_bytes=10_000,
            cache_max_bytes=15_000, db_path=db_path,
        )
    assert result is not None
    # Need cache_used <= cache_max_bytes - need = 5_000, so at least one
    # of the two 6 KB entries had to go.
    assert result.evicted_count >= 1
