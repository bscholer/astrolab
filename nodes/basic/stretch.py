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

import numpy as np
from astropy.io import fits
from pydantic import BaseModel, Field

from nodes._seq_runner import image_ref, quote
from nodes.base import Node
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register
from server.siril import SirilRuntime, make_progress_handler


class StretchParams(BaseModel):
    method: Literal["autostretch", "auto", "mtf", "asinh"] = Field(
        default="autostretch",
        description="Stretch algorithm. 'autostretch' picks shadow/midtone/highlight "
        "automatically (closest to what Siril GUI shows on load). 'auto' measures "
        "the image: it lifts the faint extended signal (galaxy, nebula) to a visible "
        "level and keeps the sky within a set range. 'mtf' is the "
        "classic three-slider midtones transfer function for manual control. "
        "'asinh' suits galaxy-style images with bright cores and dim halos.",
        json_schema_extra={
            "agent_hint": (
                "Choose 'autostretch' for a quick natural look, 'mtf' for precise"
                " manual control, or 'asinh' for galaxy cores with wide dynamic range."
            ),
        },
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
            "agent_hint": (
                "Linked keeps colors true; unlinked can flatten a color cast"
                " but often looks washed out."
            ),
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
            "agent_hint": (
                "More negative = more shadow detail preserved;"
                " less negative = punchier blacks."
            ),
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
            "agent_hint": (
                "Lower = darker sky background;"
                " higher = brighter overall image that can look milky."
            ),
        },
    )

    # --- auto (image-based) ---
    auto_object: float = Field(
        default=0.4,
        ge=0.1,
        le=0.8,
        description="Where the faint extended signal (galaxy arms, nebula) lands "
        "after the stretch, 0-1. Higher is brighter.",
        json_schema_extra={
            "hash_precision": 3,
            "ui_when": {"method": "auto"},
            "agent_hint": "Higher brightens the object; the sky follows within its limits.",
        },
    )
    auto_bg_min: float = Field(
        default=0.08,
        ge=0.0,
        le=0.5,
        description="Darkest the sky background may get.",
        json_schema_extra={"hash_precision": 3, "ui_section": "advanced",
                           "ui_when": {"method": "auto"}},
    )
    auto_bg_max: float = Field(
        default=0.15,
        ge=0.0,
        le=0.5,
        description="Brightest the sky background may get. Faint targets hit this "
        "limit; more would only show more noise.",
        json_schema_extra={"hash_precision": 3, "ui_section": "advanced",
                           "ui_when": {"method": "auto"}},
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
            "agent_hint": (
                "Raise to crush the background to black;"
                " too high clips faint nebulosity."
            ),
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
            "agent_hint": (
                "Lower = brighter midtones and lifted faint detail;"
                " too low clips highlights."
            ),
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
            "agent_hint": "Lower = brighter overall image but risks blowing out star cores.",
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
            "agent_hint": (
                "Higher lifts faint nebulosity more aggressively;"
                " very high values flatten bright cores."
            ),
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
            "agent_hint": "Slightly negative darkens the background and suppresses noise grain.",
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
            "agent_hint": (
                "Enabling this preserves color saturation but can push greens"
                " too strong on OSC sensors."
            ),
        },
    )


@register("stretch")
class StretchNode(Node[StretchParams]):
    id = "stretch"
    version = 1
    tier = "keep"
    uses_siril = True
    preview_display_ready = True

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
        if params.method == "auto":
            data = fits.getdata(src)
            black, mid, info = auto_mtf(
                data, params.auto_object, params.auto_bg_min, params.auto_bg_max
            )
            ctx.progress(
                0.2,
                f"stretch: auto sky {info['sky']:.5f} noise {info['noise']:.5f} "
                f"signal {info['signal']:.5f} -> black {black:.5f} midtones {mid:.5f} "
                f"(sky lands at {info['sky_out']:.2f}, object at {info['object_out']:.2f})",
            )
            stretch_cmd = f"mtf {black:.6g} {mid:.6g} 1"
        else:
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
        return {"image": image_ref(out_image, display_ready=True)}


def _mtf(x: float, m: float) -> float:
    return (m - 1) * x / ((2 * m - 1) * x - m)


def _midtones_for(x: float, y: float) -> float:
    """The MTF midtones balance that maps x to y."""
    return x * (y - 1) / (2 * x * y - x - y)


def auto_mtf(
    data: np.ndarray, obj: float = 0.4, bg_min: float = 0.08, bg_max: float = 0.15
) -> tuple[float, float, dict[str, float]]:
    """Pick a linked MTF (black point, midtones) from the image itself.

    Sky: median of the luminance, noise: its MAD. Signal: the 99.5th
    percentile of the luminance after an 8x8 block median, which erases
    stars and keeps extended light (galaxy arms, nebula). The black point
    sits 2.8 sigma below the sky, as in Siril's autostretch; the midtones
    put the signal at `obj`, then move so the sky stays in [bg_min, bg_max].
    A strong galaxy gets a dark sky, a faint nebula a brighter one.
    """
    lum = data.mean(axis=0) if data.ndim == 3 and data.shape[0] in (3, 4) else data
    lum = np.asarray(lum, dtype=np.float32)[::4, ::4]
    finite = lum[np.isfinite(lum)]
    sky = float(np.median(finite))
    noise = float(1.4826 * np.median(np.abs(finite - sky)))
    h, w = (lum.shape[0] // 8) * 8, (lum.shape[1] // 8) * 8
    blocks = np.nan_to_num(lum[:h, :w]).reshape(h // 8, 8, w // 8, 8).swapaxes(1, 2)
    smooth = np.median(blocks.reshape(h // 8, w // 8, 64), axis=2)
    signal = float(np.percentile(smooth, 99.5)) if smooth.size else sky
    black = max(0.0, sky - 2.8 * noise)
    scale = max(1.0 - black, 1e-6)
    xs = min(max((signal - black) / scale, 1e-6), 0.999)
    xb = min(max((sky - black) / scale, 1e-6), 0.999)
    mid = _midtones_for(xs, obj) if xs > xb else _midtones_for(xb, bg_max)
    if _mtf(xb, mid) > bg_max:
        mid = _midtones_for(xb, bg_max)
    elif _mtf(xb, mid) < bg_min:
        mid = _midtones_for(xb, bg_min)
    mid = min(max(mid, 1e-6), 0.999)
    info = {"sky": sky, "noise": noise, "signal": signal,
            "sky_out": _mtf(xb, mid), "object_out": _mtf(xs, mid)}
    return black, mid, info


def _build_stretch_cmd(p: StretchParams) -> str:
    """Render the per-method Siril command line."""
    if p.method == "autostretch":
        # Siril's `autostretch` is per-channel by default; `-linked` opts into
        # one shared curve. There is no `-unlinked` flag — pass nothing for
        # per-channel.
        flag = "-linked " if p.linked else ""
        return f"autostretch {flag}{p.shadows_clip:g} {p.target_bg:g}"
    if p.method == "mtf":
        return f"mtf {p.mtf_shadows:g} {p.mtf_midtones:g} {p.mtf_highlights:g}"
    if p.method == "asinh":
        cmd = f"asinh {p.asinh_stretch:g} {p.asinh_offset:g}"
        if p.asinh_human:
            cmd += " -human"
        return cmd
    raise ValueError(f"unknown stretch method {p.method!r}")
