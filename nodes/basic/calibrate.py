"""calibrate: apply master_dark (and optional master_flat / master_bias) to a
sequence via Siril 1.4's `calibrate` command.

Input ports
- sequence (SEQUENCE_FITS, required)        prior step's sequence dir
- dark     (MASTER_FITS,   optional)        master dark frame
- flat     (MASTER_FITS,   optional)        master flat frame
- bias     (MASTER_FITS,   optional)        master bias frame

All three masters are optional. With none provided, `calibrate` becomes a
prefix-rename pass with -cfa/-debayer applied; the resulting stack is
noisier than a dark-calibrated one but the pipeline still completes. This
unblocks sessions whose gain/temp/exptime don't match any indexed master.

Output port
- sequence (SEQUENCE_FITS): directory containing pp_<basename>_*.fit (or
                            pp_<basename>.fit when the input was a FITSEQ).

Siril writes calibrated frames into the cwd with a `pp_` prefix. We cd into
out_dir/sequence, symlink the input frames there so Siril sees them, run the
command, and (after success) drop the input symlinks so the cache entry only
contains this node's outputs.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from nodes._seq_runner import (
    _check_siril_seq_result,
    drop_staged,
    quote,
    run_siril_on_sequence,
    seq_ref,
    stage_sequence,
)
from nodes.base import Node
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register
from server.siril import SirilRuntime, make_progress_handler


class CalibrateParams(BaseModel):
    input_basename: str = Field(
        default="light",
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z0-9_]+$",
        description="Basename of the input sequence (matches Siril `<basename>_NNNNN.fit` "
        "or the `<basename>.fit` FITSEQ container). Output is automatically prefixed "
        "with 'pp_'.",
        json_schema_extra={"ui_hidden": True},
    )
    fitseq: bool = Field(
        default=True,
        description="Operate on a FITSEQ container (single .fit) rather than per-frame "
        "files. Must match the upstream convert_lights setting.",
        json_schema_extra={"ui_hidden": True},
    )
    cfa: bool = Field(
        default=True,
        description="Pass -cfa for OSC sensors so darks and flats are scaled per "
        "Bayer pattern. On by default — this is the right behavior for any sensor "
        "with BAYERPAT in the FITS header (Dwarf 3, Seestar, ZWO OSC). Flip off "
        "only for mono cameras or already-debayered inputs.",
        json_schema_extra={
            "ui_section": "advanced",
            "agent_hint": (
                "Leave on for any OSC (color) sensor; turning off with a"
                " Bayer-pattern camera produces a cyan/magenta color cast."
            ),
        },
    )
    cosmetic: bool = Field(
        default=True,
        description="Pass -cc=dark to apply hot/cold pixel correction from the dark. "
        "Cheap and almost always wanted.",
        json_schema_extra={
            "ui_section": "advanced",
            "agent_hint": (
                "Keeping on removes bright hot-pixel specks that would otherwise"
                " appear as stars in the final image."
            ),
        },
    )
    equalize_cfa: bool = Field(
        default=True,
        description="Pass -equalize_cfa when calibrating CFA flats; equalizes the two "
        "G channels of the Bayer pattern so post-debayer colors are balanced. Only "
        "meaningful with cfa=True; on by default for the OSC pipeline.",
        json_schema_extra={
            "ui_section": "advanced",
            "agent_hint": (
                "Keeps green channel balance even; turning off can cause"
                " a subtle green or magenta tint."
            ),
        },
    )
    debayer: bool = Field(
        default=True,
        description="Pass -debayer so calibrate emits debayered RGB frames. On for "
        "OSC sensors (Dwarf 3): registration applies sub-pixel shifts that scramble "
        "the Bayer pattern, so we must debayer here before register/stack. Turn off "
        "only for mono cameras or pure-CFA workflows.",
        json_schema_extra={
            "ui_section": "advanced",
            "agent_hint": (
                "Leave on for OSC sensors; turning off means registration shifts"
                " will scramble the Bayer mosaic and ruin color."
            ),
        },
    )


@register("calibrate")
class CalibrateNode(Node[CalibrateParams]):
    id = "calibrate"
    version = 1
    cost = "medium"
    uses_siril = True
    preview_hidden = True

    inputs = {
        "sequence": PortType.SEQUENCE_FITS,
        "dark": PortType.MASTER_FITS,
        "flat": PortType.MASTER_FITS,
        "bias": PortType.MASTER_FITS,
    }
    optional_inputs = frozenset({"dark", "flat", "bias"})
    outputs = {"sequence": PortType.SEQUENCE_FITS}
    params_schema = CalibrateParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: CalibrateParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out_dir_path = Path(out_dir)  # type: ignore[arg-type]
        seq_in = inputs["sequence"].path
        seq_out = out_dir_path / "sequence"

        opts: list[str] = []
        if "dark" in inputs:
            opts.append(f"-dark={quote(inputs['dark'].path.resolve())}")
        if "flat" in inputs:
            opts.append(f"-flat={quote(inputs['flat'].path.resolve())}")
        if "bias" in inputs:
            opts.append(f"-bias={quote(inputs['bias'].path.resolve())}")
        if params.cfa:
            opts.append("-cfa")
        if params.cosmetic and "dark" in inputs:
            # -cc=dark needs a dark to detect hot/cold pixels; silently drop
            # cosmetic correction when no dark is available.
            opts.append("-cc=dark")
        if params.equalize_cfa and params.cfa:
            opts.append("-equalize_cfa")
        if params.debayer:
            opts.append("-debayer")
        if params.fitseq:
            opts.append("-fitseq")

        out_basename = f"pp_{params.input_basename}"
        ctx.progress(0.2, "calibrate: running siril on sequence")

        if not params.fitseq:
            # Per-frame path: stage manually so we can record the input count,
            # then validate calibrate produced one output per input (calibrate
            # never drops frames -- if counts diverge, something went wrong).
            seq_out.mkdir(parents=True, exist_ok=True)
            if not seq_in.exists():
                raise RuntimeError(f"calibrate: input dir does not exist: {seq_in}")
            staged = stage_sequence(seq_in, seq_out, params.input_basename, params.fitseq)
            if not staged:
                raise RuntimeError(
                    f"calibrate: no input frames matching basename "
                    f"'{params.input_basename}' under {seq_in}"
                )
            n_input = len([p for p in staged if p.suffix in (".fit", ".fits")])
            commands = [
                f"cd {quote(seq_out.resolve())}",
                f"calibrate {params.input_basename} {' '.join(opts)}",
            ]
            result = SirilRuntime().run(
                commands,
                working_dir=seq_out,
                on_log=make_progress_handler(ctx),
                cancel=ctx.cancel,
            )
            # _check_siril_seq_result tolerates the Siril 1.4 shutdown segfault
            # (returncode -11) when outputs are all present. It raises on real
            # failures. min_count=n_input enforces the one-output-per-input
            # invariant that calibrate guarantees (it never drops frames).
            wrote = _check_siril_seq_result(
                result,
                node_name="calibrate",
                seq_out=seq_out,
                out_basename=out_basename,
                fitseq=False,
                min_count=n_input,
            )
            frames = sorted(
                p
                for p in seq_out.iterdir()
                if p.name.startswith(f"{out_basename}_")
                and p.suffix in (".fit", ".fits")
            )
            if len(frames) != n_input:
                raise RuntimeError(
                    f"calibrate: expected {n_input} calibrated frames, "
                    f"got {len(frames)}"
                )
            drop_staged(staged)
            wrote = f"{len(frames)} frames"
        else:
            commands = [
                f"cd {quote(seq_out.resolve())}",
                f"calibrate {params.input_basename} {' '.join(opts)}",
            ]
            wrote = run_siril_on_sequence(
                node_name="calibrate",
                seq_in=seq_in,
                seq_out=seq_out,
                commands=commands,
                basename=params.input_basename,
                out_basename=out_basename,
                fitseq=params.fitseq,
                ctx=ctx,
                runtime=SirilRuntime(),
            )

        ctx.progress(1.0, f"calibrate: wrote {wrote}")
        return {"sequence": seq_ref(seq_out)}
