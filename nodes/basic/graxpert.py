"""graxpert: AI-driven background extraction OR denoising.

Wraps the GraXpert CLI (https://www.graxpert.com/). Two modes share one
node so users don't have to rewire the graph to flip between them; the
template instantiates the kind twice (graxpert_bg, graxpert_denoise) at
different positions.

Inputs / outputs are both IMAGE_FITS. Disabled by default — the user opts
in per project, and the node runs the binary as a subprocess.

Binary location: we look for $ASTROLAB_GRAXPERT_BIN first, then fall back
to ~/tools/graxpert/graxpert (the layout install-tools-linux.sh creates),
then $PATH. If none of those exist, the node raises with a clear pointer
to the install script rather than a cryptic FileNotFoundError.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from astropy.io import fits
from pydantic import BaseModel, Field

from nodes.base import Node
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register


class GraxpertParams(BaseModel):
    enabled: bool = Field(
        default=False,
        description="Off by default. GraXpert is expensive (AI inference + a "
        "multi-GB model download on first run); flip on once you've seen the "
        "stretched stack and want a cleaner gradient or denoise pass.",
    )
    mode: Literal["bg_extract", "denoise"] = Field(
        default="bg_extract",
        description="Which GraXpert pipeline to invoke. 'bg_extract' models "
        "the sky gradient with an AI background and subtracts it (replaces "
        "Siril's seqsubsky for cases where polynomial fits leave residual "
        "structure). 'denoise' applies the AI denoiser to a stretched "
        "image; safe to run after stretch.",
    )
    smoothing: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="bg_extract only: smoothing factor for the background "
        "model. Higher (0.3-0.7) preserves coarse structure where the AI is "
        "uncertain; 0 lets the model decide. Default 0.",
        json_schema_extra={
            "hash_precision": 2,
            "ui_when": {"mode": "bg_extract"},
        },
    )
    correction: Literal["Subtraction", "Division"] = Field(
        default="Subtraction",
        description="bg_extract only: how the model is removed. Subtraction "
        "is right for additive sky glow (the OSC default). Division for "
        "multiplicative gradients (rare with our pipeline).",
        json_schema_extra={
            "ui_section": "advanced",
            "ui_when": {"mode": "bg_extract"},
        },
    )
    strength: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="denoise only: blend factor from raw (0) to fully "
        "denoised (1). 0.4-0.6 is the typical sweet spot; 1.0 over-smooths.",
        json_schema_extra={
            "hash_precision": 2,
            "ui_when": {"mode": "denoise"},
        },
    )
    use_gpu: bool = Field(
        default=True,
        description="Pass -gpu to GraXpert. The Linux box has the inference "
        "GPU; CPU inference is doable but ~10x slower.",
        json_schema_extra={"ui_section": "advanced"},
    )


@register("graxpert")
class GraxpertNode(Node[GraxpertParams]):
    id = "graxpert"
    version = 1
    cost = "expensive"

    inputs = {"image": PortType.IMAGE_FITS}
    outputs = {"image": PortType.IMAGE_FITS}
    params_schema = GraxpertParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: GraxpertParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out_dir_path = Path(out_dir)  # type: ignore[arg-type]
        src = inputs["image"].path
        if not src.exists():
            raise RuntimeError(f"graxpert: input image does not exist: {src}")

        out_image = out_dir_path / "image.fit"

        if not params.enabled:
            ctx.progress(0.9, "graxpert: disabled — passing through")
            _passthrough_fits(src, out_image)
            ctx.progress(1.0, "graxpert: wrote pass-through")
            return _result(out_image)

        binary = _locate_binary()
        if binary is None:
            raise RuntimeError(
                "graxpert: binary not found. Run scripts/install-tools-linux.sh "
                "on the processing host, or set ASTROLAB_GRAXPERT_BIN to the "
                "executable's path."
            )

        # GraXpert wants to write to a directory next to its input by default;
        # we sandbox the run inside out_dir to keep cache discipline. Stage
        # the input as a copy (not a symlink) because some GraXpert builds
        # rewrite headers in-place during the pre-process step.
        staged = out_dir_path / "input.fit"
        _passthrough_fits(src, staged)

        cmd = [
            str(binary),
            "-cli",
            "-cmd",
            "background-extraction" if params.mode == "bg_extract" else "denoising",
            str(staged),
            "-output",
            str(out_image),
        ]
        if params.mode == "bg_extract":
            cmd += [
                "-correction",
                params.correction,
                "-smoothing",
                f"{params.smoothing:g}",
            ]
        else:  # denoise
            cmd += ["-strength", f"{params.strength:g}"]
        # -gpu takes 'true' / 'false' (string), not a bare flag.
        cmd += ["-gpu", "true" if params.use_gpu else "false"]

        ctx.progress(0.1, f"graxpert: running {params.mode}")
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=60 * 60,  # AI runs can be slow on big frames; an hour is generous.
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"graxpert: failed to spawn {binary}: {exc}") from exc

        if result.returncode != 0:
            raise RuntimeError(
                f"graxpert ({params.mode}) exited {result.returncode}\n"
                f"--- cmd ---\n{' '.join(cmd)}\n"
                f"--- stdout (tail) ---\n{result.stdout[-3000:]}\n"
                f"--- stderr ---\n{result.stderr[-2000:]}"
            )

        # GraXpert sometimes appends a suffix to the requested output (e.g.
        # `image_GraXpert.fits` instead of `image.fit`). Tolerate either.
        if not out_image.exists():
            siblings = sorted(out_dir_path.glob("image*.fit*"))
            siblings = [p for p in siblings if p != staged]
            if siblings:
                siblings[0].rename(out_image)
            else:
                raise RuntimeError(
                    f"graxpert ({params.mode}) returned 0 but no output FITS "
                    f"landed in {out_dir_path}.\n--- stdout (tail) ---\n"
                    f"{result.stdout[-2000:]}"
                )

        # Drop the staged input copy so the cache entry only holds the output.
        with contextlib.suppress(FileNotFoundError):
            staged.unlink()

        ctx.progress(1.0, f"graxpert: wrote {out_image.name}")
        return _result(out_image)


def _locate_binary() -> Path | None:
    """Find the GraXpert binary via env override, ~/tools layout, or $PATH."""
    override = os.environ.get("ASTROLAB_GRAXPERT_BIN")
    if override:
        p = Path(override).expanduser()
        return p if p.is_file() else None

    layout = Path.home() / "tools" / "graxpert" / "graxpert"
    if layout.is_file():
        return layout

    found = shutil.which("graxpert") or shutil.which("GraXpert")
    return Path(found) if found else None


def _passthrough_fits(src: Path, dst: Path) -> None:
    """Read src, write dst with the same data + header. Used both for the
    'enabled=false' path and to stage GraXpert's input as a real copy."""
    with fits.open(src, memmap=False) as hdul:
        hdu = hdul[0]
        data = hdu.data
        header = hdu.header.copy()
        if data is None:
            for ext in hdul[1:]:
                if ext.data is not None:
                    data = ext.data
                    header = ext.header.copy()
                    break
    if data is None:
        raise RuntimeError(f"graxpert: no image data in {src}")
    fits.PrimaryHDU(data=data, header=header).writeto(dst, overwrite=True)


def _result(out_image: Path) -> dict[str, Ref]:
    return {
        "image": Ref(
            node_hash="",
            port="image",
            path=out_image,
            type=PortType.IMAGE_FITS,
        )
    }
