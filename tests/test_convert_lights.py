"""convert_lights node: unit tests with a stubbed SirilRuntime.

The real Siril is not available on macOS dev (Spike 01), so these tests
inject a fake runtime that pretends to write the expected output files.
A live integration test runs on the Linux box from a smoke script.
"""

from __future__ import annotations

import sqlite3  # noqa: F401  (silences unused warning, Pyright follows imports oddly)
from pathlib import Path
from typing import Any

import pytest

import nodes.basic  # noqa: F401  registers convert_lights
from nodes.basic.convert_lights import ConvertLightsNode, ConvertLightsParams
from server.models import Ref, RunContext
from server.ports import PortType
from server.siril import SirilBinary, SirilResult


class FakeRuntime:
    """Stand-in for SirilRuntime that 'executes' the .ssf by faking outputs."""

    def __init__(self, *, returncode: int = 0, on_run: Any | None = None) -> None:
        self.binary = SirilBinary(path=Path("/fake/siril"), source="env")
        self.returncode = returncode
        self.calls: list[dict[str, Any]] = []
        self._on_run = on_run

    def run(
        self,
        commands,
        *,
        working_dir=None,
        on_log=None,
        timeout=None,
        require_version="1.4.0",
        cancel=None,
    ) -> SirilResult:
        self.calls.append(
            {"commands": list(commands), "working_dir": working_dir, "require": require_version}
        )
        if self._on_run is not None:
            self._on_run(commands, working_dir)
        return SirilResult(
            returncode=self.returncode,
            stdout="[fake] ok",
            stderr="",
            ssf="\n".join(commands),
        )


def _ctx(tmp_path: Path) -> RunContext:
    import logging

    return RunContext(
        tmpdir=tmp_path / "tmp",
        progress=lambda f, m: None,
        log=logging.getLogger("test"),
    )


def _make_session_dir(root: Path, n_frames: int = 3) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for i in range(n_frames):
        (root / f"M 33_30s60_Astro_2025102{i}-000000_24C.fits").write_bytes(b"FAKE FITS")
    # Drop in a Dwarf-built artifact and a non-FITS file; both must be skipped.
    (root / "stacked-16_M 33_30s60_Astro_x.fits").write_bytes(b"")
    (root / "shotsInfo.json").write_bytes(b"{}")
    return root


def test_convert_lights_invokes_siril_and_returns_seq_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _make_session_dir(tmp_path / "session")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def fake_convert(cmds, wd):
        # Default params have fitseq=True, so emit one FITSEQ container.
        seq_dir = out_dir / "sequence"
        seq_dir.mkdir(exist_ok=True)
        (seq_dir / "light.fit").write_text("# fake fitseq\n")

    fake = FakeRuntime(on_run=fake_convert)
    monkeypatch.setattr(
        "nodes.basic.convert_lights.SirilRuntime", lambda *a, **k: fake
    )

    node = ConvertLightsNode()
    refs = node.run(
        inputs={
            "lights": Ref(
                node_hash="ext",
                port="lights",
                path=src,
                type=PortType.SEQUENCE_FITS,
            )
        },
        params=ConvertLightsParams(),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )

    assert "sequence" in refs
    seq_ref = refs["sequence"]
    assert seq_ref.type is PortType.SEQUENCE_FITS
    assert seq_ref.path == out_dir / "sequence"
    # Default fitseq=True produces a single FITSEQ container.
    assert (seq_ref.path / "light.fit").exists()

    # Verify Siril command shape.
    assert len(fake.calls) == 1
    cmds = fake.calls[0]["commands"]
    assert any(c.startswith("cd ") for c in cmds)
    convert_cmd = next(c for c in cmds if c.startswith("convert "))
    assert " -fitseq" in convert_cmd
    assert "-debayer" not in convert_cmd
    assert convert_cmd.startswith("convert light ")

    # Symlinks staged: 3 .fits files, no stacked-* and no shotsInfo.
    staged = list((out_dir / "_inputs").iterdir())
    fits_links = [p for p in staged if p.suffix == ".fits"]
    assert len(fits_links) == 3
    assert not any("stacked-" in p.name for p in staged)
    assert not any("shotsInfo" in p.name for p in staged)


def test_convert_lights_debayer_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _make_session_dir(tmp_path / "session")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def fake_convert(cmds, wd):
        seq_dir = out_dir / "sequence"
        seq_dir.mkdir(exist_ok=True)
        (seq_dir / "light.fit").write_text("# fake\n")

    fake = FakeRuntime(on_run=fake_convert)
    monkeypatch.setattr(
        "nodes.basic.convert_lights.SirilRuntime", lambda *a, **k: fake
    )

    ConvertLightsNode().run(
        inputs={
            "lights": Ref(
                node_hash="ext",
                port="lights",
                path=src,
                type=PortType.SEQUENCE_FITS,
            )
        },
        params=ConvertLightsParams(debayer=True),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    convert_cmd = next(c for c in fake.calls[0]["commands"] if c.startswith("convert "))
    assert "-debayer" in convert_cmd


def test_convert_lights_raises_when_siril_returns_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _make_session_dir(tmp_path / "session")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    fake = FakeRuntime(returncode=2)
    monkeypatch.setattr(
        "nodes.basic.convert_lights.SirilRuntime", lambda *a, **k: fake
    )
    with pytest.raises(RuntimeError, match="exited 2"):
        ConvertLightsNode().run(
            inputs={
                "lights": Ref(
                    node_hash="ext",
                    port="lights",
                    path=src,
                    type=PortType.SEQUENCE_FITS,
                )
            },
            params=ConvertLightsParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )


def test_convert_lights_raises_when_fitseq_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _make_session_dir(tmp_path / "session")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    # Siril returns 0 but the FITSEQ container is missing.
    fake = FakeRuntime(on_run=lambda cmds, wd: None)
    monkeypatch.setattr(
        "nodes.basic.convert_lights.SirilRuntime", lambda *a, **k: fake
    )
    with pytest.raises(RuntimeError, match="missing"):
        ConvertLightsNode().run(
            inputs={
                "lights": Ref(
                    node_hash="ext",
                    port="lights",
                    path=src,
                    type=PortType.SEQUENCE_FITS,
                )
            },
            params=ConvertLightsParams(),  # fitseq=True default
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )


def test_convert_lights_non_fitseq_validates_frame_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _make_session_dir(tmp_path / "session", n_frames=3)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def fake_convert_short(cmds, wd):
        # Pretend Siril only emitted 2 frames out of 3 inputs.
        seq_dir = out_dir / "sequence"
        seq_dir.mkdir(exist_ok=True)
        (seq_dir / "light_00001.fit").write_text("")
        (seq_dir / "light_00002.fit").write_text("")

    fake = FakeRuntime(on_run=fake_convert_short)
    monkeypatch.setattr(
        "nodes.basic.convert_lights.SirilRuntime", lambda *a, **k: fake
    )
    with pytest.raises(RuntimeError, match="expected 3"):
        ConvertLightsNode().run(
            inputs={
                "lights": Ref(
                    node_hash="ext",
                    port="lights",
                    path=src,
                    type=PortType.SEQUENCE_FITS,
                )
            },
            params=ConvertLightsParams(fitseq=False),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )


def test_convert_lights_non_fitseq_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _make_session_dir(tmp_path / "session", n_frames=3)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def fake_convert_full(cmds, wd):
        seq_dir = out_dir / "sequence"
        seq_dir.mkdir(exist_ok=True)
        for i in range(1, 4):
            (seq_dir / f"light_{i:05d}.fit").write_text("")

    fake = FakeRuntime(on_run=fake_convert_full)
    monkeypatch.setattr(
        "nodes.basic.convert_lights.SirilRuntime", lambda *a, **k: fake
    )
    refs = ConvertLightsNode().run(
        inputs={
            "lights": Ref(
                node_hash="ext",
                port="lights",
                path=src,
                type=PortType.SEQUENCE_FITS,
            )
        },
        params=ConvertLightsParams(fitseq=False),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    assert refs["sequence"].path.is_dir()
    assert sum(1 for _ in refs["sequence"].path.glob("light_*.fit")) == 3
    # Convert command must NOT have included -fitseq.
    convert_cmd = next(c for c in fake.calls[0]["commands"] if c.startswith("convert "))
    assert "-fitseq" not in convert_cmd


def test_convert_lights_rejects_empty_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "empty"
    src.mkdir()
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    fake = FakeRuntime()
    monkeypatch.setattr(
        "nodes.basic.convert_lights.SirilRuntime", lambda *a, **k: fake
    )
    with pytest.raises(RuntimeError, match="no .fits"):
        ConvertLightsNode().run(
            inputs={
                "lights": Ref(
                    node_hash="ext",
                    port="lights",
                    path=src,
                    type=PortType.SEQUENCE_FITS,
                )
            },
            params=ConvertLightsParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )
    assert fake.calls == []
