"""starnet_extract: split a stretched image into starless + stars layers.

Wraps the StarNet++ binary (https://www.starnetastro.com/). StarNet emits a
'starless' image; the 'stars' layer is just original minus starless. Cheap
arithmetic, but having it as an explicit output port lets downstream nodes
(starnet_replace, starnet_recombine) treat the layers as first-class data.

Disabled by default. When off, both outputs are well-defined so the
recombine math at the end of the chain reduces to identity:
  starless = input
  stars    = zero image (same shape/dtype as input)

That way users can leave the entire StarNet trio wired-but-disabled in
the template and only flip `enabled` when they want the workflow.

Binary location: $ASTROLAB_STARNET_BIN, then ~/tools/starnet/starnet++,
then $PATH. Models (.pb files) are expected to sit next to the binary.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
from astropy.io import fits
from pydantic import BaseModel, Field

from nodes.base import Node
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register


class StarnetExtractParams(BaseModel):
    enabled: bool = Field(
        default=False,
        description="Off by default. StarNet inference is heavy; flip on "
        "when you want to process the starless layer differently from the "
        "stars (deeper stretch, denoise, color tweak, etc.) before "
        "recombining.",
    )
    stride: int = Field(
        default=128,
        ge=64,
        le=512,
        description="StarNet++ tile stride in pixels. Smaller = more "
        "overlap = cleaner edges, but slower. 128 is the typical default; "
        "drop to 64 for very dense star fields.",
        json_schema_extra={"ui_section": "advanced"},
    )


@register("starnet_extract")
class StarnetExtractNode(Node[StarnetExtractParams]):
    id = "starnet_extract"
    version = 1
    cost = "expensive"

    inputs = {"image": PortType.IMAGE_FITS}
    outputs = {
        "starless": PortType.IMAGE_FITS,
        "stars": PortType.IMAGE_FITS,
    }
    params_schema = StarnetExtractParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: StarnetExtractParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out_dir_path = Path(out_dir)  # type: ignore[arg-type]
        src = inputs["image"].path
        if not src.exists():
            raise RuntimeError(f"starnet_extract: input does not exist: {src}")

        starless_out = out_dir_path / "starless.fit"
        stars_out = out_dir_path / "stars.fit"

        ctx.progress(0.05, "starnet_extract: reading input")
        data, header = _read_fits(src)

        if not params.enabled:
            ctx.progress(0.7, "starnet_extract: disabled — pass-through + zero stars")
            fits.PrimaryHDU(data=data, header=header).writeto(starless_out, overwrite=True)
            zeros = np.zeros_like(data)
            fits.PrimaryHDU(data=zeros, header=header).writeto(stars_out, overwrite=True)
            ctx.progress(1.0, "starnet_extract: pass-through")
            return _result(starless_out, stars_out)

        binary = _locate_binary()
        if binary is None:
            raise RuntimeError(
                "starnet_extract: starnet++ binary not found. Place the "
                "Linux package contents at ~/tools/starnet/ (with the .pb "
                "models alongside the binary) or set ASTROLAB_STARNET_BIN."
            )

        # StarNet++ rewrites in-place when called with one positional, or
        # writes to <output> when given two. We want the second form.
        # Run from a clean cwd so the .pb model files (which StarNet looks
        # for next to the binary) are reachable via the binary's directory.
        starnet_cwd = binary.parent
        cmd = [str(binary), str(src.resolve()), str(starless_out.resolve()), str(params.stride)]

        ctx.progress(0.1, "starnet_extract: running starnet++")
        result = subprocess.run(
            cmd,
            cwd=starnet_cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=60 * 60,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"starnet_extract: starnet++ exited {result.returncode}\n"
                f"--- cmd ---\n{' '.join(cmd)}\n"
                f"--- stdout (tail) ---\n{result.stdout[-3000:]}\n"
                f"--- stderr ---\n{result.stderr[-2000:]}"
            )
        if not starless_out.exists():
            raise RuntimeError(
                f"starnet_extract: starnet++ returned 0 but {starless_out} "
                f"is missing.\n--- stdout (tail) ---\n{result.stdout[-2000:]}"
            )

        # Stars layer = original minus starless. Clip negatives because the
        # StarNet model can occasionally produce a starless that's slightly
        # brighter than the original on some pixels (rounding noise).
        ctx.progress(0.9, "starnet_extract: deriving stars layer")
        starless_data, _ = _read_fits(starless_out)
        stars = np.clip(data.astype(np.float32) - starless_data.astype(np.float32), 0.0, None)
        # Match dtype of the input so downstream stages stay consistent.
        stars = stars.astype(data.dtype, copy=False)
        fits.PrimaryHDU(data=stars, header=header).writeto(stars_out, overwrite=True)

        ctx.progress(1.0, "starnet_extract: done")
        return _result(starless_out, stars_out)


def _locate_binary() -> Path | None:
    override = os.environ.get("ASTROLAB_STARNET_BIN")
    if override:
        p = Path(override).expanduser()
        return p if p.is_file() else None

    layout = Path.home() / "tools" / "starnet" / "starnet++"
    if layout.is_file():
        return layout

    found = shutil.which("starnet++") or shutil.which("starnet")
    return Path(found) if found else None


def _read_fits(src: Path) -> tuple[np.ndarray, fits.Header]:
    with fits.open(src, memmap=False) as hdul:
        data = hdul[0].data
        header = hdul[0].header.copy()
        if data is None:
            for ext in hdul[1:]:
                if ext.data is not None:
                    data = ext.data
                    header = ext.header.copy()
                    break
    if data is None:
        raise RuntimeError(f"starnet_extract: no image data in {src}")
    return data, header


def _result(starless: Path, stars: Path) -> dict[str, Ref]:
    return {
        "starless": Ref(
            node_hash="",
            port="starless",
            path=starless,
            type=PortType.IMAGE_FITS,
        ),
        "stars": Ref(
            node_hash="",
            port="stars",
            path=stars,
            type=PortType.IMAGE_FITS,
        ),
    }
