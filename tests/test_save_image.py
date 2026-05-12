"""save_image node: unit tests with stubbed SirilRuntime.

Covers the four success/failure combinations for returncode and PNG presence,
including Siril 1.4 shutdown-segfault tolerance.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

import nodes.basic  # noqa: F401
from nodes.basic.save_image import SaveImageNode, SaveImageParams
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
        self.calls.append({"commands": list(commands), "working_dir": working_dir})
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


def _make_input(tmp_path: Path) -> Ref:
    src_fits = tmp_path / "source.fit"
    src_fits.write_bytes(b"FAKE FITS")
    return Ref(node_hash="ext", port="image", path=src_fits, type=PortType.IMAGE_FITS)


def test_rc0_png_present_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """rc=0 + PNG written -> success, returns IMAGE_PNG ref."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def fake(cmds, wd):
        (out_dir / "image.png").write_bytes(b"PNG_DATA")

    fake_rt = FakeRuntime(returncode=0, on_run=fake)
    monkeypatch.setattr("nodes.basic.save_image.SirilRuntime", lambda *a, **k: fake_rt)

    refs = SaveImageNode().run(
        inputs={"image": _make_input(tmp_path)},
        params=SaveImageParams(),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    assert refs["image"].type is PortType.IMAGE_PNG
    assert refs["image"].path == out_dir / "image.png"
    assert refs["image"].path.exists()


def test_rc_minus11_png_present_warns_and_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """rc=-11 (Siril 1.4 shutdown segfault) + PNG present -> success with warning."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def fake(cmds, wd):
        (out_dir / "image.png").write_bytes(b"PNG_DATA")

    fake_rt = FakeRuntime(returncode=-11, on_run=fake)
    monkeypatch.setattr("nodes.basic.save_image.SirilRuntime", lambda *a, **k: fake_rt)

    with caplog.at_level(logging.WARNING, logger="nodes._seq_runner"):
        refs = SaveImageNode().run(
            inputs={"image": _make_input(tmp_path)},
            params=SaveImageParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )

    assert refs["image"].type is PortType.IMAGE_PNG
    assert refs["image"].path.exists()
    assert any("segfault" in r.message for r in caplog.records)
    assert any("-11" in r.message for r in caplog.records)


def test_rc_minus11_png_missing_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rc=-11 + PNG not written -> failure (real crash, not just shutdown segfault)."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    fake_rt = FakeRuntime(returncode=-11)
    monkeypatch.setattr("nodes.basic.save_image.SirilRuntime", lambda *a, **k: fake_rt)

    with pytest.raises(RuntimeError, match="missing or empty"):
        SaveImageNode().run(
            inputs={"image": _make_input(tmp_path)},
            params=SaveImageParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )


def test_rc0_png_missing_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rc=0 but PNG absent -> failure."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    fake_rt = FakeRuntime(returncode=0)
    monkeypatch.setattr("nodes.basic.save_image.SirilRuntime", lambda *a, **k: fake_rt)

    with pytest.raises(RuntimeError, match="missing or empty"):
        SaveImageNode().run(
            inputs={"image": _make_input(tmp_path)},
            params=SaveImageParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )
