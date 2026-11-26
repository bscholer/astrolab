"""narrowband_extract: split a CFA sequence into Ha + OIII, align the two stacks.

Input port  : sequence (SEQUENCE_FITS) - calibrated, NOT debayered. The Bayer
                                         pattern must still be intact so
                                         seqextract_HaOIII can pick out the red
                                         (Ha) and blue/green (OIII) sub-pixels.
Output ports: ha   (IMAGE_FITS) - aligned Ha stack at out_dir/r_results_ha.fit
              oiii (IMAGE_FITS) - aligned OIII stack at out_dir/r_results_oiii.fit

Phase 1 of the OSC narrowband flow (the expensive part). Phase 2 lives in
narrowband_compose, which takes ha + oiii and renders the chosen palette.
Splitting at this boundary means flipping the palette only re-runs the
cheap compose step.

The Siril chain here mirrors the first half of Naztronomy's
Smart-Telescope-PP narrowband flow verbatim:

    seqextract_HaOIII <pp_seq> -resample=ha
        -> Ha_<pp_seq> + OIII_<pp_seq>. -resample=ha upscales the Ha
           channel to match OIII's post-demosaic resolution; without it
           Ha comes out at half-rez.
    register Ha_<pp_seq> ; stack r_Ha_<pp_seq> rej 3 3 -norm=addscale ...
        -> results_ha.fit
    register OIII_<pp_seq> ; stack r_OIII_<pp_seq> rej 3 3 -norm=addscale ...
        -> results_oiii.fit
    [rename to results_NNNNN.fit, link, register results -transf=shift]
        -> r_results_ha.fit (shift-aligned to OIII)
        -> r_results_oiii.fit (shift-aligned to Ha)

The rename dance around `register results` is required because Siril's
register needs either an existing .seq file or files matching
basename_NNNNN.fit naming; the bare results_ha.fit + results_oiii.fit
match neither. See the in-line comments and PR #48 for the longer story.
"""

from __future__ import annotations

import contextlib
import shutil
from pathlib import Path

from pydantic import BaseModel, Field

from nodes._seq_runner import drop_staged, quote, stage_sequence
from nodes.base import Node
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register
from server.siril import SirilRuntime, make_progress_handler


class NarrowbandExtractParams(BaseModel):
    input_basename: str = Field(
        default="pp_light",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_]+$",
        description="Basename of the calibrated CFA sequence. Default 'pp_light' "
        "matches the canned narrowband template (convert -> calibrate without "
        "debayer). The sequence MUST be CFA (Bayer pattern intact); "
        "seqextract_HaOIII reads sub-pixel positions from the Bayer mosaic.",
        json_schema_extra={"ui_hidden": True},
    )
    fitseq: bool = Field(
        default=False,
        description="Operate on a FITSEQ container instead of per-frame files. "
        "Default off because seqextract_HaOIII writes per-frame outputs and "
        "matching the upstream sequence layout makes input staging simpler.",
        json_schema_extra={"ui_hidden": True},
    )
    shared_registration: bool = Field(
        default=False,
        description="Register once, on a plate-solved debayered copy of the "
        "frames, and apply the same transforms to the H-alpha and OIII splits. "
        "Each frame's two channels come from one exposure and share its "
        "geometry, but split 15 s frames have too few real stars to match on "
        "their own (identity transforms on a whole California night; no frame "
        "at all on a short Rosette night). Both stacks land on one grid, so the "
        "fragile stack-to-stack shift alignment is skipped; narrowband_pack "
        "fixes the sub-pixel Bayer offset that remains.",
        json_schema_extra={"ui_hidden": True},
    )


@register("narrowband_extract")
class NarrowbandExtractNode(Node[NarrowbandExtractParams]):
    id = "narrowband_extract"
    version = 2
    tier = "keep"
    uses_siril = True

    inputs = {"sequence": PortType.SEQUENCE_FITS}
    outputs = {
        "ha": PortType.IMAGE_FITS,
        "oiii": PortType.IMAGE_FITS,
    }
    params_schema = NarrowbandExtractParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: NarrowbandExtractParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out_dir_path = Path(out_dir)  # type: ignore[arg-type]
        seq_in = inputs["sequence"].path

        if not seq_in.exists():
            raise RuntimeError(
                f"narrowband_extract: input sequence dir does not exist: {seq_in}"
            )

        work_dir = out_dir_path / "_narrowband"
        work_dir.mkdir(parents=True, exist_ok=True)

        staged = stage_sequence(
            seq_in, work_dir, params.input_basename, params.fitseq
        )
        if not staged:
            raise RuntimeError(
                f"narrowband_extract: no input frames matching basename "
                f"'{params.input_basename}' under {seq_in}. The sequence "
                f"must be CFA (un-debayered) so seqextract_HaOIII can read "
                f"the Bayer pattern."
            )

        ctx.progress(0.1, "narrowband_extract: extracting Ha/OIII and aligning")
        runtime = SirilRuntime()
        if params.shared_registration:
            result = _run_shared(runtime, params.input_basename, work_dir, ctx)
        else:
            commands = _build_extract_commands(params.input_basename, work_dir)
            # Six progress-emitting Siril sub-commands: seqextract_HaOIII,
            # register Ha, stack r_Ha, register OIII, stack r_OIII, register
            # results. The interleaved load/save and `link` calls don't emit
            # progress, so they don't count. phases must match this exactly or
            # the bar rewinds when a later sub-command opens at 0%.
            result = runtime.run(
                commands,
                working_dir=work_dir,
                on_log=make_progress_handler(ctx, phases=6),
                cancel=ctx.cancel,
            )
        if result.returncode != 0:
            raise RuntimeError(
                f"narrowband_extract: siril exited {result.returncode}\n"
                f"--- ssf ---\n{result.ssf}\n"
                f"--- stdout (tail) ---\n{result.stdout[-4000:]}\n"
                f"--- stderr ---\n{result.stderr}"
            )

        ha_src = work_dir / "r_results_ha.fit"
        oiii_src = work_dir / "r_results_oiii.fit"
        if not ha_src.exists() or not oiii_src.exists():
            raise RuntimeError(
                f"narrowband_extract: siril returned 0 but expected outputs "
                f"are missing (ha={ha_src.exists()}, oiii={oiii_src.exists()}).\n"
                f"--- stdout (tail) ---\n{result.stdout[-2000:]}"
            )

        ha_out = out_dir_path / "r_results_ha.fit"
        oiii_out = out_dir_path / "r_results_oiii.fit"
        shutil.move(str(ha_src), ha_out)
        shutil.move(str(oiii_src), oiii_out)

        # Drop input symlinks plus the work_dir full of intermediates.
        drop_staged(staged)
        with contextlib.suppress(OSError):
            shutil.rmtree(work_dir)

        ctx.progress(1.0, "narrowband_extract: wrote r_results_ha + r_results_oiii")
        return {
            "ha": Ref(node_hash="", port="ha", path=ha_out, type=PortType.IMAGE_FITS),
            "oiii": Ref(node_hash="", port="oiii", path=oiii_out, type=PortType.IMAGE_FITS),
        }


def transfer_registration(src: Path, dst: Path) -> None:
    """Write sequence `dst` with the frame selection and transforms of `src`.

    `src` is the plate-solved debayered copy of the same frames (its R lines
    are per colour layer, eg R1); `dst` is a mono split sequence (Ha_ or
    OIII_), so the transforms are written as layer 0. Siril only writes a
    sequence's .seq file when a command first opens it, so right after
    seqextract_HaOIII there is none: dst is built from src under its own
    name (taken from the file name, eg OIII_pp_light_.seq). If Siril did
    write one, its image count must match. Statistics (M lines) are left
    out; Siril recomputes them.
    """
    src_lines = src.read_text().splitlines()
    s_src = next(x for x in src_lines if x.startswith("S '"))
    f_src = s_src[s_src.index("'", 3) + 1 :].split()
    if dst.exists():
        s_dst = next((x for x in dst.read_text().splitlines() if x.startswith("S '")), None)
        if s_dst is not None:
            f_dst = s_dst[s_dst.index("'", 3) + 1 :].split()
            if f_dst[1] != f_src[1]:
                raise RuntimeError(
                    f"narrowband_extract: {src.name} has {f_src[1]} images, "
                    f"{dst.name} {f_dst[1]}"
                )
    out = [x for x in src_lines if x.startswith("#")]
    out.append(f"S '{dst.stem}' " + " ".join(f_src))
    out.append("L 1")
    out += [x for x in src_lines if x.startswith("I ")]
    for x in src_lines:
        if x.startswith("R") and len(x) > 1 and x[1].isdigit():
            out.append("R0" + x[x.index(" ") :])
    dst.write_text("\n".join(out) + "\n")


def copy_registration(src: Path, dst: Path, name: str) -> None:
    """Give sequence `dst` the selection and registration of `src` (same layout)."""
    out = []
    for line in src.read_text().splitlines():
        if line.startswith("M"):
            continue
        if line.startswith("S '"):
            line = f"S '{name}'" + line[line.index("'", 3) + 1 :]
        out.append(line)
    dst.write_text("\n".join(out) + "\n")


def _run_shared(runtime: SirilRuntime, base: str, work_dir: Path, ctx: RunContext):
    """Register once on a debayered copy, apply to both split channels, stack.

    Single 15 s duo-band frames split into H-alpha and OIII have too few real
    stars to match: star matching locked onto leftover hot pixels and
    returned identity transforms for a whole 2.3 h California night, which
    smeared every star out of the stack. A debayered copy of the same frames
    plate-solves (as stack_per_night registers them); its transforms fit the
    split frames, which share the geometry.
    """
    cd = f"cd {quote(work_dir.resolve())}"
    first = runtime.run(
        [
            cd,
            f"calibrate {base} -debayer -prefix=db_",
            f"seqplatesolve db_{base} -nocache -force",
            f"seqextract_HaOIII {base} -resample=ha",
        ],
        working_dir=work_dir,
        on_log=make_progress_handler(ctx, low=0.1, high=0.5, phases=3),
        cancel=ctx.cancel,
    )
    if first.returncode != 0:
        # The reference frame didn't solve (Siril then fails the whole
        # sequence): star-match the debayered copy instead, which has the
        # signal the split frames lack.
        ctx.progress(0.3, "narrowband_extract: plate solving failed, matching stars")
        first = runtime.run(
            [
                cd,
                f"register db_{base} -2pass",
                f"seqextract_HaOIII {base} -resample=ha",
            ],
            working_dir=work_dir,
            on_log=make_progress_handler(ctx, low=0.3, high=0.5, phases=2),
            cancel=ctx.cancel,
        )
        if first.returncode != 0:
            return first
    db = next(iter(sorted(work_dir.glob(f"db_{base}*.seq"))), None)
    if db is None:
        raise RuntimeError(f"narrowband_extract: no registered debayered sequence in {work_dir}")
    seq = db.name.removeprefix("db_")  # eg pp_light_.seq
    for channel in ("OIII", "Ha"):
        if not (work_dir / f"{channel}_{seq.removesuffix('.seq')}00001.fit").exists():
            raise RuntimeError(f"narrowband_extract: seqextract_HaOIII wrote no {channel} frames")
        transfer_registration(db, work_dir / f"{channel}_{seq}")
    stack = "rej 3 3 -norm=addscale -output_norm -32b"
    return runtime.run(
        [
            cd,
            f"seqapplyreg OIII_{base} -framing=max",
            f"seqapplyreg Ha_{base} -framing=max",
            f"stack r_OIII_{base} {stack} -out=r_results_oiii",
            f"stack r_Ha_{base} {stack} -out=r_results_ha",
        ],
        working_dir=work_dir,
        on_log=make_progress_handler(ctx, low=0.5, high=0.95, phases=4),
        cancel=ctx.cancel,
    )


def _build_extract_commands(input_basename: str, work_dir: Path) -> list[str]:
    """Render the Siril SSF for the extract+align half of the narrowband flow.

    Kept as a free function so tests can introspect the exact command list
    without spinning up SirilRuntime.
    """
    ha_seq = f"Ha_{input_basename}"
    oiii_seq = f"OIII_{input_basename}"
    ha_stack = "results_ha"
    oiii_stack = "results_oiii"

    cmds: list[str] = [f"cd {quote(work_dir.resolve())}"]

    # Split the CFA sequence into Ha and OIII. -resample=ha upscales Ha so it
    # matches OIII's spatial resolution post-demosaic.
    cmds.append(f"seqextract_HaOIII {input_basename} -resample=ha")

    # Register and stack each channel independently.
    for src_seq, out_name in ((ha_seq, ha_stack), (oiii_seq, oiii_stack)):
        cmds.append(f"register {src_seq}")
        cmds.append(
            f"stack r_{src_seq} rej 3 3 -norm=addscale -output_norm -32b "
            f"-out={out_name}"
        )

    # Build a 2-frame sequence from the two stacks and shift-align it. Siril's
    # `register` needs either an existing .seq file or files matching the
    # basename_NNNNN.fit naming convention; the bare results_ha.fit +
    # results_oiii.fit match neither. Copy via load/save into results_NNNNN.fit,
    # materialize results.seq with `link`, then register.
    cmds.append(f"load {ha_stack}")
    cmds.append("save results_00001")
    cmds.append(f"load {oiii_stack}")
    cmds.append("save results_00002")
    cmds.append("link results")
    cmds.append("register results -transf=shift -interp=none")

    # `register results` writes r_results_00001 (Ha; sorted first) and
    # r_results_00002 (OIII). Copy them to the semantic names that the
    # compose node and downstream PixelMath formulas expect.
    cmds.append("load r_results_00001")
    cmds.append("save r_results_ha")
    cmds.append("load r_results_00002")
    cmds.append("save r_results_oiii")

    return cmds
