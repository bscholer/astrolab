"""color_balance node: SCNR green emits the right Siril command and tolerates
the Siril 1.4 shutdown segfault when the output is on disk."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from astropy.io import fits

import nodes.basic  # noqa: F401  registers
from nodes.basic.color_balance import ColorBalanceNode, ColorBalanceParams
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


def _write_fits(path: Path, data: np.ndarray) -> Path:
    fits.PrimaryHDU(data.astype(np.float32)).writeto(path, overwrite=True)
    return path


def test_run_emits_rmgreen_with_amount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = np.full((16, 16, 3), 0.5, dtype=np.float32)
    src = _write_fits(tmp_path / "in.fit", data)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def fake(cmds, wd):
        (out_dir / "image.fit").write_bytes(b"FAKE")

    fake_rt = FakeRuntime(on_run=fake)
    monkeypatch.setattr(
        "nodes.basic.color_balance.SirilRuntime", lambda *a, **k: fake_rt
    )

    refs = ColorBalanceNode().run(
        inputs={
            "image": Ref(node_hash="ext", port="image", path=src,
                         type=PortType.IMAGE_FITS),
        },
        params=ColorBalanceParams(amount=0.85),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )

    rmgreen_cmd = next(
        c for c in fake_rt.calls[0]["commands"] if c.startswith("rmgreen")
    )
    # type 0 = average-neutral protection; amount in [0, 1].
    assert rmgreen_cmd == "rmgreen 0 0.85"
    assert refs["image"].path.exists()


def test_run_passthrough_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """enabled=False must skip Siril entirely and copy the input through."""
    data = np.array([[0.2, 0.3, 0.4]], dtype=np.float32)
    src = _write_fits(tmp_path / "in.fit", data)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    fake_rt = FakeRuntime()
    monkeypatch.setattr(
        "nodes.basic.color_balance.SirilRuntime", lambda *a, **k: fake_rt
    )
    refs = ColorBalanceNode().run(
        inputs={
            "image": Ref(node_hash="ext", port="image", path=src,
                         type=PortType.IMAGE_FITS),
        },
        params=ColorBalanceParams(enabled=False),
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    assert fake_rt.calls == []  # Siril never ran
    assert refs["image"].path.exists()
    with fits.open(refs["image"].path) as hdul:
        out_data = np.asarray(hdul[0].data)
    np.testing.assert_array_equal(out_data, data)


def test_run_raises_when_siril_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = np.full((16, 16, 3), 0.5, dtype=np.float32)
    src = _write_fits(tmp_path / "in.fit", data)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    fake_rt = FakeRuntime(returncode=3)
    monkeypatch.setattr(
        "nodes.basic.color_balance.SirilRuntime", lambda *a, **k: fake_rt
    )

    with pytest.raises(RuntimeError, match="exited 3"):
        ColorBalanceNode().run(
            inputs={
                "image": Ref(node_hash="ext", port="image", path=src,
                             type=PortType.IMAGE_FITS),
            },
            params=ColorBalanceParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )


def test_rc_minus11_output_present_warns_and_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """rc=-11 (Siril 1.4 shutdown segfault) + output written -> success with warning."""
    data = np.full((16, 16, 3), 0.5, dtype=np.float32)
    src = _write_fits(tmp_path / "in.fit", data)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def fake(cmds, wd):
        (out_dir / "image.fit").write_bytes(b"FAKE")

    fake_rt = FakeRuntime(returncode=-11, on_run=fake)
    monkeypatch.setattr(
        "nodes.basic.color_balance.SirilRuntime", lambda *a, **k: fake_rt
    )

    with caplog.at_level(logging.WARNING, logger="nodes._seq_runner"):
        refs = ColorBalanceNode().run(
            inputs={
                "image": Ref(node_hash="ext", port="image", path=src,
                             type=PortType.IMAGE_FITS),
            },
            params=ColorBalanceParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )

    assert refs["image"].path.exists()
    assert any("segfault" in r.message for r in caplog.records)
