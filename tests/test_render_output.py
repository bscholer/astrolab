"""render_output: full-resolution artifact path.

Mirrors test_preview.py's shape but verifies the no-thumbnail behavior:
- PNG passthrough hands back the original file (no re-encode, no resize)
- FITS gets autostretched at full resolution (long edge > THUMB_LONG_EDGE
  when the source warrants)
- Output cache is namespaced so preview and output renders coexist
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from astropy.io import fits
from PIL import Image

from server.cache import ContentCache
from server.preview import THUMB_LONG_EDGE, render_output, render_preview


def _commit_entry(cache: ContentCache, node_hash: str, files: dict[str, bytes]) -> Path:
    entry = cache.reserve(node_hash)
    for name, blob in files.items():
        (entry / name).write_bytes(blob)
    (entry / "_done").touch()
    return entry


def _make_fits(path: Path, *, shape: tuple[int, int]) -> bytes:
    rng = np.random.default_rng(0)
    data = rng.normal(loc=1000.0, scale=50.0, size=shape).astype(np.float32)
    data[shape[0] // 2, shape[1] // 2] = 5000.0
    fits.PrimaryHDU(data=data).writeto(path, overwrite=True)
    return path.read_bytes()


def test_render_output_png_returns_original_file(tmp_path: Path) -> None:
    """A committed full-res PNG should be served as-is — no re-encode,
    no thumbnail. The bug this protects against: 'Open full' on the
    save_image output linked to /api/preview, which would clip a
    6000x4000 export to 512px."""
    cache = ContentCache(root=tmp_path / "cache")
    src_png = tmp_path / "src.png"
    Image.new("RGB", (2000, 1500), (10, 20, 30)).save(src_png)
    entry = _commit_entry(cache, "h_png", {"image.png": src_png.read_bytes()})

    out = render_output(cache, "h_png", "image")
    # Original PNG is in the entry — render_output returns its path,
    # not a derived thumbnail.
    assert out == entry / "image.png"
    img = Image.open(out)
    assert img.size == (2000, 1500), "full resolution must survive"


def test_render_output_fits_full_resolution(tmp_path: Path) -> None:
    """FITS taller than THUMB_LONG_EDGE produces an output PNG that
    keeps the full resolution (no `.thumbnail()` call)."""
    cache = ContentCache(root=tmp_path / "cache")
    big_dim = THUMB_LONG_EDGE * 2 + 50
    fits_blob = _make_fits(tmp_path / "src.fit", shape=(big_dim, big_dim))
    _commit_entry(cache, "h_fits", {"image.fit": fits_blob})

    out = render_output(cache, "h_fits", "image")
    assert out.exists() and out.suffix == ".png"
    img = Image.open(out)
    assert max(img.size) > THUMB_LONG_EDGE, (
        f"render_output should keep full res, got {img.size}"
    )


def test_render_output_caches_inside_entry(tmp_path: Path) -> None:
    """Second call must hit the cached `_output_<port>.png` file."""
    cache = ContentCache(root=tmp_path / "cache")
    fits_blob = _make_fits(tmp_path / "src.fit", shape=(800, 800))
    entry = _commit_entry(cache, "h_cached", {"image.fit": fits_blob})

    out1 = render_output(cache, "h_cached", "image")
    out2 = render_output(cache, "h_cached", "image")
    assert out1 == out2
    assert out1.parent == entry
    assert out1.name.startswith("_output_")
    # Confirm mtime is stable on the cached path (no re-render).
    mtime1 = out1.stat().st_mtime_ns
    out3 = render_output(cache, "h_cached", "image")
    assert out3.stat().st_mtime_ns == mtime1


def test_preview_and_output_caches_dont_collide(tmp_path: Path) -> None:
    """preview and output cache to different filenames inside the
    entry so they coexist."""
    cache = ContentCache(root=tmp_path / "cache")
    fits_blob = _make_fits(tmp_path / "src.fit", shape=(800, 800))
    entry = _commit_entry(cache, "h_dual", {"image.fit": fits_blob})

    preview = render_preview(cache, "h_dual", "image")
    output = render_output(cache, "h_dual", "image")
    assert preview != output
    assert preview.parent == entry
    assert output.parent == entry

    p_img = Image.open(preview)
    o_img = Image.open(output)
    # Preview is thumbnailed; output keeps full res.
    assert max(p_img.size) <= THUMB_LONG_EDGE
    assert max(o_img.size) == 800
