"""seq_register: align a sequence with Siril 1.4's `register` command.

Input port  : sequence (SEQUENCE_FITS) - typically the output of calibrate
Output port : sequence (SEQUENCE_FITS) - r_<basename>_*.fit or r_<basename>.fit

Siril writes registered frames into cwd with an `r_` prefix; we cd into
out_dir/sequence, symlink the input frames, run, then drop the input symlinks.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from nodes.base import Node
from nodes.basic.calibrate import _quote, _stage_sequence
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register
from server.siril import SirilRuntime


class SeqRegisterParams(BaseModel):
    input_basename: str = Field(
        default="pp_light",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_]+$",
        description="Basename of the input sequence. Output is prefixed with 'r_'.",
    )
    fitseq: bool = Field(
        default=True,
        description="Operate on a FITSEQ container instead of per-frame files.",
    )
    two_pass: bool = Field(
        default=True,
        description="Pass -2pass for the global star alignment refinement step. "
        "Roughly halves residuals at the cost of one extra pass.",
    )
    transform: str = Field(
        default="homography",
        pattern=r"^(homography|affine|similarity|shift)$",
        description="Transform model. Homography handles atmospheric turbulence and "
        "field rotation; shift is fastest but only works for tracked mounts.",
    )
    min_pairs: int = Field(
        default=10,
        ge=4,
        le=200,
        description="Minimum star pairs Siril must find to register a frame. Frames "
        "below the threshold are dropped from the registered sequence.",
    )


@register("seq_register")
class SeqRegisterNode(Node[SeqRegisterParams]):
    id = "seq_register"
    version = 1
    cost = "expensive"

    inputs = {"sequence": PortType.SEQUENCE_FITS}
    outputs = {"sequence": PortType.SEQUENCE_FITS}
    params_schema = SeqRegisterParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: SeqRegisterParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out_dir_path = Path(out_dir)  # type: ignore[arg-type]
        seq_in = inputs["sequence"].path

        if not seq_in.exists():
            raise RuntimeError(f"seq_register: input dir does not exist: {seq_in}")

        seq_out = out_dir_path / "sequence"
        seq_out.mkdir(parents=True, exist_ok=True)

        staged = _stage_sequence(seq_in, seq_out, params.input_basename, params.fitseq)
        if not staged:
            raise RuntimeError(
                f"seq_register: no input frames matching basename "
                f"'{params.input_basename}' under {seq_in}"
            )

        opts: list[str] = [f"-transf={params.transform}", f"-minpairs={params.min_pairs}"]
        if params.two_pass:
            opts.append("-2pass")

        ctx.progress(0.2, f"seq_register: aligning {len(staged)} frames")
        commands = [
            f"cd {_quote(seq_out.resolve())}",
            f"register {params.input_basename} {' '.join(opts)}",
        ]
        runtime = SirilRuntime()
        result = runtime.run(
            commands,
            working_dir=seq_out,
            on_log=lambda line: ctx.log.debug("siril: %s", line),
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"seq_register: siril exited {result.returncode}\n"
                f"--- ssf ---\n{result.ssf}\n--- stderr ---\n{result.stderr}"
            )

        out_basename = f"r_{params.input_basename}"
        if params.fitseq:
            expected = seq_out / f"{out_basename}.fit"
            if not expected.exists():
                raise RuntimeError(
                    f"seq_register: siril returned 0 but FITSEQ container "
                    f"{expected} is missing.\n--- stdout (tail) ---\n"
                    f"{result.stdout[-2000:]}"
                )
            wrote = expected.name
        else:
            frames = sorted(
                p
                for p in seq_out.iterdir()
                if p.name.startswith(f"{out_basename}_")
                and p.suffix in (".fit", ".fits")
            )
            if not frames:
                raise RuntimeError(
                    f"seq_register: siril returned 0 but no {out_basename}_*.fit* "
                    f"frames landed in {seq_out}.\n--- stdout (tail) ---\n"
                    f"{result.stdout[-2000:]}"
                )
            wrote = f"{len(frames)} frames"

        for link in staged:
            if link.is_symlink() or link.exists():
                link.unlink()

        ctx.progress(1.0, f"seq_register: wrote {wrote}")
        return {
            "sequence": Ref(
                node_hash="",
                port="sequence",
                path=seq_out,
                type=PortType.SEQUENCE_FITS,
            )
        }
