"""seq_register node: unit tests with stubbed SirilRuntime."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

import nodes.basic  # noqa: F401
from nodes.basic.seq_register import SeqRegisterNode, SeqRegisterParams
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


def _make_seq_dir(root: Path, basename: str, n_frames: int = 3) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for i in range(1, n_frames + 1):
        (root / f"{basename}_{i:05d}.fit").write_bytes(b"FAKE")
    return root


def test_register_fitseq_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "pp_light.fit").write_bytes(b"FAKE")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def fake(cmds, wd):
        (out_dir / "sequence" / "r_pp_light.fit").write_bytes(b"FAKE")

    fake_rt = FakeRuntime(on_run=fake)
    monkeypatch.setattr("nodes.basic.seq_register.SirilRuntime", lambda *a, **k: fake_rt)

    refs = SeqRegisterNode().run(
        inputs={
            "sequence": Ref(node_hash="ext", port="sequence", path=seq_in,
                            type=PortType.SEQUENCE_FITS),
        },
        params=SeqRegisterParams(),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    assert refs["sequence"].path == out_dir / "sequence"
    assert (refs["sequence"].path / "r_pp_light.fit").exists()
    cmds = fake_rt.calls[0]["commands"]
    reg = next(c for c in cmds if c.startswith("register "))
    assert reg.startswith("register pp_light ")
    assert "-2pass" in reg
    assert "-transf=homography" in reg
    assert "-minpairs=10" in reg
    apply_cmd = next(c for c in cmds if c.startswith("seqapplyreg "))
    assert apply_cmd.startswith("seqapplyreg pp_light")


def test_register_per_frame_no_count_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Register can drop frames it cannot align; we don't enforce input==output count."""
    seq_in = _make_seq_dir(tmp_path / "in", "pp_light", n_frames=5)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def fake(cmds, wd):
        # Pretend siril dropped 1 frame for low star count.
        seq_dir = out_dir / "sequence"
        for i in (1, 2, 3, 4):  # 4 of 5 inputs make it through
            (seq_dir / f"r_pp_light_{i:05d}.fit").write_bytes(b"")

    fake_rt = FakeRuntime(on_run=fake)
    monkeypatch.setattr("nodes.basic.seq_register.SirilRuntime", lambda *a, **k: fake_rt)
    refs = SeqRegisterNode().run(
        inputs={
            "sequence": Ref(node_hash="ext", port="sequence", path=seq_in,
                            type=PortType.SEQUENCE_FITS),
        },
        params=SeqRegisterParams(fitseq=False),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    frames = sorted((refs["sequence"].path).glob("r_pp_light_*.fit"))
    assert len(frames) == 4


def test_register_param_options_propagate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "pp_light.fit").write_bytes(b"FAKE")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def fake(cmds, wd):
        (out_dir / "sequence" / "r_pp_light.fit").write_bytes(b"")

    fake_rt = FakeRuntime(on_run=fake)
    monkeypatch.setattr("nodes.basic.seq_register.SirilRuntime", lambda *a, **k: fake_rt)
    SeqRegisterNode().run(
        inputs={
            "sequence": Ref(node_hash="ext", port="sequence", path=seq_in,
                            type=PortType.SEQUENCE_FITS),
        },
        params=SeqRegisterParams(two_pass=False, transform="shift", min_pairs=20),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    reg = next(c for c in fake_rt.calls[0]["commands"] if c.startswith("register "))
    assert "-2pass" not in reg
    assert "-transf=shift" in reg
    assert "-minpairs=20" in reg


def test_register_filter_options_propagate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "pp_light.fit").write_bytes(b"")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def fake(cmds, wd):
        (out_dir / "sequence" / "r_pp_light.fit").write_bytes(b"")

    fake_rt = FakeRuntime(on_run=fake)
    monkeypatch.setattr("nodes.basic.seq_register.SirilRuntime", lambda *a, **k: fake_rt)
    SeqRegisterNode().run(
        inputs={
            "sequence": Ref(node_hash="ext", port="sequence", path=seq_in,
                            type=PortType.SEQUENCE_FITS),
        },
        params=SeqRegisterParams(filter_fwhm=0.2, filter_round=0.1),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    apply_cmd = next(c for c in fake_rt.calls[0]["commands"] if c.startswith("seqapplyreg "))
    assert "-filter-fwhm=0.2" in apply_cmd
    assert "-filter-round=0.1" in apply_cmd


def test_register_raises_when_siril_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "pp_light.fit").write_bytes(b"")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    fake_rt = FakeRuntime(returncode=3)
    monkeypatch.setattr("nodes.basic.seq_register.SirilRuntime", lambda *a, **k: fake_rt)
    with pytest.raises(RuntimeError, match="exited 3"):
        SeqRegisterNode().run(
            inputs={
                "sequence": Ref(node_hash="ext", port="sequence", path=seq_in,
                                type=PortType.SEQUENCE_FITS),
            },
            params=SeqRegisterParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )
