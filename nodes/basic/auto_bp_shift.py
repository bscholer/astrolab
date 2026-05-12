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

from nodes._seq_runner import _check_siril_single_image_result, image_ref, quote
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
    version = 1
    cost = "cheap"
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
        _check_siril_single_image_result(
            result, node_name="auto_bp_shift", out_path=out_image
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
