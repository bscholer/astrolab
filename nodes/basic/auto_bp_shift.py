"""auto_bp_shift: linear black-point shift sized to a target clip fraction.

Input port  : image (IMAGE_FITS) - typically the output of crop / graxpert
Output port : image (IMAGE_FITS) - background-shifted FITS for the stretch node

This is the "Linear stretch (BP shift)" mode of the Siril GHS dialog,
automated: pick the BP value such that a fixed fraction (default 1%) of the
pixels fall below it, then rescale linearly. The rest of the histogram is
preserved (no curve, no midtone touch) so subsequent stretch nodes see a
darker, tighter starting point with the background already pushed toward
zero.

Why a separate node from `stretch`:
  - Different intent: linear pre-conditioner vs. non-linear tone curve.
  - Composes with any downstream stretch (autostretch / mtf / asinh / GHS).
  - Lets the user toggle just this step without re-running upstream stack
    work.

Why a Python-side quantile instead of a Siril command:
  - Siril 1.4 has no percentile-clip primitive; the closest path is to
    pull a histogram, parse the file, and integrate. Numpy does it in one
    call against the same FITS data Siril would read.

Param: clip_fraction (default 0.01 = 1%) — fraction of pixels that end up
clipped to black. Matches the manual workflow ("crank BP until clipped >=
1%"). At 0 the node is a no-op rescale; above ~0.05 you start to crush
faint nebulosity and should think twice.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
from astropy.io import fits
from pydantic import BaseModel, Field

from nodes._seq_runner import image_ref, quote
from nodes.base import Node
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register
from server.siril import SirilRuntime, make_progress_handler


class AutoBpShiftParams(BaseModel):
    enabled: bool = Field(
        default=True,
        description="When off the node passes the input through unchanged. "
        "Default on: the linear BP shift is a near-universal first step "
        "before a non-linear stretch and the cost is negligible.",
        json_schema_extra={
            "agent_hint": (
                "Disabling leaves a high background pedestal that makes"
                " the subsequent stretch look washed out."
            ),
        },
    )
    neutral_sky: bool = Field(
        default=True,
        description="Shift each colour channel by its own black point so the sky "
        "comes out the same level in all three. A faint target's signal sits a "
        "few ten-thousandths above the black point, so a channel offset of "
        "0.00002 turns into a strong colour cast after the stretch.",
        json_schema_extra={"ui_section": "advanced"},
    )
    clip_fraction: float = Field(
        default=0.01,
        ge=0.0,
        lt=1.0,
        description="Fraction of pixels (in [0, 1)) that should fall below the "
        "computed black point and be clipped to zero. 0.01 (the default) "
        "matches the manual 'crank BP until ~1% clipped' workflow. 0 leaves "
        "the BP at the data minimum (linear rescale only, no clipping). "
        "Values above ~0.05 will start crushing real signal — careful.",
        json_schema_extra={
            "hash_precision": 5,
            "agent_hint": (
                "Higher clips more background to black, giving a punchier image;"
                " above 0.05 you start losing faint nebula detail."
            ),
        },
    )


@register("auto_bp_shift")
class AutoBpShiftNode(Node[AutoBpShiftParams]):
    id = "auto_bp_shift"
    version = 2
    tier = "keep"
    uses_siril = True

    inputs = {"image": PortType.IMAGE_FITS}
    outputs = {"image": PortType.IMAGE_FITS}
    params_schema = AutoBpShiftParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: AutoBpShiftParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out_dir_path = Path(out_dir)  # type: ignore[arg-type]
        src = inputs["image"].path

        if not src.exists():
            raise RuntimeError(f"auto_bp_shift: input image does not exist: {src}")

        out_image = out_dir_path / "image.fit"

        if not params.enabled:
            ctx.progress(0.5, "auto_bp_shift: disabled - passing through")
            shutil.copy(src, out_image)
            ctx.progress(1.0, "auto_bp_shift: wrote pass-through")
            return {"image": image_ref(out_image)}

        if params.neutral_sky and _neutral_shift(src, out_image, params.clip_fraction, ctx):
            ctx.progress(1.0, "auto_bp_shift: wrote neutral-sky shift")
            return {"image": image_ref(out_image)}

        bp = _compute_bp(src, params.clip_fraction)
        ctx.progress(0.3, f"auto_bp_shift: BP={bp:.5f} (clip={params.clip_fraction:g})")
        # Siril's `save` writes a FITS by default; it appends .fit so we
        # strip the suffix from the stem.
        save_stem = out_image.with_suffix("").name

        commands = [
            f"cd {quote(out_dir_path.resolve())}",
            f"load {quote(src.resolve())}",
            f"linstretch -BP={bp:.6f} -clipmode=clip",
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
                f"auto_bp_shift: siril exited {result.returncode}\n"
                f"--- ssf ---\n{result.ssf}\n"
                f"--- stdout (tail) ---\n{result.stdout[-4000:]}\n"
                f"--- stderr ---\n{result.stderr}"
            )

        if not out_image.exists():
            raise RuntimeError(
                f"auto_bp_shift: siril returned 0 but {out_image} is missing.\n"
                f"--- stdout (tail) ---\n{result.stdout[-2000:]}"
            )

        ctx.progress(1.0, f"auto_bp_shift: wrote {out_image.name}")
        return {"image": image_ref(out_image)}


def _compute_bp(src: Path, clip_fraction: float) -> float:
    """Return the pixel value at `clip_fraction` of the cumulative
    histogram, normalized into [0, 1) for Siril's linstretch.

    Siril's `linstretch -BP=` takes a normalized value: the on-disk FITS
    data can be float32 in roughly [0, 1] (Siril's internal stack output)
    or uint16 (raw lights). We detect the scale by peeking at the data
    max; if it's > 1.0 we divide through. Negative values (post-BG-extract
    residuals) are kept in the quantile but the final BP is clamped to
    [0, 1) so Siril doesn't reject the command.
    """
    with fits.open(src, memmap=True) as hdul:
        data = np.asarray(hdul[0].data, dtype=np.float64)
    if data.size == 0:
        raise RuntimeError(f"auto_bp_shift: {src} has no pixel data")

    bp = float(np.quantile(data, clip_fraction))
    data_max = float(np.nanmax(data))
    # Renormalize a uint16-scaled file into [0, 1] for Siril's linstretch.
    # The 1.01 cushion catches float32 stacks that just barely peek above
    # 1.0 from rejection-stack noise without flipping into uint16 mode.
    if data_max > 1.01:
        bp = bp / data_max

    if bp < 0.0:
        bp = 0.0
    if bp >= 1.0:
        bp = 0.999999
    return bp


SKY_PERCENTILE = 10


def _sky(channel: np.ndarray, lit: np.ndarray, block: int = 8) -> float:
    """A channel's sky level: a low percentile of 8x8 block medians.

    Block medians cut the noise eightfold and erase stars, so the percentile
    sits on the sky itself whether a nebula covers a tenth of the frame or
    half of it; a percentile of raw pixels lands at a different point in
    the noise depending on how much of the channel is nebula.
    """
    h, w = (channel.shape[0] // block) * block, (channel.shape[1] // block) * block
    c = channel[:h, :w].reshape(h // block, block, w // block, block).swapaxes(1, 2)
    m = lit[:h, :w].reshape(h // block, block, w // block, block).swapaxes(1, 2)
    full = m.reshape(h // block, w // block, -1).all(axis=2)
    meds = np.median(c.reshape(h // block, w // block, -1), axis=2)[full]
    return float(np.percentile(meds, SKY_PERCENTILE)) if meds.size else 0.0


def _neutral_shift(src: Path, out_image: Path, clip_fraction: float, ctx: RunContext) -> bool:
    """Per-channel black point that leaves the sky equal in every channel.

    The shared black point is the `clip_fraction` quantile of all pixels, as
    in the plain path; each channel then moves by how far its own sky
    (see _sky) sits from the mean sky, so the sky lands at the same level
    everywhere. Zero pixels (registration
    border) stay zero. Returns False for data this doesn't apply to (mono,
    or not float in [0, 1]), and the caller takes the Siril path.
    """
    with fits.open(src, memmap=False) as hdul:
        data, header = hdul[0].data, hdul[0].header.copy()
    if data is None or data.ndim != 3 or data.shape[0] != 3:
        return False
    data = data.astype(np.float32)
    if float(np.nanmax(data)) > 1.01:
        return False
    lit = (data != 0).any(axis=0)
    if not lit.any():
        return False
    sample = data[:, lit][:, ::7]
    bp = float(np.quantile(sample, clip_fraction))
    sky = np.array([_sky(data[c], lit) for c in range(3)])
    bps = np.clip(bp + (sky - sky.mean()), 0.0, 0.999999)
    out = np.empty_like(data)
    for c in range(3):
        out[c] = np.clip((data[c] - bps[c]) / (1.0 - bps[c]), 0.0, 1.0)
    out[:, ~lit] = 0.0
    ctx.progress(
        0.6,
        f"auto_bp_shift: black points R {bps[0]:.5f} G {bps[1]:.5f} B {bps[2]:.5f}"
        " (sky made neutral)",
    )
    fits.PrimaryHDU(out, header=header).writeto(out_image, overwrite=True)
    return True

