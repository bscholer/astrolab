"""HEALPix addressing and tile handling for the Tonight thumbnails."""

from __future__ import annotations

import io
import math
import urllib.error
from email.message import Message
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from server import sky_thumbs as st

TILE_M31 = "Norder5/Dir0/Npix677.jpg"


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (512, 512), (10, 20, 30)).save(buf, "JPEG")
    return buf.getvalue()


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize(
    ("ra", "dec", "face"),
    [
        (45, 80, 0),
        (315, 60, 3),
        (0, 0, 4),
        (90, 0, 5),
        (270, 0, 7),
        (45, -60, 8),
        (315, -80, 11),
    ],
)
def test_ang_to_xyf_assigns_base_faces(ra: float, dec: float, face: int) -> None:
    _, _, f = st.ang_to_xyf(np.array([math.radians(ra)]), np.array([math.radians(dec)]), 12)
    assert int(f[0]) == face


def test_tile_address_of_m31_matches_survey_layout() -> None:
    """Pinned against real tiles: this address holds M 31 in the DSS survey."""
    order = 5
    ix, iy, f = st.ang_to_xyf(
        np.array([math.radians(10.685)]), np.array([math.radians(41.269)]), order + 9
    )
    npix = st.tile_npix(int(f[0]), int(ix[0]) >> 9, int(iy[0]) >> 9, order)
    assert st.tile_path(order, npix) == TILE_M31


def test_render_puts_the_target_in_the_centre() -> None:
    """A marked tile pixel at the target position must land mid-frame. A
    row/column swap in tile sampling sends it elsewhere."""
    ra, dec, fov = 83.82, -5.39, 1.5
    work = st.THUMB_PX * 2
    order = st.order_for_fov(fov, work)
    ix, iy, f = st.ang_to_xyf(
        np.array([math.radians(ra)]), np.array([math.radians(dec)]), order + 9
    )
    x, y = int(ix[0]), int(iy[0])
    wanted = st.tile_npix(int(f[0]), x >> 9, y >> 9, order)

    def fetch(_order: int, npix: int) -> np.ndarray | None:
        if npix != wanted:
            return None
        tile = np.zeros((512, 512, 3), dtype=np.uint8)
        tile[max(0, (x & 511) - 3) : (x & 511) + 4, max(0, (y & 511) - 3) : (y & 511) + 4] = 255
        return tile

    img = np.asarray(Image.open(io.BytesIO(st.render(ra, dec, fov, fetch=fetch))).convert("L"))
    row, col = np.unravel_index(int(img.argmax()), img.shape)
    assert abs(row - st.THUMB_PX / 2) <= 3
    assert abs(col - st.THUMB_PX / 2) <= 3


@pytest.mark.parametrize("dec", [-89.5, -41.8, 0.0, 41.8, 89.5])
@pytest.mark.parametrize("size", [None, 1.0, 30.0, 180.0, 1000.0])
def test_tile_budget_holds_everywhere(dec: float, size: float | None) -> None:
    """A frame above the budget can never render, so that target stays blank."""
    fov = st.fov_for_size(size)
    work = st.THUMB_PX * 2
    order = st.order_for_fov(fov, work)
    ra, de = st.tan_grid(120.0, dec, fov, work)
    ix, iy, f = st.ang_to_xyf(ra, de, order + 9)
    tiles = np.unique(np.stack([f, ix >> 9, iy >> 9], axis=-1).reshape(-1, 3), axis=0)
    assert len(tiles) <= st._MAX_TILES


def test_fetch_falls_back_to_the_second_host(monkeypatch: pytest.MonkeyPatch) -> None:
    urls: list[str] = []

    def urlopen(url: str, timeout: float) -> _Resp:
        urls.append(url)
        if len(urls) == 1:
            raise urllib.error.URLError("refused")
        return _Resp(_jpeg())

    monkeypatch.setattr(st.urllib.request, "urlopen", urlopen)
    tile = st.fetch_tile(5, 1234)
    assert tile is not None
    assert tile.shape == (512, 512, 3)
    assert urls[1].startswith(st.TILE_BASES[1])


def test_fetch_treats_404_as_a_sky_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    def urlopen(url: str, timeout: float) -> _Resp:
        raise urllib.error.HTTPError(url, 404, "missing", Message(), None)

    monkeypatch.setattr(st.urllib.request, "urlopen", urlopen)
    assert st.fetch_tile(5, 1234) is None


def test_fetch_raises_when_every_host_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def urlopen(url: str, timeout: float) -> _Resp:
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(st.urllib.request, "urlopen", urlopen)
    with pytest.raises(st.ThumbUnavailable):
        st.fetch_tile(5, 1234)


def test_get_thumb_renders_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[float] = []

    def render(ra: float, dec: float, fov: float) -> bytes:
        calls.append(fov)
        return b"jpeg"

    monkeypatch.setattr(st, "render", render)
    assert st.get_thumb(tmp_path, "M 31", 10.0, 41.0, 180.0) == b"jpeg"
    assert st.get_thumb(tmp_path, "M 31", 10.0, 41.0, 180.0) == b"jpeg"
    assert len(calls) == 1


def test_get_thumb_does_not_store_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def render(ra: float, dec: float, fov: float) -> bytes:
        raise st.ThumbUnavailable("down")

    monkeypatch.setattr(st, "render", render)
    with pytest.raises(st.ThumbUnavailable):
        st.get_thumb(tmp_path, "M 31", 10.0, 41.0, 180.0)
    assert not st.thumb_path(tmp_path, "M 31").exists()
