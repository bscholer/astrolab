"""Quick preview: the finishing chain on a small copy, as the editor sees it."""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from astropy.io import fits
from PIL import Image

import nodes.basic  # noqa: F401  registers nodes
from server import quick_preview as qp
from server.templates import load_template


def _base(path: Path, shape=(3, 300, 420)) -> Path:
    rng = np.random.default_rng(3)
    data = (0.002 + 0.0004 * rng.standard_normal(shape)).astype(np.float32)
    data[0, 100:200, 150:300] += 0.004  # a nebula, brighter in H-alpha
    data[:, 50, 60] += 0.3  # a star
    fits.PrimaryHDU(data.clip(1e-5, 1)).writeto(path)
    return path


def test_the_base_is_the_step_before_the_finishing_chain() -> None:
    for template_id in ("stack_per_night_duo", "stack_per_night", "calibrate_register_stack"):
        split = qp.finishing_split(load_template(template_id))
        assert split is not None
        assert split[1] == "graxpert_denoise" and split[2] == "image"


def test_render_returns_a_jpeg_of_the_preview_size(tmp_path: Path) -> None:
    template = load_template("stack_per_night_duo")
    path = _base(tmp_path / "base.fit")
    jpeg = qp.render(path, "h1", template, {}, max_edge=200)
    img = Image.open(io.BytesIO(jpeg))
    assert img.format == "JPEG" and max(img.size) <= 200


def test_overrides_change_the_picture(tmp_path: Path) -> None:
    template = load_template("stack_per_night_duo")
    path = _base(tmp_path / "base.fit")
    plain = np.asarray(Image.open(io.BytesIO(qp.render(path, "h2", template, {}, 200))), float)
    bright = qp.render(path, "h2", template, {"stretch": {"target_bg": 0.3}}, 200)
    brighter = np.asarray(Image.open(io.BytesIO(bright)), float)
    assert brighter.mean() > plain.mean() + 5
    toned = qp.render(path, "h2", template, {"starnet_recombine": {"contrast": 0.8}}, 200)
    assert not np.array_equal(np.asarray(Image.open(io.BytesIO(toned))), plain.astype(np.uint8))


def test_effective_params_reject_out_of_range_values() -> None:
    template = load_template("stack_per_night_duo")
    import pydantic
    import pytest

    with pytest.raises(pydantic.ValidationError):
        qp.effective_params(template, {"stretch": {"target_bg": 5}})
