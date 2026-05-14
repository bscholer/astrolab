"""End-to-end runner test for the foundation slice.

Verifies the wiring: build a tiny template with one downscale node, run it,
get a downscaled PNG out of the cache. Re-run and confirm we hit the cache
instead of recomputing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from pydantic import BaseModel

import nodes.basic  # noqa: F401  registers the downscale node
from nodes.base import Node
from server.cache import ContentCache
from server.models import Job, NodeSpec, Ref, RunContext, Template
from server.ports import PortType
from server.registry import register
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


# ---- multi-output cache regression --------------------------------------------
#
# narrowband_extract returns two ports (ha, oiii) and writes them as
# `r_results_ha.fit` and `r_results_oiii.fit`. Before the per-entry manifest
# landed, the cache lookup reconstructed paths via a `<port>.*` glob and
# couldn't find these files, so the second run of the pipeline blew up with
# `cached entry <hash> missing output 'ha'`. The test pair below pins both
# the multi-port write/read round-trip and the legacy fallback so we don't
# regress.


class _SplitParams(BaseModel):
    pass


@register("__test_split_writer__")
class _SplitWriterNode(Node[_SplitParams]):
    """Test node that mimics narrowband_extract's filename choice.

    Declares two output ports (`alpha`, `beta`) but writes them as
    `prefix_alpha.dat` / `prefix_beta.dat`, NOT `alpha.dat` / `beta.dat`.
    The cache lookup must read the manifest to find the files; if it
    falls back to globbing `<port>.*` it will fail to locate them.
    """

    id = "__test_split_writer__"
    version = 1
    cost = "cheap"
    inputs = {"image": PortType.IMAGE_PNG}
    outputs = {
        "alpha": PortType.IMAGE_FITS,
        "beta": PortType.IMAGE_FITS,
    }
    params_schema = _SplitParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: _SplitParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out_dir_path = Path(out_dir)  # type: ignore[arg-type]
        alpha = out_dir_path / "prefix_alpha.dat"
        beta = out_dir_path / "prefix_beta.dat"
        alpha.write_bytes(b"alpha")
        beta.write_bytes(b"beta")
        return {
            "alpha": Ref(node_hash="", port="alpha", path=alpha, type=PortType.IMAGE_FITS),
            "beta": Ref(node_hash="", port="beta", path=beta, type=PortType.IMAGE_FITS),
        }


class _WarnParams(BaseModel):
    pass


@register("__test_warn_emitter__")
class _WarnEmitterNode(Node[_WarnParams]):
    """Test node whose only job is to call ctx.warn so we can pin the
    persistence + replay contract end to end.
    """

    id = "__test_warn_emitter__"
    version = 1
    cost = "cheap"
    inputs = {"image": PortType.IMAGE_PNG}
    outputs = {"image": PortType.IMAGE_PNG}
    params_schema = _WarnParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: _WarnParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out_dir_path = Path(out_dir)  # type: ignore[arg-type]
        out_path = out_dir_path / "image.png"
        # Copy input through unchanged; we only care about the warning
        # path, not the data.
        out_path.write_bytes(inputs["image"].path.read_bytes())
        ctx.warn("fallback", "thermal margin exceeded", details={"delta_c": 7.5})
        ctx.warn("partial", "dropped 3 frames")
        return {
            "image": Ref(
                node_hash="", port="image", path=out_path, type=PortType.IMAGE_PNG
            )
        }


def _warn_template() -> Template:
    return Template(
        id="warn_only",
        version=1,
        nodes=[NodeSpec(id="warn", kind="__test_warn_emitter__")],
        outputs={"image": "warn.image"},
    )


def _warn_job(src: Path) -> Job:
    return Job(
        template_id="warn_only",
        template_version=1,
        inputs={
            "warn.image": Ref(
                node_hash="external", port="image", path=src, type=PortType.IMAGE_PNG
            )
        },
    )


def test_runner_emits_and_persists_node_warnings(
    tmp_path: Path, astrolab_home: Path
) -> None:
    """ctx.warn calls during run() must:

      1. surface as node_warning events on the first pass, AND
      2. land in the cache entry's _warnings.json sidecar, AND
      3. replay as node_warning events on a cache hit so a fresh
         consumer sees them without having to re-run the node.
    """
    src = tmp_path / "in.png"
    _make_test_png(src)

    events_first: list[dict] = []
    run_job(_warn_template(), _warn_job(src), events=events_first.append)
    warnings_first = [e for e in events_first if e["type"] == "node_warning"]
    assert len(warnings_first) == 2
    assert {w["kind"] for w in warnings_first} == {"fallback", "partial"}
    fallback = next(w for w in warnings_first if w["kind"] == "fallback")
    assert fallback["details"] == {"delta_c": 7.5}

    # Replay on cache hit.
    events_second: list[dict] = []
    run_job(_warn_template(), _warn_job(src), events=events_second.append)
    warnings_second = [e for e in events_second if e["type"] == "node_warning"]
    assert len(warnings_second) == 2, (
        f"expected warnings to replay on cache hit, got: {events_second!r}"
    )
    assert any(e["type"] == "node_cached" for e in events_second), (
        "second run should have been a cache hit"
    )


def _split_template() -> Template:
    return Template(
        id="split_only",
        version=1,
        nodes=[NodeSpec(id="split", kind="__test_split_writer__")],
        outputs={"alpha": "split.alpha", "beta": "split.beta"},
    )


def _split_job(src: Path) -> Job:
    return Job(
        template_id="split_only",
        template_version=1,
        inputs={
            "split.image": Ref(
                node_hash="external", port="image", path=src, type=PortType.IMAGE_PNG
            )
        },
    )


def test_cache_hit_for_multi_output_node_with_non_port_filenames(
    tmp_path: Path, astrolab_home: Path
) -> None:
    """Regression: narrowband_extract-style nodes (multi-output, filenames
    don't match port names) must cache-hit cleanly on rerun. Before the
    manifest, the second call raised 'cached entry <h> missing output ha'."""
    src = tmp_path / "in.png"
    _make_test_png(src)

    template = _split_template()
    job = _split_job(src)

    first = run_job(template, job)
    second = run_job(template, job)

    assert first["alpha"].path == second["alpha"].path
    assert first["beta"].path == second["beta"].path
    assert second["alpha"].path.read_bytes() == b"alpha"
    assert second["beta"].path.read_bytes() == b"beta"
    # Confirm the cache really did serve a hit; if it re-ran, the entry dir
    # would have been wiped and rewritten with new inode/mtime.
    assert (
        first["alpha"].path.stat().st_mtime_ns
        == second["alpha"].path.stat().st_mtime_ns
    )


def test_cache_hit_falls_back_when_manifest_missing(
    tmp_path: Path, astrolab_home: Path
) -> None:
    """Legacy entries committed before the manifest only have `_done`. The
    runtime must still hand back the convention-based file (eg downscale's
    `image.png`) so users don't have to nuke their cache after upgrading."""
    src = tmp_path / "in.png"
    _make_test_png(src)
    template = _build_template()
    job = _job_with_input(src)

    # Populate the cache, then strip the manifest to fake a legacy entry.
    first = run_job(template, job)
    entry_dir = first["image"].path.parent
    manifest = entry_dir / "_outputs.json"
    assert manifest.exists()
    manifest.unlink()

    second = run_job(template, job)
    assert second["image"].path == first["image"].path
    assert second["image"].path.exists()


# ---- template upgrade cache survival -----------------------------------------
#
# Issue #61: bumping a template version while keeping the upstream node
# identities (id, version, params, edges) bit-identical must leave those
# upstream cache entries reusable. Adding `auto_bp_shift` downstream of
# `crop` is a real-world instance — the user's stacked output (hours of
# compute) must not get invalidated by the template bump alone.


def test_template_version_bump_preserves_upstream_cache(
    tmp_path: Path, astrolab_home: Path
) -> None:
    src = tmp_path / "in.png"
    _make_test_png(src, (2000, 1000))

    v1 = Template(
        id="upgrade_demo",
        version=1,
        nodes=[
            NodeSpec(id="ds", kind="downscale", params={"target_size_px": 512}),
        ],
        outputs={"image": "ds.image"},
    )
    # v2 keeps `ds` bit-identical but adds a second downscale downstream and
    # bumps the template version. This mirrors the auto_bp_shift insertion
    # we plan to ship: a new node slotted in below an unchanged upstream chain.
    v2 = Template(
        id="upgrade_demo",
        version=2,
        nodes=[
            NodeSpec(id="ds", kind="downscale", params={"target_size_px": 512}),
            NodeSpec(
                id="ds2",
                kind="downscale",
                params={"target_size_px": 256},
                inputs={"image": "ds.image"},
            ),
        ],
        outputs={"image": "ds2.image"},
    )

    job_v1 = Job(
        template_id="upgrade_demo",
        template_version=1,
        inputs={
            "ds.image": Ref(
                node_hash="ext", port="image", path=src, type=PortType.IMAGE_PNG
            )
        },
    )
    job_v2 = job_v1.model_copy(update={"template_version": 2})

    # Prime the cache on v1: `ds` runs for real.
    events_v1: list[dict] = []
    run_job(v1, job_v1, events=events_v1.append)
    ds_hash_v1 = next(
        e["hash"] for e in events_v1 if e["type"] == "node_started" and e["node_id"] == "ds"
    )

    # Run v2: `ds` must cache-hit (same id/version/params/inputs), `ds2`
    # must run for real (new node).
    events_v2: list[dict] = []
    run_job(v2, job_v2, events=events_v2.append)

    ds_started = next(
        e for e in events_v2 if e["type"] == "node_started" and e["node_id"] == "ds"
    )
    assert ds_started["hash"] == ds_hash_v1, (
        "template_version must not enter the per-node hash; "
        "ds re-hashed across the template bump"
    )

    cached_events = {e["node_id"] for e in events_v2 if e["type"] == "node_cached"}
    assert "ds" in cached_events, "upstream node should have cache-hit on the bumped template"
    assert "ds2" not in cached_events, "newly inserted node should not have cache-hit"
