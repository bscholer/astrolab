"""seq_stack node: unit tests with stubbed SirilRuntime."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

import nodes.basic  # noqa: F401
from nodes.basic.seq_stack import SeqStackNode, SeqStackParams
from server.models import Ref, RunContext
from server.ports import PortType
from server.siril import SirilBinary, SirilResult


class FakeRuntime:
    def __init__(self, *, returncode: int = 0, on_run: Any | None = None) -> None:
        self.binary = SirilBinary(path=Path("/fake/siril"), source="env")
        self.returncode = returncode
        self.calls: list[dict[str, Any]] = []
        self._on_run = on_run

    def run(self, commands, *, working_dir=None, on_log=None, timeout=None,
            require_version="1.4.0", cancel=None) -> SirilResult:
        self.calls.append(
            {"commands": list(commands), "working_dir": working_dir, "require": require_version}
        )
        if self._on_run is not None:
            self._on_run(commands, working_dir)
        return SirilResult(
            returncode=self.returncode, stdout="ok", stderr="", ssf="\n".join(commands),
        )


def _ctx(tmp_path: Path) -> RunContext:
    return RunContext(
        tmpdir=tmp_path / "tmp",
        progress=lambda f, m: None,
        log=logging.getLogger("test"),
    )


def test_stack_default_rej_writes_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "r_pp_light.fit").write_bytes(b"FAKE")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def fake(cmds, wd):
        (out_dir / "image.fit").write_bytes(b"STACKED")

    fake_rt = FakeRuntime(on_run=fake)
    monkeypatch.setattr("nodes.basic.seq_stack.SirilRuntime", lambda *a, **k: fake_rt)
    refs = SeqStackNode().run(
        inputs={
            "sequence": Ref(node_hash="ext", port="sequence", path=seq_in,
                            type=PortType.SEQUENCE_FITS),
        },
        params=SeqStackParams(),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    img = refs["image"]
    assert img.type is PortType.IMAGE_FITS
    assert img.path == out_dir / "image.fit"
    assert img.path.exists()

    stack_cmd = next(c for c in fake_rt.calls[0]["commands"] if c.startswith("stack "))
    assert stack_cmd.startswith("stack r_pp_light rej w 3.0 3.0 ")
    assert "-norm=addscale" in stack_cmd
    assert "-output_norm" in stack_cmd
    assert "-32bits" not in stack_cmd  # 1.4 default
    assert f"-out={out_dir.resolve() / 'image.fit'}" in stack_cmd


def test_stack_mean_no_rejection_args(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "r_pp_light.fit").write_bytes(b"FAKE")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def fake(cmds, wd):
        (out_dir / "image.fit").write_bytes(b"")

    fake_rt = FakeRuntime(on_run=fake)
    monkeypatch.setattr("nodes.basic.seq_stack.SirilRuntime", lambda *a, **k: fake_rt)
    SeqStackNode().run(
        inputs={
            "sequence": Ref(node_hash="ext", port="sequence", path=seq_in,
                            type=PortType.SEQUENCE_FITS),
        },
        params=SeqStackParams(method="mean"),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    stack_cmd = next(c for c in fake_rt.calls[0]["commands"] if c.startswith("stack "))
    assert "stack r_pp_light mean" in stack_cmd
    assert " w " not in stack_cmd  # no rejection type
    assert "3.0 3.0" not in stack_cmd


def test_stack_raises_when_image_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "r_pp_light.fit").write_bytes(b"")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    fake_rt = FakeRuntime()
    monkeypatch.setattr("nodes.basic.seq_stack.SirilRuntime", lambda *a, **k: fake_rt)
    with pytest.raises(RuntimeError, match="missing"):
        SeqStackNode().run(
            inputs={
                "sequence": Ref(node_hash="ext", port="sequence", path=seq_in,
                                type=PortType.SEQUENCE_FITS),
            },
            params=SeqStackParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )


def test_stack_raises_when_siril_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "r_pp_light.fit").write_bytes(b"")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    fake_rt = FakeRuntime(returncode=4)
    monkeypatch.setattr("nodes.basic.seq_stack.SirilRuntime", lambda *a, **k: fake_rt)
    with pytest.raises(RuntimeError, match="exited 4"):
        SeqStackNode().run(
            inputs={
                "sequence": Ref(node_hash="ext", port="sequence", path=seq_in,
                                type=PortType.SEQUENCE_FITS),
            },
            params=SeqStackParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )
