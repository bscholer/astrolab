"""Framing on the sky: a crop stored as RA/Dec, size and position angle.

The point of a sky box is that it keeps framing the same stars when a new
night grows or shifts the combined canvas, at any angle. These tests build
synthetic plate-solved canvases with stars at known sky positions.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

import numpy as np
import pytest
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.wcs import WCS

from nodes.basic.crop import CropNode, CropParams
from nodes.basic.framing import (
    CanvasBox,
    SkyBox,
    canvas_to_display,
    canvas_to_sky,
    celestial_wcs,
    cut,
    display_to_canvas,
    north_display_angle,
    sky_to_canvas,
)
from server.models import Ref, RunContext
from server.ports import PortType

SCALE = 2.78 / 3600  # deg/px, the DWARF 3 tele
RA0, DEC0 = 314.7, 44.3  # near the North America nebula


def _wcs(width: int, height: int, *, crpix=None, rot_deg: float = 0.0, mirror: bool = False) -> WCS:
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [RA0, DEC0]
    w.wcs.crpix = list(crpix) if crpix is not None else [width / 2 + 0.5, height / 2 + 0.5]
    r = math.radians(rot_deg)
    sx = SCALE if mirror else -SCALE
    w.wcs.cd = np.array(
        [[sx * math.cos(r), -SCALE * math.sin(r)], [sx * math.sin(r), SCALE * math.cos(r)]]
    )
    return w


def _stars(n: int = 40, seed: int = 3) -> SkyCoord:
    rng = np.random.default_rng(seed)
    return SkyCoord(
        RA0 + rng.uniform(-0.6, 0.6, n) / math.cos(math.radians(DEC0)),
        DEC0 + rng.uniform(-0.4, 0.4, n),
        unit="deg",
    )


def _render(w: WCS, width: int, height: int, stars: SkyCoord) -> np.ndarray:
    """(3, H, W) sky at 0.02 with a Gaussian star at each position."""
    img = np.full((height, width), 0.02, np.float32)
    yy, xx = np.mgrid[0:height, 0:width]
    px, py = w.world_to_pixel(stars)
    for x, y in zip(px, py, strict=True):
        if -10 < x < width + 10 and -10 < y < height + 10:
            img += 0.5 * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * 1.5**2)).astype(np.float32)
    return np.stack([img, img * 0.8, img * 0.6])


def _write(path: Path, data: np.ndarray, w: WCS | None) -> Path:
    header = w.to_header() if w is not None else fits.Header()
    if w is not None:
        header["PLTSOLVD"] = True
    fits.PrimaryHDU(data, header=header).writeto(path, overwrite=True)
    return path


def _run(tmp_path: Path, src: Path, params: CropParams, name: str = "out"):
    out = tmp_path / name
    out.mkdir()
    (tmp_path / "tmp").mkdir(exist_ok=True)
    ctx = RunContext(tmpdir=tmp_path / "tmp", progress=lambda p, m: None, log=logging.getLogger("t"))
    ref = Ref(node_hash="t", port="image", path=src, type=PortType.IMAGE_FITS)
    res = CropNode().run({"image": ref}, params, ctx, out)
    with fits.open(res["image"].path, memmap=False) as hdul:
        return hdul[0].data, hdul[0].header


def _sky(box: SkyBox) -> CropParams:
    return CropParams(
        ra=box.ra, dec=box.dec, sky_width=box.width_arcmin, sky_height=box.height_arcmin, angle=box.angle
    )


@pytest.mark.parametrize("rot_deg,mirror", [(0, False), (33, False), (-120, False), (20, True)])
@pytest.mark.parametrize("phi", [0.0, 17.0, 90.0, 205.0])
def test_display_sky_round_trip(rot_deg: float, mirror: bool, phi: float) -> None:
    w = _wcs(800, 500, rot_deg=rot_deg, mirror=mirror)
    drawn = display_to_canvas(500, 410.0, 230.0, 300.0, 180.0, phi)
    back = canvas_to_display(500, sky_to_canvas(w, canvas_to_sky(w, drawn)))
    assert back["cx"] == pytest.approx(410.0, abs=0.01)
    assert back["cy"] == pytest.approx(230.0, abs=0.01)
    assert back["width"] == pytest.approx(300.0, rel=1e-3)
    assert back["height"] == pytest.approx(180.0, rel=1e-3)
    assert (back["angle"] - phi + 180) % 360 - 180 == pytest.approx(0.0, abs=0.05)


def test_north_up_display_angle() -> None:
    """Siril's convention (row 0 at the bottom) with a standard solve is north
    up, east left: the editor's 'north up' snap is then 0."""
    assert north_display_angle(_wcs(800, 500), 500, 800) == pytest.approx(0.0, abs=0.01)
    # Canvas turned 30 degrees: north is 30 degrees off up on the display.
    a = north_display_angle(_wcs(800, 500, rot_deg=30), 500, 800)
    assert min(a, 360 - a) == pytest.approx(30.0, abs=0.05)


def test_sky_box_frames_the_same_stars_after_the_canvas_grows(tmp_path: Path) -> None:
    """Night two points a bit off and the union canvas grows and shifts: the
    same sky box still frames the same stars, at the same angle."""
    stars = _stars()
    w1 = _wcs(640, 420)
    one = _write(tmp_path / "one.fit", _render(w1, 640, 420, stars), w1)
    # Bigger canvas, the old one sitting 150 px right and 60 px up inside it.
    w2 = _wcs(900, 560, crpix=(w1.wcs.crpix[0] + 150, w1.wcs.crpix[1] + 60))
    two = _write(tmp_path / "two.fit", _render(w2, 900, 560, stars), w2)

    box = canvas_to_sky(w1, CanvasBox(330.0, 200.0, 360.0, 240.0, math.radians(-25)))
    a, ha = _run(tmp_path, one, _sky(box), "a")
    b, hb = _run(tmp_path, two, _sky(box), "b")
    assert a.shape == b.shape
    lit = (a > 0) & (b > 0)
    assert lit.mean() > 0.9
    # Same stars in the same places (sub-pixel resampling differences only).
    assert np.abs(a - b)[lit].max() < 0.02
    corr = np.corrcoef(a[0][lit[0]], b[0][lit[0]])[0, 1]
    assert corr > 0.99
    # The output's own solve agrees on the sky position of a star.
    star = stars[np.argmin(stars.separation(SkyCoord(box.ra, box.dec, unit="deg")))]
    pa = np.array(celestial_wcs(ha).world_to_pixel(star))
    pb = np.array(celestial_wcs(hb).world_to_pixel(star))
    assert np.allclose(pa, pb, atol=0.05)


def test_output_is_turned_to_the_position_angle(tmp_path: Path) -> None:
    """Up on the output is the box's position angle. With the sky's own
    parity (east left of north), turning east to the top puts north on the
    right: north sits PA degrees clockwise from up."""
    w = _wcs(700, 500, rot_deg=12)
    src = _write(tmp_path / "in.fit", _render(w, 700, 500, _stars()), w)
    for pa in (0.0, 90.0, 135.0):
        box = SkyBox(RA0, DEC0, 12.0, 8.0, pa)
        data, header = _run(tmp_path, src, _sky(box), f"pa{int(pa)}")
        out_w = celestial_wcs(header)
        north = north_display_angle(out_w, data.shape[1], data.shape[2])
        assert (north - pa + 180) % 360 - 180 == pytest.approx(0.0, abs=0.1)
        # 12 x 8 arcmin at 2.78"/px.
        assert data.shape[2] == round(12 * 60 / 2.78)
        assert data.shape[1] == round(8 * 60 / 2.78)


def test_quarter_turn_matches_rot90(tmp_path: Path) -> None:
    """A 90-degree box on whole pixels is exactly the array turned."""
    rng = np.random.default_rng(1)
    planes = rng.uniform(0.01, 1.0, (1, 40, 60)).astype(np.float32)
    straight = cut(planes, CanvasBox(30.0, 20.0, 20.0, 10.0))
    turned = cut(planes, CanvasBox(30.0, 20.0, 10.0, 20.0, math.radians(90)))
    assert np.allclose(np.rot90(straight[0], 1), turned[0], atol=1e-5)


def test_turned_box_keeps_nodata_as_nodata() -> None:
    """Past the canvas and over the registration border is 0; real data never is."""
    planes = np.zeros((1, 100, 100), np.float32)
    planes[0, 20:80, 20:80] = np.random.default_rng(2).uniform(0.001, 0.003, (60, 60))
    out = cut(planes, CanvasBox(50.0, 50.0, 120.0, 120.0, math.radians(30)))[0]
    assert (out == 0).any()
    centre = out[45:75, 45:75]
    assert (centre > 0).all()


def test_canvas_box_y_is_from_the_top_of_the_preview(tmp_path: Path) -> None:
    """Previews and the saved picture put FITS row 0 at the bottom, so a box at
    the top of the preview takes the last rows of the array."""
    data = np.zeros((40, 40), np.float32) + 0.01
    data[30:, :] = 0.5  # the top of the preview
    src = _write(tmp_path / "in.fit", data, None)
    out, _ = _run(tmp_path, src, CropParams(x=0.0, y=0.0, width=1.0, height=0.25))
    assert out.shape == (10, 40)
    assert np.allclose(out, 0.5)


def test_sky_box_on_an_unsolved_image_fails(tmp_path: Path) -> None:
    src = _write(tmp_path / "in.fit", np.full((30, 30), 0.1, np.float32), None)
    with pytest.raises(RuntimeError, match="plate solve"):
        _run(tmp_path, src, CropParams(ra=RA0, dec=DEC0, sky_width=5, sky_height=5))


# ---------------------------------------------------------------------------
# combine_stacks always hands the crop a plate-solved, upright image
# ---------------------------------------------------------------------------


class _FakeSiril:
    """Stands in for Siril's platesolve: writes solve_outx.fit turned over
    with an upright solve, like Siril does with a mirrored stack."""

    calls: list[list[str]] = []
    succeed = True

    def run(self, lines, working_dir, **_kw):
        from types import SimpleNamespace

        _FakeSiril.calls.append(lines)
        if _FakeSiril.succeed:
            with fits.open(working_dir / "solve_inx.fit") as hdul:
                data = hdul[0].data[..., ::-1, :]
            h, w = data.shape[-2:]
            _write(working_dir / "solve_outx.fit", data, _wcs(w, h))
        return SimpleNamespace(returncode=0 if _FakeSiril.succeed else 1, stdout="log: solve")


def _combine_one(tmp_path: Path, src: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from nodes.basic import combine_stacks
    from nodes.basic.combine_stacks import CombineStacksNode, CombineStacksParams

    _FakeSiril.calls = []
    monkeypatch.setattr(combine_stacks, "SirilRuntime", _FakeSiril)
    out = tmp_path / "combined"
    out.mkdir()
    (tmp_path / "tmp").mkdir(exist_ok=True)
    ctx = RunContext(tmpdir=tmp_path / "tmp", progress=lambda p, m: None, log=logging.getLogger("t"))
    ref = Ref(node_hash="t", port="image", path=src, type=PortType.IMAGE_FITS)
    res = CombineStacksNode().run({"images": [ref]}, CombineStacksParams(), ctx, out)
    return res["image"].path


def test_one_night_master_comes_out_solved_and_upright(tmp_path, monkeypatch) -> None:
    """A one-night master used to pass the night's stack through: no solve,
    and mirrored (per-night duo stacks are stored north down)."""
    from nodes.basic.combine_stacks import solved_upright

    night = np.zeros((3, 30, 40), np.float32) + 0.02
    night[:, 0, :] = 0.5  # stored north down: the first row is the top of the sky
    src = _write(tmp_path / "night.fit", night, None)
    out = _combine_one(tmp_path, src, monkeypatch)
    assert len(_FakeSiril.calls) == 1 and any("platesolve" in c for c in _FakeSiril.calls[0])
    assert solved_upright(out)
    assert np.allclose(fits.getdata(out)[:, -1, :], 0.5)


def test_solved_upright_image_is_not_solved_again(tmp_path, monkeypatch) -> None:
    w = _wcs(40, 30)
    src = _write(tmp_path / "night.fit", np.full((3, 30, 40), 0.02, np.float32), w)
    _combine_one(tmp_path, src, monkeypatch)
    assert _FakeSiril.calls == []


def test_mirrored_solve_is_solved_again(tmp_path, monkeypatch) -> None:
    w = _wcs(40, 30, mirror=True)
    src = _write(tmp_path / "night.fit", np.full((3, 30, 40), 0.02, np.float32), w)
    _combine_one(tmp_path, src, monkeypatch)
    assert len(_FakeSiril.calls) == 1


def test_unsolvable_master_fails_instead_of_going_on(tmp_path, monkeypatch) -> None:
    src = _write(tmp_path / "night.fit", np.full((3, 30, 40), 0.02, np.float32), None)
    monkeypatch.setattr(_FakeSiril, "succeed", False)
    with pytest.raises(RuntimeError, match="plate-solve"):
        _combine_one(tmp_path, src, monkeypatch)


# ---------------------------------------------------------------------------
# The editor's API: draw on the canvas, store on the sky, draw it back
# ---------------------------------------------------------------------------


def test_framing_api_stores_on_the_sky_and_redraws_on_a_grown_canvas(tmp_path, astrolab_home) -> None:
    from fastapi.testclient import TestClient

    import server.api as api

    stars = _stars()
    w1 = _wcs(640, 420)
    w2 = _wcs(900, 560, crpix=(w1.wcs.crpix[0] + 150, w1.wcs.crpix[1] + 60))
    for h, w, (cw, ch) in (("night1", w1, (640, 420)), ("night2", w2, (900, 560))):
        entry = api.job_manager.cache.reserve(h)
        _write(entry / "image.fit", _render(w, cw, ch, stars), w)
        api.job_manager.cache.commit(h, {})
    with TestClient(api.app) as client:
        info = client.post("/api/framing/night1/image", json={"params": {}}).json()
        assert info["solved"] and info["box"] is None
        assert info["north_angle"] == pytest.approx(0.0, abs=0.01)
        assert info["arcsec_per_px"] == pytest.approx(2.78, rel=1e-3)

        drawn = {"cx": 300.0, "cy": 200.0, "width": 320.0, "height": 200.0, "angle": 30.0}
        params = client.post("/api/framing/night1/image/box", json=drawn).json()
        assert params["sky_width"] > 0 and params["width"] == 0
        assert params["angle"] == pytest.approx(330.0, abs=0.05)  # north tilts off-centre

        again = client.post("/api/framing/night1/image", json={"params": params}).json()["box"]
        assert again["cx"] == pytest.approx(300.0, abs=0.05)
        assert again["cy"] == pytest.approx(200.0, abs=0.05)
        # The grown canvas: same box, moved by the canvas shift (display Y down).
        grown = client.post("/api/framing/night2/image", json={"params": params}).json()["box"]
        assert grown["cx"] == pytest.approx(450.0, abs=0.05)
        assert grown["cy"] == pytest.approx(200.0 + (560 - 420) - 60, abs=0.05)
        assert grown["angle"] == pytest.approx(30.0, abs=0.01)
        assert client.post("/api/framing/nope/image", json={"params": {}}).status_code == 404
