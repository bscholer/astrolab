"""calibrate: apply master_dark (and optional master_flat / master_bias) to a
sequence via Siril 1.4's `calibrate` command.

Input ports
- sequence (SEQUENCE_FITS, required)        prior step's sequence dir
- dark     (MASTER_FITS,   required)        master dark frame
- flat     (MASTER_FITS,   optional)        master flat frame
- bias     (MASTER_FITS,   optional)        master bias frame

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

from nodes.base import Node
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register
from server.siril import SirilRuntime


class CalibrateParams(BaseModel):
    input_basename: str = Field(
        default="light",
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z0-9_]+$",
        description="Basename of the input sequence (matches Siril `<basename>_NNNNN.fit` "
        "or the `<basename>.fit` FITSEQ container). Output is automatically prefixed "
        "with 'pp_'.",
    )
    fitseq: bool = Field(
        default=True,
        description="Operate on a FITSEQ container (single .fit) rather than per-frame "
        "files. Must match the upstream convert_lights setting.",
    )
    cfa: bool = Field(
        default=False,
        description="Pass -cfa for OSC sensors so darks and flats are scaled per "
        "Bayer pattern. Off by default for already-debayered or mono data.",
    )
    cosmetic: bool = Field(
        default=True,
        description="Pass -cc=dark to apply hot/cold pixel correction from the dark. "
        "Cheap and almost always wanted.",
    )
    equalize_cfa: bool = Field(
        default=False,
        description="Pass -equalize_cfa when calibrating CFA flats (fixes channel "
        "offsets). Only meaningful with cfa=True.",
    )


@register("calibrate")
class CalibrateNode(Node[CalibrateParams]):
    id = "calibrate"
    version = 1
    cost = "medium"

    inputs = {
        "sequence": PortType.SEQUENCE_FITS,
        "dark": PortType.MASTER_FITS,
        "flat": PortType.MASTER_FITS,
        "bias": PortType.MASTER_FITS,
    }
    optional_inputs = frozenset({"flat", "bias"})
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

        if not seq_in.exists():
            raise RuntimeError(f"calibrate: input sequence dir does not exist: {seq_in}")

        seq_out = out_dir_path / "sequence"
        seq_out.mkdir(parents=True, exist_ok=True)

        staged = _stage_sequence(seq_in, seq_out, params.input_basename, params.fitseq)
        if not staged:
            raise RuntimeError(
                f"calibrate: no input frames matching basename "
                f"'{params.input_basename}' under {seq_in}"
            )

        opts: list[str] = [f"-dark={_quote(inputs['dark'].path.resolve())}"]
        if "flat" in inputs:
            opts.append(f"-flat={_quote(inputs['flat'].path.resolve())}")
        if "bias" in inputs:
            opts.append(f"-bias={_quote(inputs['bias'].path.resolve())}")
        if params.cfa:
            opts.append("-cfa")
        if params.cosmetic:
            opts.append("-cc=dark")
        if params.equalize_cfa and params.cfa:
            opts.append("-equalize_cfa")
        if params.fitseq:
            opts.append("-fitseq")

        ctx.progress(0.2, f"calibrate: running siril on {len(staged)} frames")
        commands = [
            f"cd {_quote(seq_out.resolve())}",
            f"calibrate {params.input_basename} {' '.join(opts)}",
        ]
        runtime = SirilRuntime()
        result = runtime.run(
            commands,
            working_dir=seq_out,
            on_log=lambda line: ctx.log.debug("siril: %s", line),
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"calibrate: siril exited {result.returncode}\n"
                f"--- ssf ---\n{result.ssf}\n--- stderr ---\n{result.stderr}"
            )

        out_basename = f"pp_{params.input_basename}"
        if params.fitseq:
            expected = seq_out / f"{out_basename}.fit"
            if not expected.exists():
                raise RuntimeError(
                    f"calibrate: siril returned 0 but FITSEQ container "
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
                    f"calibrate: siril returned 0 but no {out_basename}_*.fit* "
                    f"frames landed in {seq_out}.\n--- stdout (tail) ---\n"
                    f"{result.stdout[-2000:]}"
                )
            if len(frames) != len(staged):
                raise RuntimeError(
                    f"calibrate: expected {len(staged)} calibrated frames, "
                    f"got {len(frames)}"
                )
            wrote = f"{len(frames)} frames"

        # Strip input symlinks so the cache entry only holds this node's outputs.
        for link in staged:
            if link.is_symlink() or link.exists():
                link.unlink()

        ctx.progress(1.0, f"calibrate: wrote {wrote}")
        return {
            "sequence": Ref(
                node_hash="",
                port="sequence",
                path=seq_out,
                type=PortType.SEQUENCE_FITS,
            )
        }


def _stage_sequence(
    seq_in: Path, seq_out: Path, basename: str, fitseq: bool
) -> list[Path]:
    """Symlink the input sequence into seq_out so Siril finds it in cwd.

    Returns the list of staged links so the caller can clean them up post-run.
    """
    staged: list[Path] = []
    if fitseq:
        src = seq_in / f"{basename}.fit"
        if not src.exists():
            return []
        link = seq_out / src.name
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(src.resolve())
        staged.append(link)
        return staged

    for f in sorted(seq_in.iterdir()):
        if not (f.is_file() or f.is_symlink()):
            continue
        if not f.name.startswith(f"{basename}_"):
            continue
        if f.suffix not in (".fit", ".fits"):
            continue
        link = seq_out / f.name
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(f.resolve())
        staged.append(link)
    return staged


def _quote(path: Path) -> str:
    s = str(path)
    if any(c in s for c in (" ", "\t", '"')):
        return '"' + s.replace('"', r"\"") + '"'
    return s
