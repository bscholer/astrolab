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
    version = 3
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
            cov = footprint_coverage(work, out_image)
            kept = mask_low_coverage(out_image, params.min_coverage, cov)
            ctx.progress(0.95, f"combine_stacks: {kept:.0%} of the frame is well covered")
        shutil.rmtree(work, ignore_errors=True)
        ctx.progress(1.0, f"combine_stacks: combined {len(images)} stacks")
        return {"image": image_ref(out_image)}


COVERAGE_BLOCK = 64
EDGE_LOW_SHARE = 0.1
"""A rectangle edge may cross this share of poorly covered blocks (a bright
star or a galaxy core reads as low coverage too)."""


def coverage_map_shape(shape: tuple[int, ...], block: int = COVERAGE_BLOCK) -> tuple[int, int]:
    return shape[0] // block, (shape[1] - 1) // block


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


def _block_mean(m: np.ndarray, block: int = COVERAGE_BLOCK) -> np.ndarray:
    h, w = (m.shape[0] // block) * block, ((m.shape[1] - 1) // block) * block
    return m[:h, :w].reshape(h // block, block, w // block, block).mean(axis=(1, 3))


def _sky_noise(lum: np.ndarray, lit: np.ndarray) -> float:
    """Noise of the sky: neighbour differences where both pixels are among the
    darker 40% (nebula and stars excluded, they add photon noise)."""
    vals = lum[lit]
    cut = float(np.percentile(vals[:: max(1, vals.size // 200_000)], 40))
    a, b = lum[:, 1:], lum[:, :-1]
    sky = (a != 0) & (b != 0) & (a <= cut) & (b <= cut)
    diff = (a - b)[sky]
    if diff.size < 100:
        return 1.0
    diff = diff[:: max(1, diff.size // 400_000)]
    sigma = float(np.median(np.abs(diff - np.median(diff)))) / 0.6745 / np.sqrt(2)
    return sigma or 1.0


def footprint_coverage(work: Path, out_image: Path) -> np.ndarray | None:
    """Coverage per block from where each registered night has data.

    Each night counts by its noise weight (1 / background variance, as the
    stack weights it), so the map is the share of the total weight behind
    each block. Unlike the noise map this is blind to the image itself:
    a bright narrowband nebula raises local noise and used to read as
    poorly covered, which trimmed the California Nebula out of its own
    frame. None when the registered nights are not on the output canvas.
    """
    with fits.open(out_image, memmap=False) as hdul:
        shape = hdul[0].data.shape[-2:]
    nights = sorted(work.glob("r_night_*.fit*"))
    if len(nights) < 2:
        return None
    total = np.zeros(shape, np.float32)
    weights = 0.0
    for p in nights:
        with fits.open(p, memmap=False) as hdul:
            d = hdul[0].data
        if d is None or d.shape[-2:] != shape:
            return None
        lum = np.nan_to_num(d.mean(axis=0) if d.ndim == 3 else d).astype(np.float32)
        lit = lum != 0
        if not lit.any():
            continue
        sigma = _sky_noise(lum, lit)
        w = 1.0 / sigma**2
        total += w * lit
        weights += w
    if weights == 0:
        return None
    return _block_mean(total / weights)


def mask_low_coverage(
    out_image: Path, min_coverage: float, cov: np.ndarray | None = None
) -> float:
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
    if cov is None or cov.shape != coverage_map_shape(lum.shape):
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
