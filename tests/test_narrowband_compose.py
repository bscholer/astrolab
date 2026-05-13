"""narrowband_compose node: unit tests with a stubbed SirilRuntime.

Covers Phase 2 of the narrowband flow: PixelMath OIII normalization +
(optional synthetic green) + rgbcomp per palette. The Naztronomy formula
identifiers ($r_results_ha$, $r_results_oiii$, $normalized_r_results_oiii$)
must round-trip verbatim because the OIII normalization is bit-sensitive.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import nodes.basic  # noqa: F401  ensures the node registers
from nodes.basic.narrowband_compose import (
    PALETTE_CONFIG,
    NarrowbandComposeNode,
    NarrowbandComposeParams,
)
from server.models import Ref, RunContext
from server.ports import PortType
from server.siril import SirilBinary, SirilResult


class FakeRuntime:
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
            stdout="ok",
            stderr="",
            ssf="\n".join(commands),
        )


def _ctx(tmp_path: Path) -> RunContext:
    return RunContext(
        tmpdir=tmp_path / "tmp",
        progress=lambda f, m: None,
        log=logging.getLogger("test"),
        cancel=threading.Event(),
    )


def _make_inputs(root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    ha = root / "ha.fit"
    oiii = root / "oiii.fit"
    ha.write_bytes(b"HA")
    oiii.write_bytes(b"OIII")
    return ha, oiii


def _run_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    *,
    write_image: bool = True,
) -> FakeRuntime:
    ha, oiii = _make_inputs(tmp_path / "in")
    out_dir = tmp_path / f"out_{mode}"
    out_dir.mkdir()

    def fake(cmds, wd):
        if write_image:
            (out_dir / "image.fit").write_bytes(b"COMPOSED")

    fake_rt = FakeRuntime(on_run=fake)
    monkeypatch.setattr(
        "nodes.basic.narrowband_compose.SirilRuntime", lambda *a, **k: fake_rt
    )
    NarrowbandComposeNode().run(
        inputs={
            "ha": Ref(
                node_hash="ext-ha",
                port="ha",
                path=ha,
                type=PortType.IMAGE_FITS,
            ),
            "oiii": Ref(
                node_hash="ext-oiii",
                port="oiii",
                path=oiii,
                type=PortType.IMAGE_FITS,
            ),
        },
        params=NarrowbandComposeParams(mode=mode),  # type: ignore[arg-type]
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    return fake_rt


@pytest.mark.parametrize("mode", ["hoo", "ohh", "hho", "ooh", "hoh", "oho", "hso"])
def test_pixelmath_normalization_formula_exact_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """OIII->Ha normalization must match Naztronomy verbatim across all modes."""
    rt = _run_node(tmp_path, monkeypatch, mode)
    cmds = rt.calls[0]["commands"]
    expected = (
        "pm $r_results_oiii$*mad($r_results_ha$)/mad($r_results_oiii$)"
        "-mad($r_results_ha$)/mad($r_results_oiii$)*median($r_results_oiii$)"
        "+median($r_results_ha$)"
    )
    assert any(c == expected for c in cmds), (
        "expected exact PixelMath line, got command list:\n" + "\n".join(cmds)
    )


@pytest.mark.parametrize("mode", ["hoo", "ohh", "hho", "ooh", "hoh", "oho", "hso"])
def test_rgbcomp_channel_order_matches_palette_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    rt = _run_node(tmp_path, monkeypatch, mode)
    cmds = rt.calls[0]["commands"]
    rgbcomp = next(c for c in cmds if c.startswith("rgbcomp "))
    body = rgbcomp[len("rgbcomp "):]
    out_token = body.split(" -out=")[-1]
    rgb_part = body[: -(len(" -out=") + len(out_token))]
    r_file, g_file, b_file = rgb_part.split(" ", 2)

    sources = {
        "ha": "r_results_ha",
        "oiii": "normalized_r_results_oiii",
        "synthetic": "synthetic_green",
    }
    config = PALETTE_CONFIG[mode]
    channels = config["channels"]  # type: ignore[index]
    assert r_file == sources[channels["R"]]  # type: ignore[index]
    assert g_file == sources[channels["G"]]  # type: ignore[index]
    assert b_file == sources[channels["B"]]  # type: ignore[index]


def test_hso_emits_synthetic_green_pixelmath_and_uses_it_in_g(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = _run_node(tmp_path, monkeypatch, "hso")
    cmds = rt.calls[0]["commands"]

    expected_synth = "pm ($r_results_ha$ * 0.7) + ($normalized_r_results_oiii$ * 0.3)"
    assert any(c == expected_synth for c in cmds), (
        "expected synthetic-green PixelMath line for hso, got:\n" + "\n".join(cmds)
    )
    save_idx = cmds.index("save synthetic_green")
    synth_idx = cmds.index(expected_synth)
    assert synth_idx < save_idx

    rgbcomp = next(c for c in cmds if c.startswith("rgbcomp "))
    _, g_file, _ = rgbcomp[len("rgbcomp "):].split(" -out=")[0].split(" ", 2)
    assert g_file == "synthetic_green"


def test_non_hso_modes_do_not_emit_synthetic_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for mode in ("hoo", "ohh", "hho", "ooh", "hoh", "oho"):
        rt = _run_node(tmp_path, monkeypatch, mode)
        cmds = rt.calls[0]["commands"]
        assert not any(c == "save synthetic_green" for c in cmds), (
            f"mode {mode} should not produce synthetic_green, got:\n"
            + "\n".join(cmds)
        )


def test_missing_ha_raises_before_siril(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    oiii = tmp_path / "oiii.fit"
    oiii.write_bytes(b"OIII")

    fake_rt = FakeRuntime()
    monkeypatch.setattr(
        "nodes.basic.narrowband_compose.SirilRuntime", lambda *a, **k: fake_rt
    )
    with pytest.raises(RuntimeError, match="ha input does not exist"):
        NarrowbandComposeNode().run(
            inputs={
                "ha": Ref(
                    node_hash="ext",
                    port="ha",
                    path=tmp_path / "missing-ha.fit",
                    type=PortType.IMAGE_FITS,
                ),
                "oiii": Ref(
                    node_hash="ext",
                    port="oiii",
                    path=oiii,
                    type=PortType.IMAGE_FITS,
                ),
            },
            params=NarrowbandComposeParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )
    assert fake_rt.calls == []


def test_missing_oiii_raises_before_siril(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    ha = tmp_path / "ha.fit"
    ha.write_bytes(b"HA")

    fake_rt = FakeRuntime()
    monkeypatch.setattr(
        "nodes.basic.narrowband_compose.SirilRuntime", lambda *a, **k: fake_rt
    )
    with pytest.raises(RuntimeError, match="oiii input does not exist"):
        NarrowbandComposeNode().run(
            inputs={
                "ha": Ref(
                    node_hash="ext",
                    port="ha",
                    path=ha,
                    type=PortType.IMAGE_FITS,
                ),
                "oiii": Ref(
                    node_hash="ext",
                    port="oiii",
                    path=tmp_path / "missing-oiii.fit",
                    type=PortType.IMAGE_FITS,
                ),
            },
            params=NarrowbandComposeParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )
    assert fake_rt.calls == []


def test_unknown_mode_rejected_by_pydantic() -> None:
    with pytest.raises(ValidationError):
        NarrowbandComposeParams(mode="rgb")  # type: ignore[arg-type]


def test_raises_when_siril_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ha = tmp_path / "ha.fit"
    oiii = tmp_path / "oiii.fit"
    ha.write_bytes(b"HA")
    oiii.write_bytes(b"OIII")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    fake_rt = FakeRuntime(returncode=5)
    monkeypatch.setattr(
        "nodes.basic.narrowband_compose.SirilRuntime", lambda *a, **k: fake_rt
    )
    with pytest.raises(RuntimeError, match="exited 5"):
        NarrowbandComposeNode().run(
            inputs={
                "ha": Ref(
                    node_hash="ext",
                    port="ha",
                    path=ha,
                    type=PortType.IMAGE_FITS,
                ),
                "oiii": Ref(
                    node_hash="ext",
                    port="oiii",
                    path=oiii,
                    type=PortType.IMAGE_FITS,
                ),
            },
            params=NarrowbandComposeParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )


def test_raises_when_image_missing_after_zero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ha = tmp_path / "ha.fit"
    oiii = tmp_path / "oiii.fit"
    ha.write_bytes(b"HA")
    oiii.write_bytes(b"OIII")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    fake_rt = FakeRuntime()  # no on_run, image.fit never lands
    monkeypatch.setattr(
        "nodes.basic.narrowband_compose.SirilRuntime", lambda *a, **k: fake_rt
    )
    with pytest.raises(RuntimeError, match="missing"):
        NarrowbandComposeNode().run(
            inputs={
                "ha": Ref(
                    node_hash="ext",
                    port="ha",
                    path=ha,
                    type=PortType.IMAGE_FITS,
                ),
                "oiii": Ref(
                    node_hash="ext",
                    port="oiii",
                    path=oiii,
                    type=PortType.IMAGE_FITS,
                ),
            },
            params=NarrowbandComposeParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )
