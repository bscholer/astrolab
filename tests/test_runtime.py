"""End-to-end runner test for the foundation slice.

Verifies the wiring: build a tiny template with one downscale node, run it,
get a downscaled PNG out of the cache. Re-run and confirm we hit the cache
instead of recomputing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

import nodes.basic  # noqa: F401  registers the downscale node
from server.cache import ContentCache
from server.models import Job, NodeSpec, Ref, Template
from server.ports import PortType
from server.runtime import run_job


def _make_test_png(path: Path, size: tuple[int, int] = (2000, 1000)) -> None:
    img = Image.new("RGB", size, color=(128, 64, 200))
    img.save(path, format="PNG")


def _build_template() -> Template:
    return Template(
        id="downscale_only",
        version=1,
        description="Downscale a PNG.",
        nodes=[
            NodeSpec(
                id="ds",
                kind="downscale",
                params={"target_size_px": 512},
            ),
        ],
        outputs={"image": "ds.image"},
    )


def _job_with_input(src: Path) -> Job:
    return Job(
        template_id="downscale_only",
        template_version=1,
        inputs={
            "ds.image": Ref(
                node_hash="external",
                port="image",
                path=src,
                type=PortType.IMAGE_PNG,
            )
        },
    )


def test_runner_executes_one_node_end_to_end(tmp_path: Path, astrolab_home: Path) -> None:
    src = tmp_path / "in.png"
    _make_test_png(src, (2000, 1000))

    outputs = run_job(_build_template(), _job_with_input(src))

    assert "image" in outputs
    out_ref = outputs["image"]
    assert out_ref.path.exists()
    assert out_ref.type is PortType.IMAGE_PNG

    with Image.open(out_ref.path) as img:
        w, h = img.size
        assert max(w, h) == 512
        # Original aspect ratio preserved (within a pixel).
        assert abs(w / h - 2.0) < 0.01


def test_runner_serves_from_cache_on_rerun(tmp_path: Path, astrolab_home: Path) -> None:
    src = tmp_path / "in.png"
    _make_test_png(src)

    template = _build_template()
    job = _job_with_input(src)

    first = run_job(template, job)
    first_path = first["image"].path
    first_mtime = first_path.stat().st_mtime_ns

    second = run_job(template, job)
    second_path = second["image"].path

    assert second_path == first_path, "cache should hand back the same path"
    # If the runner did not hit cache it would have re-reserved (rmtree) and
    # re-written, changing the mtime.
    assert second_path.stat().st_mtime_ns == first_mtime, "expected cache hit, got re-run"


def test_runner_cache_keyed_on_params(tmp_path: Path, astrolab_home: Path) -> None:
    src = tmp_path / "in.png"
    _make_test_png(src)

    base_template = _build_template()
    other_template = base_template.model_copy(deep=True)
    other_template.nodes[0].params = {"target_size_px": 256}

    base = run_job(base_template, _job_with_input(src))
    other = run_job(other_template, _job_with_input(src))

    assert base["image"].node_hash != other["image"].node_hash
    with Image.open(other["image"].path) as img:
        assert max(img.size) == 256


def test_runner_cache_keyed_on_inputs(tmp_path: Path, astrolab_home: Path) -> None:
    src_a = tmp_path / "a.png"
    src_b = tmp_path / "b.png"
    _make_test_png(src_a, (2000, 1000))
    _make_test_png(src_b, (1500, 1500))

    template = _build_template()

    # Different external inputs produce different cache entries because the
    # external Ref's node_hash differs (we used "external" + path-derived).
    # In practice, external inputs would be hashed by file content; for now
    # the convention is the caller picks a unique node_hash per logical input.
    job_a = Job(
        template_id="downscale_only",
        template_version=1,
        inputs={
            "ds.image": Ref(
                node_hash="ext-a", port="image", path=src_a, type=PortType.IMAGE_PNG
            )
        },
    )
    job_b = Job(
        template_id="downscale_only",
        template_version=1,
        inputs={
            "ds.image": Ref(
                node_hash="ext-b", port="image", path=src_b, type=PortType.IMAGE_PNG
            )
        },
    )

    out_a = run_job(template, job_a)
    out_b = run_job(template, job_b)
    assert out_a["image"].node_hash != out_b["image"].node_hash


def test_runner_writes_into_cache_root(tmp_path: Path, astrolab_home: Path) -> None:
    src = tmp_path / "in.png"
    _make_test_png(src)

    outputs = run_job(_build_template(), _job_with_input(src))
    out_path = outputs["image"].path

    cache_root = astrolab_home / "cache"
    assert cache_root in out_path.parents


def test_runner_raises_on_unresolved_input(astrolab_home: Path) -> None:
    template = _build_template()
    bare_job = Job(template_id="downscale_only", template_version=1)

    with pytest.raises(Exception) as excinfo:
        run_job(template, bare_job)
    assert "ds.image" in str(excinfo.value) or "image" in str(excinfo.value)


def test_runner_uses_explicit_cache_when_provided(tmp_path: Path) -> None:
    src = tmp_path / "in.png"
    _make_test_png(src)

    custom_cache_root = tmp_path / "my-cache"
    cache = ContentCache(root=custom_cache_root)

    outputs = run_job(_build_template(), _job_with_input(src), cache=cache)
    assert custom_cache_root in outputs["image"].path.parents
