"""narrowband_palette node: unit tests with stubbed SirilRuntime.

The node ports Naztronomy's HOO/SHO/HSO flow over to a single Siril SSF.
We don't run Siril here (Linux-only, expensive), so the tests inspect the
captured command list to confirm the SSF lines up with the proven script
and that the per-mode palette config wires R/G/B to the right intermediate
files.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import nodes.basic  # noqa: F401  ensures the node registers
from nodes.basic.narrowband_palette import (
    PALETTE_CONFIG,
    NarrowbandPaletteNode,
    NarrowbandPaletteParams,
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


def _make_seq_dir(root: Path, basename: str, n_frames: int = 3) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for i in range(1, n_frames + 1):
        (root / f"{basename}_{i:05d}.fit").write_bytes(b"FAKE")
    return root


def _run_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    *,
    write_image: bool = True,
) -> FakeRuntime:
    """Set up a tmp sequence + out_dir, run the node with `mode`, return the FakeRuntime."""
    seq_in = _make_seq_dir(tmp_path / "in", "pp_light")
    out_dir = tmp_path / f"out_{mode}"
    out_dir.mkdir()

    def fake(cmds, wd):
        if write_image:
            (out_dir / "image.fit").write_bytes(b"COMPOSED")

    fake_rt = FakeRuntime(on_run=fake)
    monkeypatch.setattr(
        "nodes.basic.narrowband_palette.SirilRuntime", lambda *a, **k: fake_rt
    )
    NarrowbandPaletteNode().run(
        inputs={
            "sequence": Ref(
                node_hash="ext",
                port="sequence",
                path=seq_in,
                type=PortType.SEQUENCE_FITS,
            ),
        },
        params=NarrowbandPaletteParams(mode=mode),  # type: ignore[arg-type]
        ctx=_ctx(tmp_path),
        out_dir=out_dir,
    )
    return fake_rt


# ---------------------------------------------------------------------------
# Common command-shape assertions, exercised across every supported mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["hoo", "ohh", "hho", "ooh", "hoh", "oho", "hso"])
def test_seqextract_step_runs_first_with_resample_ha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Every mode must start by extracting Ha + OIII off the un-debayered CFA.

    The -resample=ha flag is load-bearing: without it Ha comes out at half
    resolution after Bayer demosaic. This is the gotcha the Naztronomy
    script's comment explicitly calls out.
    """
    rt = _run_node(tmp_path, monkeypatch, mode)
    cmds = rt.calls[0]["commands"]
    extract = next(c for c in cmds if c.startswith("seqextract_HaOIII "))
    assert extract == "seqextract_HaOIII pp_light -resample=ha"
    # Extract must run before either register call.
    extract_idx = cmds.index(extract)
    register_idx = next(
        i for i, c in enumerate(cmds) if c.startswith("register Ha_pp_light")
    )
    assert extract_idx < register_idx


@pytest.mark.parametrize("mode", ["hoo", "ohh", "hho", "ooh", "hoh", "oho", "hso"])
def test_per_channel_register_and_stack_are_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """register Ha -> stack Ha -> register OIII -> stack OIII, in that order.

    The relative order matters: shift-only `register results` later picks up
    `results_ha` and `results_oiii` together, so both stacks must already
    exist on disk by the time the align step runs.
    """
    rt = _run_node(tmp_path, monkeypatch, mode)
    cmds = rt.calls[0]["commands"]

    reg_ha_idx = next(i for i, c in enumerate(cmds) if c == "register Ha_pp_light")
    stack_ha_idx = next(
        i
        for i, c in enumerate(cmds)
        if c.startswith("stack r_Ha_pp_light rej 3 3")
    )
    reg_oiii_idx = next(
        i for i, c in enumerate(cmds) if c == "register OIII_pp_light"
    )
    stack_oiii_idx = next(
        i
        for i, c in enumerate(cmds)
        if c.startswith("stack r_OIII_pp_light rej 3 3")
    )
    assert reg_ha_idx < stack_ha_idx < reg_oiii_idx < stack_oiii_idx

    # Stack flags must mirror the Naztronomy build-script settings exactly.
    for stack_cmd in (cmds[stack_ha_idx], cmds[stack_oiii_idx]):
        assert "-norm=addscale" in stack_cmd
        assert "-output_norm" in stack_cmd
        assert "-32b" in stack_cmd
    assert "-out=results_ha" in cmds[stack_ha_idx]
    assert "-out=results_oiii" in cmds[stack_oiii_idx]


@pytest.mark.parametrize("mode", ["hoo", "ohh", "hho", "ooh", "hoh", "oho", "hso"])
def test_shift_only_align_runs_after_both_stacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    rt = _run_node(tmp_path, monkeypatch, mode)
    cmds = rt.calls[0]["commands"]
    align = next(c for c in cmds if c.startswith("register results"))
    assert align == "register results -transf=shift -interp=none"
    align_idx = cmds.index(align)
    stack_oiii_idx = next(
        i
        for i, c in enumerate(cmds)
        if c.startswith("stack r_OIII_pp_light rej 3 3")
    )
    assert align_idx > stack_oiii_idx


@pytest.mark.parametrize("mode", ["hoo", "ohh", "hho", "ooh", "hoh", "oho", "hso"])
def test_pixelmath_normalization_formula_exact_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """The OIII->Ha normalization PixelMath formula must match Naztronomy verbatim.

    Diverging here changes the numeric output of every narrowband composition,
    which is the whole point of porting the proven script and not improvising.
    """
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
    """rgbcomp R G B must point at the files dictated by PALETTE_CONFIG[mode]."""
    rt = _run_node(tmp_path, monkeypatch, mode)
    cmds = rt.calls[0]["commands"]
    rgbcomp = next(c for c in cmds if c.startswith("rgbcomp "))
    # Strip "rgbcomp " prefix and the -out=... suffix to read R/G/B by position.
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


# ---------------------------------------------------------------------------
# HSO-only behavior: synthetic green must be built and routed into G
# ---------------------------------------------------------------------------


def test_hso_emits_synthetic_green_pixelmath_and_uses_it_in_g(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = _run_node(tmp_path, monkeypatch, "hso")
    cmds = rt.calls[0]["commands"]

    expected_synth = "pm ($r_results_ha$ * 0.7) + ($normalized_r_results_oiii$ * 0.3)"
    assert any(c == expected_synth for c in cmds), (
        "expected synthetic-green PixelMath line for hso, got:\n" + "\n".join(cmds)
    )
    save_synth = next(c for c in cmds if c == "save synthetic_green")
    synth_idx = cmds.index(expected_synth)
    save_idx = cmds.index(save_synth)
    assert synth_idx < save_idx

    # Synthetic file must wind up in G of rgbcomp.
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


# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------


def test_missing_input_dir_raises_before_siril(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-existent input sequence dir must error out before launching Siril."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    fake_rt = FakeRuntime()
    monkeypatch.setattr(
        "nodes.basic.narrowband_palette.SirilRuntime", lambda *a, **k: fake_rt
    )
    with pytest.raises(RuntimeError, match="does not exist"):
        NarrowbandPaletteNode().run(
            inputs={
                "sequence": Ref(
                    node_hash="ext",
                    port="sequence",
                    path=tmp_path / "missing",
                    type=PortType.SEQUENCE_FITS,
                ),
            },
            params=NarrowbandPaletteParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )
    assert fake_rt.calls == []


def test_empty_input_sequence_raises_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An input dir with no matching frames should fail before Siril launch."""
    seq_in = tmp_path / "in"
    seq_in.mkdir()
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    fake_rt = FakeRuntime()
    monkeypatch.setattr(
        "nodes.basic.narrowband_palette.SirilRuntime", lambda *a, **k: fake_rt
    )
    with pytest.raises(RuntimeError, match="no input frames"):
        NarrowbandPaletteNode().run(
            inputs={
                "sequence": Ref(
                    node_hash="ext",
                    port="sequence",
                    path=seq_in,
                    type=PortType.SEQUENCE_FITS,
                ),
            },
            params=NarrowbandPaletteParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )
    assert fake_rt.calls == []


def test_unknown_mode_rejected_by_pydantic() -> None:
    """The Literal type in NarrowbandPaletteParams covers mode validation."""
    with pytest.raises(ValidationError):
        NarrowbandPaletteParams(mode="rgb")  # type: ignore[arg-type]


def test_raises_when_siril_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seq_in = _make_seq_dir(tmp_path / "in", "pp_light")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    fake_rt = FakeRuntime(returncode=5)
    monkeypatch.setattr(
        "nodes.basic.narrowband_palette.SirilRuntime", lambda *a, **k: fake_rt
    )
    with pytest.raises(RuntimeError, match="exited 5"):
        NarrowbandPaletteNode().run(
            inputs={
                "sequence": Ref(
                    node_hash="ext",
                    port="sequence",
                    path=seq_in,
                    type=PortType.SEQUENCE_FITS,
                ),
            },
            params=NarrowbandPaletteParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )


def test_raises_when_siril_returns_zero_but_image_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean exit with no output file must still trip the missing-image guard."""
    seq_in = _make_seq_dir(tmp_path / "in", "pp_light")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    fake_rt = FakeRuntime()  # write_image effectively False since no on_run
    monkeypatch.setattr(
        "nodes.basic.narrowband_palette.SirilRuntime", lambda *a, **k: fake_rt
    )
    with pytest.raises(RuntimeError, match="missing"):
        NarrowbandPaletteNode().run(
            inputs={
                "sequence": Ref(
                    node_hash="ext",
                    port="sequence",
                    path=seq_in,
                    type=PortType.SEQUENCE_FITS,
                ),
            },
            params=NarrowbandPaletteParams(),
            ctx=_ctx(tmp_path),
            out_dir=out_dir,
        )
