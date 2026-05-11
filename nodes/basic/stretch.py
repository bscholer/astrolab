"""stretch: non-linear tone-mapping of a stacked FITS into a viewable range.

Input port  : image (IMAGE_FITS) - typically the output of seq_stack
Output port : image (IMAGE_FITS) - stretched FITS, ready for save_image

Wraps Siril 1.4's stretching commands. Linear stack data sits mostly near zero
with a long bright tail; a stretch lifts faint nebulosity above the noise
floor while keeping bright stars from blowing out. Siril's autostretch is the
"just do it" default (matches what the GUI shows when you load a FITS); MTF
exposes the classic three-slider control; asinh suits galaxy-style images
where dim halos coexist with bright cores.

Output stays as a FITS so downstream nodes (save_image, future color_balance)
can keep working on float pixel data. The save_image node renders the final
8-bit PNG/JPEG.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from nodes._seq_runner import image_ref, quote
from nodes.base import Node
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register
from server.siril import SirilRuntime, make_progress_handler


class StretchParams(BaseModel):
    method: Literal["autostretch", "mtf", "asinh"] = Field(
        default="autostretch",
        description="Stretch algorithm. 'autostretch' picks shadow/midtone/highlight "
        "automatically (closest to what Siril GUI shows on load). 'mtf' is the "
        "classic three-slider midtones transfer function for manual control. "
        "'asinh' suits galaxy-style images with bright cores and dim halos.",
    )

    # --- autostretch ---
    linked: bool = Field(
        default=True,
        description="-linked stretches all three channels with the same curve, "
        "preserving color. -unlinked stretches each channel independently which "
        "often gives a flatter, more 'auto' look but can wash out color casts "
        "you'd want to keep. Default linked.",
        json_schema_extra={
            "ui_section": "advanced",
            "ui_when": {"method": "autostretch"},
        },
    )
    shadows_clip: float = Field(
        default=-2.8,
        ge=-10.0,
        le=0.0,
        description="Sigma offset below the median where shadows get clipped. "
        "More negative keeps more shadow detail; closer to zero produces a "
        "punchier image with crushed blacks. Siril default is -2.8.",
        json_schema_extra={
            "hash_precision": 3,
            "ui_when": {"method": "autostretch"},
        },
    )
    target_bg: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        description="Target background level in [0,1]. 0.25 (Siril default) "
        "places the sky a quarter of the way up; lower values darken the "
        "background, higher values brighten it.",
        json_schema_extra={
            "hash_precision": 3,
            "ui_when": {"method": "autostretch"},
        },
    )

    # --- mtf ---
    mtf_shadows: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Black point. Anything below this becomes pure black.",
        json_schema_extra={
            "hash_precision": 4,
            "ui_when": {"method": "mtf"},
        },
    )
    mtf_midtones: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        description="Midtone balance. Lower values brighten faint detail "
        "(typical 0.1-0.3 for deep-sky); 0.5 is no-op.",
        json_schema_extra={
            "hash_precision": 4,
            "ui_when": {"method": "mtf"},
        },
    )
    mtf_highlights: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="White point. Anything above this clips to pure white.",
        json_schema_extra={
            "hash_precision": 4,
            "ui_when": {"method": "mtf"},
        },
    )

    # --- asinh ---
    asinh_stretch: float = Field(
        default=10.0,
        ge=1.0,
        le=1000.0,
        description="Stretch factor. Higher pulls fainter detail up; 10-50 is "
        "typical for galaxies, 100+ for very dim targets.",
        json_schema_extra={
            "hash_precision": 2,
            "ui_when": {"method": "asinh"},
        },
    )
    asinh_offset: float = Field(
        default=0.0,
        ge=-1.0,
        le=1.0,
        description="Black-point offset applied before the asinh curve. "
        "Slightly negative (e.g. -0.01) suppresses the noise floor.",
        json_schema_extra={
            "hash_precision": 4,
            "ui_when": {"method": "asinh"},
        },
    )
    asinh_human: bool = Field(
        default=False,
        description="Use the human-vision-weighted variant (-human). Preserves "
        "color saturation better but can over-emphasize green channels on OSC "
        "data.",
        json_schema_extra={
            "ui_section": "advanced",
            "ui_when": {"method": "asinh"},
        },
    )


@register("stretch")
class StretchNode(Node[StretchParams]):
    id = "stretch"
    version = 1
    cost = "cheap"
    uses_siril = True

    inputs = {"image": PortType.IMAGE_FITS}
    outputs = {"image": PortType.IMAGE_FITS}
    params_schema = StretchParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: StretchParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out_dir_path = Path(out_dir)  # type: ignore[arg-type]
        src = inputs["image"].path

        if not src.exists():
            raise RuntimeError(f"stretch: input image does not exist: {src}")

        out_image = out_dir_path / "image.fit"

        # Siril's `save` writes a FITS by default; we strip the .fit suffix
        # because Siril appends one itself.
        save_stem = out_image.with_suffix("").name

        ctx.progress(0.1, f"stretch: applying {params.method}")
        stretch_cmd = _build_stretch_cmd(params)
        commands = [
            f"cd {quote(out_dir_path.resolve())}",
            f"load {quote(src.resolve())}",
            stretch_cmd,
            f"save {quote(save_stem)}",
        ]
        runtime = SirilRuntime()
        result = runtime.run(
            commands,
            working_dir=out_dir_path,
            on_log=make_progress_handler(ctx),
            cancel=ctx.cancel,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"stretch: siril exited {result.returncode}\n"
                f"--- ssf ---\n{result.ssf}\n"
                f"--- stdout (tail) ---\n{result.stdout[-4000:]}\n"
                f"--- stderr ---\n{result.stderr}"
            )

        if not out_image.exists():
            raise RuntimeError(
                f"stretch: siril returned 0 but {out_image} is missing.\n"
                f"--- stdout (tail) ---\n{result.stdout[-2000:]}"
            )

        ctx.progress(1.0, f"stretch: wrote {out_image.name}")
        return {"image": image_ref(out_image)}


def _build_stretch_cmd(p: StretchParams) -> str:
    """Render the per-method Siril command line."""
    if p.method == "autostretch":
        flag = "-linked" if p.linked else "-unlinked"
        return f"autostretch {flag} {p.shadows_clip:g} {p.target_bg:g}"
    if p.method == "mtf":
        return f"mtf {p.mtf_shadows:g} {p.mtf_midtones:g} {p.mtf_highlights:g}"
    if p.method == "asinh":
        cmd = f"asinh {p.asinh_stretch:g} {p.asinh_offset:g}"
        if p.asinh_human:
            cmd += " -human"
        return cmd
    raise ValueError(f"unknown stretch method {p.method!r}")
