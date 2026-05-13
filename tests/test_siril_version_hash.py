"""Tests for Siril-version mixing into node cache keys.

Verifies:
- Siril-using nodes get different hashes when the Siril version changes.
- Hash is stable when the version string is unchanged.
- Non-Siril nodes are unaffected by the version string.
- get_siril_version() returns '' gracefully when Siril is absent.
- run_job passes the version into the hash for uses_siril nodes.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from pydantic import BaseModel

import nodes.basic  # noqa: F401  ensures all nodes are registered
from nodes.base import Node
from server.canonical import node_hash
from server.models import Job, NodeSpec, Ref, RunContext, Template
from server.ports import PortType
from server.registry import register
from server.runtime import run_job
from server.siril import SirilNotFound, get_siril_version

# ---------------------------------------------------------------------------
# Unit tests: uses_siril flag on node classes
# ---------------------------------------------------------------------------


def test_siril_nodes_are_flagged() -> None:
    """All nodes that instantiate SirilRuntime must have uses_siril=True."""
    from nodes.basic.calibrate import CalibrateNode
    from nodes.basic.convert_lights import ConvertLightsNode
    from nodes.basic.narrowband_compose import NarrowbandComposeNode
    from nodes.basic.narrowband_extract import NarrowbandExtractNode
    from nodes.basic.save_image import SaveImageNode
    from nodes.basic.seq_bg_extract import SeqBgExtractNode
    from nodes.basic.seq_register import SeqRegisterNode
    from nodes.basic.seq_resample import SeqResampleNode
    from nodes.basic.seq_stack import SeqStackNode
    from nodes.basic.starnet_replace import StarnetReplaceNode
    from nodes.basic.stretch import StretchNode

    siril_nodes = [
        CalibrateNode,
        ConvertLightsNode,
        NarrowbandComposeNode,
        NarrowbandExtractNode,
        SaveImageNode,
        SeqBgExtractNode,
        SeqRegisterNode,
        SeqResampleNode,
        SeqStackNode,
        StarnetReplaceNode,
        StretchNode,
    ]
    for cls in siril_nodes:
        assert cls.uses_siril is True, f"{cls.__name__}.uses_siril should be True"


def test_non_siril_nodes_are_not_flagged() -> None:
    """Nodes that never touch Siril must keep uses_siril=False."""
    from nodes.basic.crop import CropNode
    from nodes.basic.downscale import DownscaleNode
    from nodes.basic.starnet_extract import StarnetExtractNode
    from nodes.basic.starnet_recombine import StarnetRecombineNode

    non_siril_nodes = [
        CropNode,
        DownscaleNode,
        StarnetExtractNode,
        StarnetRecombineNode,
    ]
    for cls in non_siril_nodes:
        assert cls.uses_siril is False, f"{cls.__name__}.uses_siril should be False"


# ---------------------------------------------------------------------------
# Unit tests: get_siril_version() contract
# ---------------------------------------------------------------------------


def test_get_siril_version_returns_empty_string_when_not_found() -> None:
    """On a box without Siril (macOS dev), get_siril_version must return '' and
    not raise. The version probe resets the module-level cache so the mock is
    clean."""
    import server.siril as siril_mod

    # Reset cached state so the patch actually runs find_siril.
    siril_mod._siril_version_resolved = False
    siril_mod._cached_siril_version = None

    with patch.object(siril_mod, "find_siril", side_effect=SirilNotFound("no siril")):
        version = get_siril_version()

    assert version == ""


def test_get_siril_version_returns_version_string_when_found() -> None:
    import server.siril as siril_mod
    from server.siril import SirilBinary

    siril_mod._siril_version_resolved = False
    siril_mod._cached_siril_version = None

    fake_binary = SirilBinary(
        path=Path("/fake/siril"), version=(1, 4, 3), source="appimage"
    )
    with patch.object(siril_mod, "find_siril", return_value=fake_binary):
        version = get_siril_version()

    assert version == "1.4.3"


def test_get_siril_version_is_cached_after_first_call() -> None:
    """Second call must return the same value without calling find_siril again."""
    import server.siril as siril_mod
    from server.siril import SirilBinary

    siril_mod._siril_version_resolved = False
    siril_mod._cached_siril_version = None

    fake_binary = SirilBinary(
        path=Path("/fake/siril"), version=(1, 4, 0), source="system"
    )
    with patch.object(siril_mod, "find_siril", return_value=fake_binary) as mock_find:
        v1 = get_siril_version()
        v2 = get_siril_version()

    assert v1 == v2 == "1.4.0"
    mock_find.assert_called_once()  # resolved exactly once


# ---------------------------------------------------------------------------
# Unit tests: node_hash changes with siril_version extra key
# ---------------------------------------------------------------------------


class _FakeRef:
    def __init__(self, node_hash: str, port: str = "out", path: str = "/x") -> None:
        self.node_hash = node_hash
        self.port = port
        self.path = path


class _P(BaseModel):
    x: int = 1


def test_siril_version_change_changes_hash() -> None:
    """A node hash that includes siril_version must differ when the version bumps."""
    inputs = {"seq": _FakeRef("abc")}
    p = _P()
    h1 = node_hash(
        node_id="seq_stack",
        node_version=1,
        inputs=inputs,  # type: ignore[arg-type]
        params=p,
        extra_keys={"siril_version": "1.4.0"},
    )
    h2 = node_hash(
        node_id="seq_stack",
        node_version=1,
        inputs=inputs,  # type: ignore[arg-type]
        params=p,
        extra_keys={"siril_version": "1.4.1"},
    )
    assert h1 != h2


def test_siril_version_stable_when_unchanged() -> None:
    inputs = {"seq": _FakeRef("abc")}
    p = _P()
    h1 = node_hash(
        node_id="seq_stack",
        node_version=1,
        inputs=inputs,  # type: ignore[arg-type]
        params=p,
        extra_keys={"siril_version": "1.4.0"},
    )
    h2 = node_hash(
        node_id="seq_stack",
        node_version=1,
        inputs=inputs,  # type: ignore[arg-type]
        params=p,
        extra_keys={"siril_version": "1.4.0"},
    )
    assert h1 == h2


def test_non_siril_node_hash_unaffected_by_siril_version() -> None:
    """downscale has uses_siril=False; its hash must not include the version."""
    inputs = {"image": _FakeRef("img")}
    p = _P()
    # No extra_keys -> what a non-Siril node gets
    h_no_extra = node_hash(
        node_id="downscale", node_version=1, inputs=inputs, params=p  # type: ignore[arg-type]
    )
    # If someone were to add the key it would change (confirming sensitivity),
    # but the runtime doesn't add it for non-Siril nodes.
    h_with_extra = node_hash(
        node_id="downscale",
        node_version=1,
        inputs=inputs,  # type: ignore[arg-type]
        params=p,
        extra_keys={"siril_version": "1.4.0"},
    )
    # Sanity: adding a key does matter.
    assert h_no_extra != h_with_extra
    # The runtime should produce h_no_extra for a non-Siril node regardless
    # of which Siril is installed. This is verified by the integration test below.


# ---------------------------------------------------------------------------
# Integration test: run_job mixes version for Siril nodes, not for others
# ---------------------------------------------------------------------------


class _TrivialParams(BaseModel):
    pass


@register("__test_siril_ver_node__")
class _TestSirilNode(Node[_TrivialParams]):
    """Minimal node that declares uses_siril=True and writes a trivial output."""

    id = "__test_siril_ver_node__"
    version = 1
    cost = "cheap"
    uses_siril = True
    inputs = {"image": PortType.IMAGE_PNG}
    outputs = {"image": PortType.IMAGE_PNG}
    params_schema = _TrivialParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: _TrivialParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out = Path(out_dir) / "image.png"  # type: ignore[arg-type]
        out.write_bytes(b"fake")
        return {"image": Ref(node_hash="", port="image", path=out, type=PortType.IMAGE_PNG)}


@register("__test_no_siril_node__")
class _TestNoSirilNode(Node[_TrivialParams]):
    """Minimal node with uses_siril=False (default)."""

    id = "__test_no_siril_node__"
    version = 1
    cost = "cheap"
    # uses_siril inherits False from base
    inputs = {"image": PortType.IMAGE_PNG}
    outputs = {"image": PortType.IMAGE_PNG}
    params_schema = _TrivialParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: _TrivialParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out = Path(out_dir) / "image.png"  # type: ignore[arg-type]
        out.write_bytes(b"fake")
        return {"image": Ref(node_hash="", port="image", path=out, type=PortType.IMAGE_PNG)}


def _png_src(tmp_path: Path) -> Path:
    from PIL import Image

    p = tmp_path / "src.png"
    Image.new("RGB", (4, 4), (0, 0, 0)).save(p)
    return p


def _one_node_template(kind: str) -> Template:
    return Template(
        id=f"test_{kind}",
        version=1,
        nodes=[NodeSpec(id="n", kind=kind)],
        outputs={"image": "n.image"},
    )


def _one_node_job(src: Path, kind: str) -> Job:
    return Job(
        template_id=f"test_{kind}",
        template_version=1,
        inputs={
            "n.image": Ref(
                node_hash="ext", port="image", path=src, type=PortType.IMAGE_PNG
            )
        },
    )


def test_run_job_siril_node_hash_changes_when_version_changes(
    tmp_path: Path, astrolab_home: Path
) -> None:
    """Simulate a Siril upgrade: first run at 1.4.0, second at 1.4.1. The
    uses_siril node must produce different cache hashes; the no-siril node
    must not change."""
    import server.siril as siril_mod

    src = _png_src(tmp_path)
    template_siril = _one_node_template("__test_siril_ver_node__")
    template_plain = _one_node_template("__test_no_siril_node__")
    job_siril = _one_node_job(src, "__test_siril_ver_node__")
    job_plain = _one_node_job(src, "__test_no_siril_node__")

    # Run with Siril 1.4.0
    siril_mod._siril_version_resolved = False
    siril_mod._cached_siril_version = None
    with patch.object(
        siril_mod,
        "find_siril",
        return_value=siril_mod.SirilBinary(
            path=Path("/fake/siril"), version=(1, 4, 0), source="appimage"
        ),
    ):
        out_siril_140 = run_job(template_siril, job_siril)
        out_plain_140 = run_job(template_plain, job_plain)

    hash_siril_140 = out_siril_140["image"].node_hash
    hash_plain_140 = out_plain_140["image"].node_hash

    # Simulate upgrade to 1.4.1
    siril_mod._siril_version_resolved = False
    siril_mod._cached_siril_version = None
    with patch.object(
        siril_mod,
        "find_siril",
        return_value=siril_mod.SirilBinary(
            path=Path("/fake/siril"), version=(1, 4, 1), source="appimage"
        ),
    ):
        out_siril_141 = run_job(template_siril, job_siril)
        out_plain_141 = run_job(template_plain, job_plain)

    hash_siril_141 = out_siril_141["image"].node_hash
    hash_plain_141 = out_plain_141["image"].node_hash

    # Siril node: version bump must invalidate the hash.
    assert hash_siril_140 != hash_siril_141

    # Non-Siril node: hash is unaffected.
    assert hash_plain_140 == hash_plain_141
