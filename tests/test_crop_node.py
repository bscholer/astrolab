"""Tests for the merged crop node.

Covers:
- Auto-trim mode (width == 0 sentinel): detects signal bbox and crops.
- User-box mode (width > 0): applies explicit normalized coords.
- Disabled mode: passes through unchanged.
- Threshold + padding params in auto-trim mode.
- Edge cases: no signal above threshold, full-frame user box.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

import nodes.basic  # noqa: F401  registers nodes on import
from nodes.basic.crop import CropNode, CropParams, _bbox_of_signal, _spatial_shape
from server.models import Ref, RunContext
from server.ports import PortType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fits_with_border(path: Path, *, inner_size: int = 16, border: int = 4, fill: float = 0.0, signal_level: float = 0.02) -> None:
    """Write a FITS where a central inner_size x inner_size region has signal
    and the outer border pixels are filled with `fill` (simulating the
    registration zero-fill)."""
    total = inner_size + 2 * border
    data = np.full((total, total), fill, dtype=np.float32)
    data[border:border + inner_size, border:border + inner_size] = signal_level
    fits.PrimaryHDU(data=data).writeto(path, overwrite=True)


def _make_fits_uniform(path: Path, *, size: int = 24, level: float = 0.02) -> None:
    """Write a FITS with uniform signal — auto-trim should not crop anything."""
    data = np.full((size, size), level, dtype=np.float32)
    fits.PrimaryHDU(data=data).writeto(path, overwrite=True)


def _make_fits_zero(path: Path, *, size: int = 16) -> None:
    """Write an all-zeros FITS — nothing above threshold."""
    data = np.zeros((size, size), dtype=np.float32)
    fits.PrimaryHDU(data=data).writeto(path, overwrite=True)


def _make_ref(path: Path) -> Ref:
    return Ref(node_hash="test", port="image", path=path, type=PortType.IMAGE_FITS)


def _make_ctx(tmp_path: Path) -> RunContext:
    (tmp_path / "tmp").mkdir(exist_ok=True)
    return RunContext(
        tmpdir=tmp_path / "tmp",
        progress=lambda pct, msg: None,
        log=logging.getLogger("test.crop"),
    )


# ---------------------------------------------------------------------------
# Unit tests: _bbox_of_signal
# ---------------------------------------------------------------------------


def test_bbox_of_signal_2d_no_signal() -> None:
    arr = np.zeros((10, 10), dtype=np.float32)
    assert _bbox_of_signal(arr, 0.001) == (0, 0, 0, 0)


def test_bbox_of_signal_2d_all_signal() -> None:
    arr = np.ones((10, 20), dtype=np.float32)
    x, y, w, h = _bbox_of_signal(arr, 0.001)
    assert x == 0 and y == 0 and w == 20 and h == 10


def test_bbox_of_signal_2d_bordered() -> None:
    arr = np.zeros((20, 20), dtype=np.float32)
    arr[4:16, 4:16] = 0.05  # inner 12x12 square with 4px border
    x, y, w, h = _bbox_of_signal(arr, 0.001)
    assert x == 4 and y == 4 and w == 12 and h == 12


def test_bbox_of_signal_3d_chw() -> None:
    """RGB in C,H,W layout."""
    arr = np.zeros((3, 20, 20), dtype=np.float32)
    arr[:, 5:15, 5:15] = 0.05
    x, y, w, h = _bbox_of_signal(arr, 0.001)
    assert x == 5 and y == 5 and w == 10 and h == 10


# ---------------------------------------------------------------------------
# Unit tests: _spatial_shape
# ---------------------------------------------------------------------------


def test_spatial_shape_2d() -> None:
    arr = np.zeros((30, 40), dtype=np.float32)
    assert _spatial_shape(arr) == (30, 40)


def test_spatial_shape_3d_chw() -> None:
    arr = np.zeros((3, 30, 40), dtype=np.float32)
    assert _spatial_shape(arr) == (30, 40)


def test_spatial_shape_3d_hwc() -> None:
    arr = np.zeros((30, 40, 3), dtype=np.float32)
    assert _spatial_shape(arr) == (30, 40)


# ---------------------------------------------------------------------------
# Integration tests: CropNode.run()
# ---------------------------------------------------------------------------


def test_crop_auto_trim_removes_border(tmp_path: Path) -> None:
    """With width == 0 (auto-trim sentinel), the node should detect the signal
    bbox and crop away the zero-fill border."""
    src = tmp_path / "in.fit"
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _make_fits_with_border(src, inner_size=16, border=4, fill=0.0, signal_level=0.02)

    node = CropNode()
    params = CropParams(enabled=True, width=0.0, height=0.0, threshold=0.001, padding=0)
    result = node.run(
        inputs={"image": _make_ref(src)},
        params=params,
        ctx=_make_ctx(tmp_path),
        out_dir=out_dir,
    )

    with fits.open(result["image"].path, memmap=False) as hdul:
        data = hdul[0].data
    assert data.shape == (16, 16), (
        f"Expected (16, 16) after auto-trim; got {data.shape}"
    )


def test_crop_auto_trim_with_padding(tmp_path: Path) -> None:
    """Padding expands the auto-detected bbox (clamped to frame edges)."""
    src = tmp_path / "in.fit"
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    # 24x24 total: 4px border, 16px inner signal
    _make_fits_with_border(src, inner_size=16, border=4, fill=0.0, signal_level=0.02)

    node = CropNode()
    params = CropParams(enabled=True, width=0.0, height=0.0, threshold=0.001, padding=2)
    result = node.run(
        inputs={"image": _make_ref(src)},
        params=params,
        ctx=_make_ctx(tmp_path),
        out_dir=out_dir,
    )

    with fits.open(result["image"].path, memmap=False) as hdul:
        data = hdul[0].data
    # bbox is (4,4,16,16); padding=2 expands to (2,2,20,20)
    assert data.shape == (20, 20), (
        f"Expected (20, 20) after auto-trim + padding; got {data.shape}"
    )


def test_crop_auto_trim_no_signal_passes_through(tmp_path: Path) -> None:
    """When nothing is above threshold, auto-trim should pass the frame through."""
    src = tmp_path / "in.fit"
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _make_fits_zero(src, size=16)

    node = CropNode()
    params = CropParams(enabled=True, width=0.0, height=0.0, threshold=0.001, padding=0)
    result = node.run(
        inputs={"image": _make_ref(src)},
        params=params,
        ctx=_make_ctx(tmp_path),
        out_dir=out_dir,
    )

    with fits.open(result["image"].path, memmap=False) as hdul:
        data = hdul[0].data
    assert data.shape == (16, 16)


def test_crop_auto_trim_uniform_passes_through(tmp_path: Path) -> None:
    """When all pixels are signal (no border to trim), pass-through."""
    src = tmp_path / "in.fit"
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _make_fits_uniform(src, size=20, level=0.05)

    node = CropNode()
    params = CropParams(enabled=True, width=0.0, height=0.0, threshold=0.001, padding=0)
    result = node.run(
        inputs={"image": _make_ref(src)},
        params=params,
        ctx=_make_ctx(tmp_path),
        out_dir=out_dir,
    )

    with fits.open(result["image"].path, memmap=False) as hdul:
        data = hdul[0].data
    assert data.shape == (20, 20)


def test_crop_user_box_explicit_coords(tmp_path: Path) -> None:
    """When width > 0, the node uses explicit normalized coords (user box)."""
    src = tmp_path / "in.fit"
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    # 40x40 uniform signal
    _make_fits_uniform(src, size=40, level=0.05)

    node = CropNode()
    # Crop to the center 50% x 50%: x=0.25, y=0.25, w=0.5, h=0.5
    params = CropParams(enabled=True, x=0.25, y=0.25, width=0.5, height=0.5)
    result = node.run(
        inputs={"image": _make_ref(src)},
        params=params,
        ctx=_make_ctx(tmp_path),
        out_dir=out_dir,
    )

    with fits.open(result["image"].path, memmap=False) as hdul:
        data = hdul[0].data
    assert data.shape == (20, 20), (
        f"Expected (20, 20) for center 50% crop; got {data.shape}"
    )


def test_crop_user_box_full_frame_passes_through(tmp_path: Path) -> None:
    """User box covering the full frame should pass through unchanged."""
    src = tmp_path / "in.fit"
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _make_fits_uniform(src, size=32, level=0.05)

    node = CropNode()
    params = CropParams(enabled=True, x=0.0, y=0.0, width=1.0, height=1.0)
    result = node.run(
        inputs={"image": _make_ref(src)},
        params=params,
        ctx=_make_ctx(tmp_path),
        out_dir=out_dir,
    )

    with fits.open(result["image"].path, memmap=False) as hdul:
        data = hdul[0].data
    assert data.shape == (32, 32)


def test_crop_disabled_passes_through(tmp_path: Path) -> None:
    """Disabled node always passes the image through regardless of params."""
    src = tmp_path / "in.fit"
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _make_fits_with_border(src, inner_size=16, border=8, fill=0.0, signal_level=0.05)

    node = CropNode()
    params = CropParams(enabled=False, width=0.0, height=0.0, threshold=0.001)
    result = node.run(
        inputs={"image": _make_ref(src)},
        params=params,
        ctx=_make_ctx(tmp_path),
        out_dir=out_dir,
    )

    # 16 inner + 2*8 border = 32x32 total; disabled so no trim
    with fits.open(result["image"].path, memmap=False) as hdul:
        data = hdul[0].data
    assert data.shape == (32, 32)


def test_crop_auto_trim_default_params() -> None:
    """Default CropParams should be auto-trim mode (width == 0) and enabled."""
    p = CropParams()
    assert p.enabled is True
    assert p.width == 0.0
    assert p.height == 0.0
    assert p.threshold == pytest.approx(1e-3)
    assert p.padding == 0


def test_crop_node_version_bumped() -> None:
    """Node version should be 2 (bumped from 1 to invalidate caches)."""
    assert CropNode.version == 2
