"""combine_stacks: align several stacks of one target and stack them into one image.

Input port  : images (IMAGE_FITS_LIST) - one stack per night (per session), any exposure
Output port : image  (IMAGE_FITS)      - the combined stack at out_dir/image.fit

Built for the stack-per-night template: each night is calibrated, registered, and
stacked on its own (and cached), then this node joins the nights. The sub-stacks
are plate-solved and reprojected onto the first one's grid (star matching if
plate solving fails), then stacked with additive+scale normalization and noise
weighting, so a long or 30 s night counts for more than a short or 15 s one.
Rejection only runs with enough stacks to judge outliers; satellites and planes
were already rejected within each night.
"""

from __future__ import annotations

import shutil
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


class CombineStacksParams(BaseModel):
    weight: Literal["noise", "nbstack", "none"] = Field(
        default="noise",
        description="How much each night counts. 'noise' weights by measured background "
        "noise, which accounts for both exposure length and frame count; 'nbstack' by "
        "frame count only.",
        json_schema_extra={"ui_section": "advanced"},
    )
    min_for_rejection: int = Field(
        default=5,
        ge=3,
        description="Use winsorized rejection across nights only with at least this "
        "many nights; fewer get a plain weighted mean.",
        json_schema_extra={"ui_section": "advanced"},
    )
    min_coverage: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Trim to the area that got at least this share of the "
        "exposure, judged from local noise. Nights framed differently leave "
        "bands only some of them cover; those are noisier and step in "
        "brightness at each night's edge. 0 keeps everything.",
        json_schema_extra={"hash_precision": 3, "ui_section": "advanced"},
    )
    framing: Literal["max", "min", "first"] = Field(
        default="max",
        description="Output frame: 'max' keeps the union of all nights, 'min' only the "
        "area every night covers, 'first' the first night's frame.",
        json_schema_extra={"ui_section": "advanced"},
    )


@register("combine_stacks")
class CombineStacksNode(Node[CombineStacksParams]):
    id = "combine_stacks"
    version = 2
    tier = "keep"
    uses_siril = True

    inputs = {"images": PortType.IMAGE_FITS_LIST}
    outputs = {"image": PortType.IMAGE_FITS}
    params_schema = CombineStacksParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: CombineStacksParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out_dir_path = Path(out_dir)  # type: ignore[arg-type]
        images: list[Ref] = inputs["images"]  # type: ignore[assignment]
        out_image = out_dir_path / "image.fit"
        if not images:
            raise RuntimeError("combine_stacks: no stacks to combine")
        if len(images) == 1:
            shutil.copyfile(images[0].path, out_image)
            return {"image": image_ref(out_image)}

        work = out_dir_path / "_combine"
        work.mkdir(parents=True, exist_ok=True)
        for i, ref in enumerate(images):
            # Ends in a letter so Siril doesn't read it as frame i of a sequence.
            (work / f"in{i:04d}x.fit").symlink_to(Path(ref.path).resolve())

        rejection = "w" if len(images) >= params.min_for_rejection else "n"
        stack = (
            f"stack r_night rej {rejection} 3 3 -norm=addscale -output_norm -rgb_equal"
            + ("" if params.weight == "none" else f" -weight={params.weight}")
            + f" -out={quote(out_image.resolve())}"
        )
        aligners = {
            "plate solving": [
                "seqplatesolve night -nocache -force",
                f"seqapplyreg night -framing={params.framing}",
            ],
            "star matching": [
                "register night -2pass",
                f"seqapplyreg night -framing={params.framing}",
            ],
        }
        errors: list[str] = []
        for name, align in aligners.items():
            ctx.progress(0.1, f"combine_stacks: aligning {len(images)} stacks by {name}")
            result = SirilRuntime().run(
                [f"cd {quote(work.resolve())}", "link night", *align, stack],
                working_dir=work,
                on_log=make_progress_handler(ctx),
                cancel=ctx.cancel,
            )
            if result.returncode == 0 and out_image.exists():
                break
            errors.append(f"{name}: siril exited {result.returncode}\n{result.stdout[-2000:]}")
            for f in work.iterdir():
                if f.name.startswith("in") and f.is_symlink():
                    continue
                if f.is_dir() and not f.is_symlink():
                    shutil.rmtree(f, ignore_errors=True)
                else:
                    f.unlink(missing_ok=True)
        else:
            raise RuntimeError("combine_stacks: could not align the stacks\n" + "\n".join(errors))

        if params.min_coverage > 0:
            kept = mask_low_coverage(out_image, params.min_coverage)
            ctx.progress(0.95, f"combine_stacks: {kept:.0%} of the frame is well covered")
        shutil.rmtree(work, ignore_errors=True)
        ctx.progress(1.0, f"combine_stacks: combined {len(images)} stacks")
        return {"image": image_ref(out_image)}


COVERAGE_BLOCK = 64
EDGE_LOW_SHARE = 0.1
"""A rectangle edge may cross this share of poorly covered blocks (a bright
star or a galaxy core reads as low coverage too)."""


def coverage_map(lum: np.ndarray, block: int = COVERAGE_BLOCK) -> np.ndarray:
    """Relative coverage per block, from local noise.

    A pixel stacked from fewer nights is noisier, and noise goes as one over
    the square root of the exposure behind it, so (best noise / noise)^2 is
    the share of the exposure a block got. Noise is the MAD of neighbouring
    pixel differences, which ignores gradients. Blocks with little data are NaN.
    """
    h, w = (lum.shape[0] // block) * block, ((lum.shape[1] - 1) // block) * block
    diff = lum[:h, 1 : w + 1] - lum[:h, :w]
    valid = (lum[:h, 1 : w + 1] != 0) & (lum[:h, :w] != 0)
    shape = (h // block, block, w // block, block)
    d = diff.reshape(shape).swapaxes(1, 2).reshape(h // block, w // block, -1)
    v = valid.reshape(shape).swapaxes(1, 2).reshape(h // block, w // block, -1)
    sig = np.full(d.shape[:2], np.nan, dtype=np.float32)
    for i in range(d.shape[0]):
        for j in range(d.shape[1]):
            vals = d[i, j][v[i, j]]
            if vals.size > 0.8 * block * block:
                sig[i, j] = np.median(np.abs(vals - np.median(vals))) / 0.6745
    if np.all(np.isnan(sig)):
        return sig
    ref = np.nanpercentile(sig, 10)
    return (ref / sig) ** 2


def mask_low_coverage(out_image: Path, min_coverage: float) -> float:
    """Blank the combined image outside the largest well-covered rectangle.

    The rectangle shrinks from the bounding box, emptiest edge first, until
    each edge crosses at most EDGE_LOW_SHARE of blocks below `min_coverage`.
    Inside it nothing changes, so a bright core that reads as low coverage
    is never blanked; the crop node then trims the blank border away.
    Returns the share of the frame kept.
    """
    from nodes.basic.crop import _shrink_to_data

    with fits.open(out_image, memmap=False) as hdul:
        data, header = hdul[0].data, hdul[0].header.copy()
    lum = np.nan_to_num(data.mean(axis=0) if data.ndim == 3 else data).astype(np.float32)
    cov = coverage_map(lum)
    if cov.size == 0 or np.all(np.isnan(cov)):
        return 1.0
    good = np.nan_to_num(cov, nan=0.0) >= min_coverage
    rows, cols = np.flatnonzero(good.any(axis=1)), np.flatnonzero(good.any(axis=0))
    if rows.size == 0:
        return 1.0
    x, y = int(cols[0]), int(rows[0])
    bw, bh = int(cols[-1]) - x + 1, int(rows[-1]) - y + 1
    x, y, bw, bh = _shrink_to_data(good, x, y, bw, bh, tol=EDGE_LOW_SHARE)
    b = COVERAGE_BLOCK
    y0, y1, x0, x1 = y * b, (y + bh) * b, x * b, (x + bw) * b
    keep = np.zeros(lum.shape, dtype=bool)
    keep[y0:y1, x0:x1] = True
    data = np.where(keep, data, 0).astype(data.dtype)
    fits.PrimaryHDU(data, header=header).writeto(out_image, overwrite=True)
    return float(keep.mean())
