"""Preview rendering: FITS -> stretched PNG, PNG passthrough, sequence frame pick."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from PIL import Image

from server.cache import ContentCache
from server.preview import PreviewError, render_preview


def _commit_entry(cache: ContentCache, node_hash: str, files: dict[str, bytes | Path]) -> Path:
    """Pre-populate a fake cache entry. Each key is the file/dir name to write."""
    entry = cache.reserve(node_hash)
    for name, content in files.items():
        target = entry / name
        if isinstance(content, Path) and content.is_dir():
            # copy the directory shape
            import shutil
            shutil.copytree(content, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content if isinstance(content, bytes) else content.read_bytes())
    (entry / "_done").touch()
    return entry


def _make_fits(path: Path, shape=(64, 64), seed: int = 0) -> Path:
    rng = np.random.default_rng(seed)
    data = rng.normal(loc=1000.0, scale=50.0, size=shape).astype(np.float32)
    # Add a brighter peak so the stretch has something to do.
    data[shape[0] // 2, shape[1] // 2] = 5000.0
    fits.PrimaryHDU(data=data).writeto(path, overwrite=True)
    return path


def test_render_passthrough_png(tmp_path: Path) -> None:
    cache = ContentCache(root=tmp_path / "cache")
    src_png = tmp_path / "src.png"
    Image.new("RGB", (200, 100), (10, 20, 30)).save(src_png)
    _commit_entry(cache, "h0", {"image.png": src_png.read_bytes()})

    out = render_preview(cache, "h0", "image")
    assert out.suffix == ".png"
    assert out.exists()
    img = Image.open(out)
    # Thumbnailed: long edge clipped to 512, but original is 200 so untouched.
    assert max(img.size) <= 512


def test_render_fits_image(tmp_path: Path) -> None:
    cache = ContentCache(root=tmp_path / "cache")
    fits_src = _make_fits(tmp_path / "src.fit")
    _commit_entry(cache, "h1", {"image.fit": fits_src.read_bytes()})

    out = render_preview(cache, "h1", "image")
    img = Image.open(out)
    assert img.mode == "RGB"
    arr = np.array(img)
    # Should have pixel variation (stretch worked).
    assert arr.std() > 5


def test_render_bayer_fits_yields_color(tmp_path: Path) -> None:
    """OSC raws (BAYERPAT='RGGB' etc.) must be debayered before stretch,
    otherwise the alternating R/G/B pixels render as monochrome sparkles."""
    cache = ContentCache(root=tmp_path / "cache")
    rng = np.random.default_rng(0)
    # Build a 2x2 super-pixel where R, G, and B each have a distinct mean
    # signal so the debayered image must show channel separation.
    h, w = 64, 64
    arr = np.zeros((h, w), dtype=np.float32)
    arr[0::2, 0::2] = rng.normal(2000, 50, ((h + 1) // 2, (w + 1) // 2))   # R
    arr[0::2, 1::2] = rng.normal(1000, 50, ((h + 1) // 2, (w + 1) // 2))   # G1
    arr[1::2, 0::2] = rng.normal(1000, 50, ((h + 1) // 2, (w + 1) // 2))   # G2
    arr[1::2, 1::2] = rng.normal(500,  50, ((h + 1) // 2, (w + 1) // 2))   # B
    src = tmp_path / "bayer.fit"
    fits.PrimaryHDU(data=arr, header=fits.Header({"BAYERPAT": "RGGB"})).writeto(
        src, overwrite=True
    )
    _commit_entry(cache, "hbayer", {"image.fit": src.read_bytes()})

    out = render_preview(cache, "hbayer", "image")
    img = np.array(Image.open(out))
    # Half-res because of the 2x2 collapse.
    assert img.shape[0] <= 32 and img.shape[1] <= 32
    # If the renderer had treated this as mono, R == G == B per pixel; the
    # debayered version must have at least some channel divergence because
    # R came from different pixels than B.
    rg_diff = np.abs(img[..., 0].astype(int) - img[..., 1].astype(int))
    rb_diff = np.abs(img[..., 0].astype(int) - img[..., 2].astype(int))
    assert rg_diff.max() > 0
    assert rb_diff.max() > 0


def test_render_sequence_picks_middle_frame(tmp_path: Path) -> None:
    cache = ContentCache(root=tmp_path / "cache")
    seq_dir = tmp_path / "seq"
    seq_dir.mkdir()
    for i in range(5):
        _make_fits(seq_dir / f"r_pp_light_{i:05d}.fit", seed=i)
    (seq_dir / "r_pp_light_.seq").write_bytes(b"# seq index")

    # Place sequence/ as a directory inside the cache entry.
    entry = cache.reserve("h2")
    target = entry / "sequence"
    target.mkdir()
    for f in seq_dir.iterdir():
        (target / f.name).write_bytes(f.read_bytes())
    (entry / "_done").touch()

    out = render_preview(cache, "h2", "sequence")
    assert out.exists()
    img = Image.open(out)
    assert img.size[0] > 0


def test_unknown_hash_raises(tmp_path: Path) -> None:
    cache = ContentCache(root=tmp_path / "cache")
    with pytest.raises(PreviewError, match="no committed cache entry"):
        render_preview(cache, "doesnotexist", "image")


def test_unknown_port_raises(tmp_path: Path) -> None:
    cache = ContentCache(root=tmp_path / "cache")
    _commit_entry(cache, "h3", {"image.fit": _make_fits(tmp_path / "x.fit").read_bytes()})
    with pytest.raises(PreviewError, match="not found"):
        render_preview(cache, "h3", "totally_wrong_port")


def test_preview_is_cached_across_calls(tmp_path: Path) -> None:
    cache = ContentCache(root=tmp_path / "cache")
    _commit_entry(cache, "h4", {"image.fit": _make_fits(tmp_path / "x.fit").read_bytes()})
    out1 = render_preview(cache, "h4", "image")
    mtime1 = out1.stat().st_mtime
    out2 = render_preview(cache, "h4", "image")
    assert out1 == out2
    assert out2.stat().st_mtime == mtime1  # not re-rendered
