"""seq_bg_extract node: stubbed-runtime tests."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

import nodes.basic  # noqa: F401
from nodes.basic.seq_bg_extract import SeqBgExtractNode, SeqBgExtractParams
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
            require_version="1.4.0") -> SirilResult:
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


def test_bg_extract_default_emits_seqsubsky(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "pp_light.fit").write_bytes(b"FAKE")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def fake(cmds, wd):
        (out_dir / "sequence" / "bkg_pp_light.fit").write_bytes(b"FAKE")

    fake_rt = FakeRuntime(on_run=fake)
    monkeypatch.setattr("nodes.basic.seq_bg_extract.SirilRuntime", lambda *a, **k: fake_rt)
    refs = SeqBgExtractNode().run(
        inputs={
            "sequence": Ref(node_hash="ext", port="sequence", path=seq_in,
                            type=PortType.SEQUENCE_FITS),
        },
        params=SeqBgExtractParams(),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    cmd = next(c for c in fake_rt.calls[0]["commands"] if c.startswith("seqsubsky "))
    # Naztronomy defaults: degree 1, samples 10.
    assert cmd.startswith("seqsubsky pp_light 1")
    assert "-samples=10" in cmd
    assert (refs["sequence"].path / "bkg_pp_light.fit").exists()


def test_bg_extract_param_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "pp_light.fit").write_bytes(b"FAKE")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def fake(cmds, wd):
        (out_dir / "sequence" / "bkg_pp_light.fit").write_bytes(b"FAKE")

    fake_rt = FakeRuntime(on_run=fake)
    monkeypatch.setattr("nodes.basic.seq_bg_extract.SirilRuntime", lambda *a, **k: fake_rt)
    SeqBgExtractNode().run(
        inputs={
            "sequence": Ref(node_hash="ext", port="sequence", path=seq_in,
                            type=PortType.SEQUENCE_FITS),
        },
        params=SeqBgExtractParams(degree=2, samples=20),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    cmd = next(c for c in fake_rt.calls[0]["commands"] if c.startswith("seqsubsky "))
    assert "seqsubsky pp_light 2" in cmd
    assert "-samples=20" in cmd


def test_bg_extract_raises_when_siril_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "pp_light.fit").write_bytes(b"FAKE")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    fake_rt = FakeRuntime(returncode=2)
    monkeypatch.setattr("nodes.basic.seq_bg_extract.SirilRuntime", lambda *a, **k: fake_rt)
    with pytest.raises(RuntimeError, match="exited 2"):
        SeqBgExtractNode().run(
            inputs={
                "sequence": Ref(node_hash="ext", port="sequence", path=seq_in,
                                type=PortType.SEQUENCE_FITS),
            },
            params=SeqBgExtractParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )
