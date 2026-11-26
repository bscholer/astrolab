"""Regression tests for the duo-band / HOO bugs of Nov 2026, one per bug.

Each builds a synthetic image that reproduces what went wrong on real data
(California, Rosette) and runs the real node code on it. Siril-dependent
steps (the split, registration) are covered by sequence-file tests in
test_narrowband_pack.py, since Siril isn't available in CI.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

import nodes.basic  # noqa: F401  registers node kinds
from nodes.basic.channel_map import hoo
from nodes.basic.combine_stacks import CombineStacksParams
from nodes.basic.crop import CropNode, CropParams
from nodes.basic.graxpert import restore_nodata
from nodes.basic.stretch import auto_mtf
from server.models import Ref, RunContext
from server.ports import PortType


def _ctx(tmp_path: Path) -> RunContext:
    return RunContext(tmpdir=tmp_path, progress=lambda f, m: None, log=logging.getLogger("t"))


def _nebula_scene(h: int = 600, w: int = 900, seed: int = 3) -> np.ndarray:
    """Rosette-like: an H-alpha ring, OIII in the core, stars bright in OIII."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    r = np.hypot(yy - h / 2, xx - w / 2)
    core = np.exp(-(r**2) / (2 * 70.0**2))
    ring = np.exp(-((r - 190) ** 2) / (2 * 45.0**2))
    stars = np.zeros((h, w))
    for y, x in rng.uniform(10, [h - 10, w - 10], (400, 2)):
        stars += np.exp(-((yy - y) ** 2 + (xx - x) ** 2) / (2 * 1.6**2))
    sky = 0.07
    ha = sky + 0.02 * ring + 0.01 * core + 0.05 * stars
    oiii = sky + 0.002 * ring + 0.012 * core + 0.2 * stars
    noise = rng.normal(0, 1e-4, (3, h, w))
    return (np.stack([ha, oiii, oiii]) + noise).astype(np.float32)


def _blue_over_red(img: np.ndarray, ys: slice, xs: slice) -> float:
    return float(np.median(img[2][ys, xs]) / np.median(img[0][ys, xs]))


# --- Combine Nights trimmed the Rosette's top off (coverage trim) ----------------


def test_combine_keeps_every_nights_area_by_default() -> None:
    # The user frames the picture; mosaics and single frames combine whole.
    assert CombineStacksParams().min_coverage == 0.0


# --- The early crop shrank into partly covered edges ----------------------------


def test_auto_crop_keeps_the_union_of_offset_nights(tmp_path: Path) -> None:
    """Two nights framed apart (or a mosaic): the corners are empty, the edges
    partly covered. Auto-trim may only drop rows/columns with no data at all."""
    h, w = 400, 600
    img = np.zeros((3, h + 40, w + 80), np.float32)
    img[:, 20 : 20 + h - 100, 40 : 40 + w - 150] = 0.05  # night 1
    img[:, 120 : 20 + h, 190 : 40 + w] = 0.05  # night 2, offset
    src = tmp_path / "in.fit"
    fits.PrimaryHDU(img).writeto(src)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    ref = Ref(node_hash="x", port="image", path=src, type=PortType.IMAGE_FITS)
    res = CropNode().run({"image": ref}, CropParams(), _ctx(tmp_path), out_dir)
    out = fits.getdata(res["image"].path)
    assert (out[0] > 0).sum() == (img[0] > 0).sum()  # every data pixel survives
    assert out.shape[1:] == (h, w)  # only the empty margin went


# --- GraXpert filled empty canvas with made-up sky ------------------------------


def test_graxpert_output_keeps_empty_canvas_empty(tmp_path: Path) -> None:
    src_img = np.full((3, 50, 80), 0.07, np.float32)
    src_img[:, :12, :] = 0  # registration border / mosaic gap
    filled = src_img.copy()
    filled[:, :12, :] = 0.006  # what GraXpert wrote there on the Rosette
    src, out = tmp_path / "src.fit", tmp_path / "out.fit"
    fits.PrimaryHDU(src_img).writeto(src)
    fits.PrimaryHDU(filled).writeto(out)
    assert restore_nodata(src, out) == 12 * 80
    data = fits.getdata(out)
    assert not data[:, :12, :].any()
    assert np.allclose(data[:, 12:, :], 0.07)


# --- Empty canvas pulled the stretch's sky level down ---------------------------


def test_stretch_ignores_empty_canvas() -> None:
    scene = _nebula_scene()
    padded = np.zeros((3, scene.shape[1] * 2, scene.shape[2]), np.float32)
    padded[:, : scene.shape[1]] = scene  # half the canvas empty
    a = auto_mtf(scene)[:2]
    b = auto_mtf(padded)[:2]
    assert b == pytest.approx(a, rel=0.05)


# --- HOO colour depended on the framing (tight crop -> red/blue blotches) -------


def test_hoo_colour_does_not_depend_on_the_crop() -> None:
    scene = _nebula_scene()
    wide = hoo(scene, 1.4)
    tight_in = scene[:, 150:450, 250:650]  # cropped in around the nebula
    tight = hoo(tight_in, 1.4)
    core_w = (slice(290, 310), slice(440, 460))
    core_t = (slice(140, 160), slice(190, 210))
    ring_w = (slice(105, 125), slice(440, 460))  # top of the ring, inside both crops
    ring_t = (slice(-45 + 150, -25 + 150), slice(190, 210))
    # The core is bluer than the ring in both, and the ratios stay close.
    assert _blue_over_red(wide, *core_w) > _blue_over_red(wide, *ring_w)
    assert _blue_over_red(tight, *core_t) > _blue_over_red(tight, *ring_t)
    assert _blue_over_red(tight, *core_t) == pytest.approx(_blue_over_red(wide, *core_w), rel=0.35)


def test_hoo_colour_ignores_empty_canvas() -> None:
    scene = _nebula_scene()
    padded = np.zeros((3, scene.shape[1] + 300, scene.shape[2]), np.float32)
    padded[:, 300:] = scene  # the empty band the Rosette got
    a = hoo(scene, 1.4)
    b = hoo(padded, 1.4)[:, 300:]
    core = (slice(290, 310), slice(440, 460))
    assert _blue_over_red(b, *core) == pytest.approx(_blue_over_red(a, *core), rel=0.15)
    assert not hoo(padded, 1.4)[:, :300].any()  # empty stays empty


# --- Bright stars painted square colour patches ---------------------------------


def test_stars_do_not_paint_square_patches() -> None:
    rng = np.random.default_rng(9)
    h, w = 400, 600
    yy, xx = np.mgrid[0:h, 0:w]
    nebula = np.exp(-((xx - 150) ** 2 + (yy - 200) ** 2) / (2 * 80.0**2))
    star = np.exp(-((yy - 200) ** 2 + (xx - 470) ** 2) / (2 * 3.0**2))  # in empty sky
    ha = 0.07 + 0.02 * nebula + 0.3 * star
    oiii = 0.07 + 0.005 * nebula + 0.9 * star  # stars far brighter in OIII
    img = np.stack([ha, oiii, oiii]) + rng.normal(0, 1e-4, (3, h, w))
    out = hoo(img.astype(np.float32), 1.4)
    # The sky around the star, where a coloured block would show, stays grey.
    around = (slice(180, 220), slice(486, 500))
    r, b = np.median(out[0][around]), np.median(out[2][around])
    assert b / r == pytest.approx(1.0, abs=0.1)
