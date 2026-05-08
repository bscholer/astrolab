"""calibrate node: unit tests with a stubbed SirilRuntime.

Mirrors test_convert_lights: real Siril is Linux-only, so we patch the runtime
to fake the calibrate step's outputs and assert command shape + Ref shape.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

import nodes.basic  # noqa: F401  registers calibrate
from nodes.basic.calibrate import CalibrateNode, CalibrateParams
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


def _make_seq_dir(root: Path, basename: str = "light", n_frames: int = 3) -> Path:
    """Build a per-frame sequence dir like convert_lights produces."""
    root.mkdir(parents=True, exist_ok=True)
    for i in range(1, n_frames + 1):
        (root / f"{basename}_{i:05d}.fit").write_bytes(b"FAKE")
    (root / f"{basename}_conversion.txt").write_text("# fake\n")
    return root


def _make_master(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MASTER")
    return path


def test_calibrate_dark_only_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "light.fit").write_bytes(b"FAKE FITSEQ")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    dark = _make_master(tmp_path / "masters" / "dark.fit")

    def fake(cmds, wd):
        seq_dir = out_dir / "sequence"
        # FITSEQ default: emit pp_light.fit
        (seq_dir / "pp_light.fit").write_bytes(b"FAKE")

    fake_rt = FakeRuntime(on_run=fake)
    monkeypatch.setattr("nodes.basic.calibrate.SirilRuntime", lambda *a, **k: fake_rt)

    refs = CalibrateNode().run(
        inputs={
            "sequence": Ref(node_hash="ext", port="sequence", path=seq_in, type=PortType.SEQUENCE_FITS),
            "dark": Ref(node_hash="ext", port="dark", path=dark, type=PortType.MASTER_FITS),
        },
        params=CalibrateParams(fitseq=True),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    seq_ref = refs["sequence"]
    assert seq_ref.type is PortType.SEQUENCE_FITS
    assert seq_ref.path == out_dir / "sequence"
    assert (seq_ref.path / "pp_light.fit").exists()

    cmds = fake_rt.calls[0]["commands"]
    cal = next(c for c in cmds if c.startswith("calibrate "))
    assert "calibrate light " in cal
    assert f"-dark={dark.resolve()}" in cal
    assert "-flat" not in cal
    assert "-bias" not in cal
    assert "-cc=dark" in cal  # cosmetic default on
    assert "-fitseq" in cal


def test_calibrate_passes_debayer_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """OSC pipelines must debayer at calibrate so register/stack don't run on
    Bayer pixels (sub-pixel registration would scramble the pattern)."""
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "light.fit").write_bytes(b"FAKE")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    dark = _make_master(tmp_path / "masters" / "dark.fit")

    def fake(cmds, wd):
        (out_dir / "sequence" / "pp_light.fit").write_bytes(b"FAKE")

    fake_rt = FakeRuntime(on_run=fake)
    monkeypatch.setattr("nodes.basic.calibrate.SirilRuntime", lambda *a, **k: fake_rt)
    CalibrateNode().run(
        inputs={
            "sequence": Ref(node_hash="ext", port="sequence", path=seq_in,
                            type=PortType.SEQUENCE_FITS),
            "dark": Ref(node_hash="ext", port="dark", path=dark, type=PortType.MASTER_FITS),
        },
        params=CalibrateParams(fitseq=True),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    cal = next(c for c in fake_rt.calls[0]["commands"] if c.startswith("calibrate "))
    assert "-debayer" in cal


def test_calibrate_debayer_off_when_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "light.fit").write_bytes(b"FAKE")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    dark = _make_master(tmp_path / "masters" / "dark.fit")

    def fake(cmds, wd):
        (out_dir / "sequence" / "pp_light.fit").write_bytes(b"FAKE")

    fake_rt = FakeRuntime(on_run=fake)
    monkeypatch.setattr("nodes.basic.calibrate.SirilRuntime", lambda *a, **k: fake_rt)
    CalibrateNode().run(
        inputs={
            "sequence": Ref(node_hash="ext", port="sequence", path=seq_in,
                            type=PortType.SEQUENCE_FITS),
            "dark": Ref(node_hash="ext", port="dark", path=dark, type=PortType.MASTER_FITS),
        },
        params=CalibrateParams(fitseq=True, debayer=False),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    cal = next(c for c in fake_rt.calls[0]["commands"] if c.startswith("calibrate "))
    assert "-debayer" not in cal


def test_calibrate_with_flat_and_bias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "light.fit").write_bytes(b"FAKE")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    dark = _make_master(tmp_path / "masters" / "dark.fit")
    flat = _make_master(tmp_path / "masters" / "flat.fit")
    bias = _make_master(tmp_path / "masters" / "bias.fit")

    def fake(cmds, wd):
        (out_dir / "sequence" / "pp_light.fit").write_bytes(b"FAKE")

    fake_rt = FakeRuntime(on_run=fake)
    monkeypatch.setattr("nodes.basic.calibrate.SirilRuntime", lambda *a, **k: fake_rt)

    CalibrateNode().run(
        inputs={
            "sequence": Ref(node_hash="ext", port="sequence", path=seq_in, type=PortType.SEQUENCE_FITS),
            "dark": Ref(node_hash="ext", port="dark", path=dark, type=PortType.MASTER_FITS),
            "flat": Ref(node_hash="ext", port="flat", path=flat, type=PortType.MASTER_FITS),
            "bias": Ref(node_hash="ext", port="bias", path=bias, type=PortType.MASTER_FITS),
        },
        params=CalibrateParams(fitseq=True),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    cal = next(c for c in fake_rt.calls[0]["commands"] if c.startswith("calibrate "))
    assert f"-flat={flat.resolve()}" in cal
    assert f"-bias={bias.resolve()}" in cal


def test_calibrate_per_frame_validates_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seq_in = _make_seq_dir(tmp_path / "in", "light", n_frames=3)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    dark = _make_master(tmp_path / "masters" / "dark.fit")

    def fake_short(cmds, wd):
        # Only 2 of 3 expected outputs land.
        seq_dir = out_dir / "sequence"
        (seq_dir / "pp_light_00001.fit").write_bytes(b"")
        (seq_dir / "pp_light_00002.fit").write_bytes(b"")

    fake_rt = FakeRuntime(on_run=fake_short)
    monkeypatch.setattr("nodes.basic.calibrate.SirilRuntime", lambda *a, **k: fake_rt)

    with pytest.raises(RuntimeError, match="expected 3"):
        CalibrateNode().run(
            inputs={
                "sequence": Ref(node_hash="ext", port="sequence", path=seq_in, type=PortType.SEQUENCE_FITS),
                "dark": Ref(node_hash="ext", port="dark", path=dark, type=PortType.MASTER_FITS),
            },
            params=CalibrateParams(fitseq=False),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )


def test_calibrate_raises_when_siril_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    (seq_in / "light.fit").write_bytes(b"")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    dark = _make_master(tmp_path / "masters" / "dark.fit")

    fake_rt = FakeRuntime(returncode=2)
    monkeypatch.setattr("nodes.basic.calibrate.SirilRuntime", lambda *a, **k: fake_rt)
    with pytest.raises(RuntimeError, match="exited 2"):
        CalibrateNode().run(
            inputs={
                "sequence": Ref(node_hash="ext", port="sequence", path=seq_in, type=PortType.SEQUENCE_FITS),
                "dark": Ref(node_hash="ext", port="dark", path=dark, type=PortType.MASTER_FITS),
            },
            params=CalibrateParams(fitseq=True),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )


def test_calibrate_missing_input_basename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Input dir has frames under a different basename.
    seq_in = _make_seq_dir(tmp_path / "in", "other", n_frames=2)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    dark = _make_master(tmp_path / "masters" / "dark.fit")

    fake_rt = FakeRuntime()
    monkeypatch.setattr("nodes.basic.calibrate.SirilRuntime", lambda *a, **k: fake_rt)
    with pytest.raises(RuntimeError, match="no input frames"):
        CalibrateNode().run(
            inputs={
                "sequence": Ref(node_hash="ext", port="sequence", path=seq_in, type=PortType.SEQUENCE_FITS),
                "dark": Ref(node_hash="ext", port="dark", path=dark, type=PortType.MASTER_FITS),
            },
            params=CalibrateParams(fitseq=False),  # forces per-frame match
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )
    assert fake_rt.calls == []


def test_calibrate_drops_input_symlinks_after_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cache entry should contain only the calibrate outputs, not the staged inputs."""
    seq_in = _make_seq_dir(tmp_path / "in", "light", n_frames=3)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    dark = _make_master(tmp_path / "masters" / "dark.fit")

    def fake(cmds, wd):
        seq_dir = out_dir / "sequence"
        for i in range(1, 4):
            (seq_dir / f"pp_light_{i:05d}.fit").write_bytes(b"")

    fake_rt = FakeRuntime(on_run=fake)
    monkeypatch.setattr("nodes.basic.calibrate.SirilRuntime", lambda *a, **k: fake_rt)
    CalibrateNode().run(
        inputs={
            "sequence": Ref(node_hash="ext", port="sequence", path=seq_in, type=PortType.SEQUENCE_FITS),
            "dark": Ref(node_hash="ext", port="dark", path=dark, type=PortType.MASTER_FITS),
        },
        params=CalibrateParams(fitseq=False),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    contents = sorted(p.name for p in (out_dir / "sequence").iterdir())
    assert all(name.startswith("pp_light_") for name in contents)
    assert not any(name.startswith("light_") for name in contents)
