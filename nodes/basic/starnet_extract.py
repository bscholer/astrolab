"""starnet_extract: split a stretched image into starless + stars layers.

Wraps the StarNet++ v2 CLI (https://www.starnetastro.com/). StarNet only
reads/writes 16-bit TIFF, so this node:
  1. converts the input FITS (float32 in [0,1] post-stretch) to a 16-bit TIFF,
  2. runs `starnet++ input.tif starless.tif <stride>` with LD_LIBRARY_PATH
     pointed at the bundled tensorflow .so files,
  3. reads the starless TIFF back into FITS (with the original header),
  4. derives the stars layer as `original - starless` (clipped at 0).

Outputs are both IMAGE_FITS so they slot straight into the rest of the
pipeline. Disabled by default; when off, starless = input and stars = a
zero-filled FITS of the same shape, which keeps recombine's identity
math correct.

Binary location: $ASTROLAB_STARNET_BIN, then ~/tools/starnet/starnet++,
then $PATH. Whatever directory the binary lives in is added to
LD_LIBRARY_PATH so the shipped libtensorflow_framework.so.2 resolves.
"""

from __future__ import annotations

import contextlib
import os
import shutil
from pathlib import Path

import numpy as np
import tifffile
from astropy.io import fits
from pydantic import BaseModel, Field

from nodes.base import Node
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register
from server.subproc import make_line_progress_handler, run_streamed


class StarnetExtractParams(BaseModel):
    enabled: bool = Field(
        default=False,
        description="Off by default. StarNet inference is heavy; flip on "
        "when you want to process the starless layer differently from the "
        "stars (deeper stretch, denoise, color tweak, etc.) before "
        "recombining.",
        json_schema_extra={
            "agent_hint": (
                "Enable when stars are bloated or you want to stretch nebulosity"
                " more aggressively without blowing out star cores."
            ),
        },
    )
    stride: int = Field(
        default=256,
        ge=64,
        le=512,
        description="StarNet++ tile stride in pixels. Smaller = more "
        "overlap = cleaner edges, but slower. 256 is StarNet's default; "
        "drop to 128 for very dense star fields.",
        json_schema_extra={
            "agent_hint": (
                "Smaller stride gives smoother star removal at tile boundaries;"
                " drop to 128 if you see grid artifacts in dense fields."
            ),
        },
    )


@register("starnet_extract")
class StarnetExtractNode(Node[StarnetExtractParams]):
    id = "starnet_extract"
    version = 2  # bumped: now does FITS<->TIFF + LD_LIBRARY_PATH

    cost = "expensive"
    preview_display_ready = True

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

        # FITS -> 16-bit TIFF for StarNet. Track the channel layout so we
        # can write the result back as FITS in the same shape.
        ctx.progress(0.1, "starnet_extract: FITS -> TIFF")
        in_tiff = out_dir_path / "input.tif"
        out_tiff = out_dir_path / "starless.tif"
        layout = _fits_to_tiff_uint16(data, in_tiff)

        # StarNet looks for libtensorflow*.so + the .pb model in its CWD or
        # on LD_LIBRARY_PATH. Set both: cwd to the binary's dir (where the
        # weights live) and LD_LIBRARY_PATH so the dynamic linker resolves.
        starnet_dir = binary.parent.resolve()
        env = os.environ.copy()
        prev_lib = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = f"{starnet_dir}:{prev_lib}" if prev_lib else str(starnet_dir)

        cmd = [
            str(binary),
            str(in_tiff.resolve()),
            str(out_tiff.resolve()),
            str(params.stride),
        ]

        ctx.progress(0.2, "starnet_extract: running starnet++")
        # StarNet++ prints 'Total iterations = N' once and then per-tile
        # 'Iteration: K' lines; the shared parser knows that pattern. Cap
        # the band at 0.85 so the post-process FITS write is room to land.
        on_line = make_line_progress_handler(ctx, low=0.2, high=0.85, prefix="starnet++: ")
        try:
            result = run_streamed(
                cmd,
                cwd=starnet_dir,
                env=env,
                on_line=on_line,
                cancel=ctx.cancel,
                timeout=60 * 60,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"starnet_extract: failed to spawn {binary}: {exc}") from exc
        if result.returncode != 0:
            raise RuntimeError(
                f"starnet_extract: starnet++ exited {result.returncode}\n"
                f"--- cmd ---\n{' '.join(cmd)}\n"
                f"--- stdout (tail) ---\n{result.stdout[-3000:]}"
            )
        if not out_tiff.exists():
            raise RuntimeError(
                f"starnet_extract: starnet++ returned 0 but {out_tiff} is missing.\n"
                f"--- stdout (tail) ---\n{result.stdout[-2000:]}"
            )

        # TIFF starless back into FITS.
        ctx.progress(0.85, "starnet_extract: TIFF -> FITS")
        starless_data = _tiff_uint16_to_fits_array(out_tiff, layout, data.dtype)
        fits.PrimaryHDU(data=starless_data, header=header).writeto(starless_out, overwrite=True)

        # Stars layer: original - starless, clipped at 0 (occasional small
        # negatives from rounding shouldn't propagate).
        ctx.progress(0.95, "starnet_extract: deriving stars layer")
        stars = np.clip(
            data.astype(np.float32) - starless_data.astype(np.float32), 0.0, None
        ).astype(data.dtype, copy=False)
        fits.PrimaryHDU(data=stars, header=header).writeto(stars_out, overwrite=True)

        # Drop the TIFF intermediates; the cache entry only needs the FITS.
        for tmp in (in_tiff, out_tiff):
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()

        ctx.progress(1.0, "starnet_extract: done")
        return _result(starless_out, stars_out)


# ---- channel layout ----------------------------------------------------------


class _Layout:
    """How the source FITS organized its axes; used to round-trip cleanly."""

    __slots__ = ("kind",)

    def __init__(self, kind: str) -> None:
        # 'mono' (HxW), 'chw' (CxHxW), or 'hwc' (HxWxC).
        self.kind = kind


def _fits_to_tiff_uint16(data: np.ndarray, dst: Path) -> _Layout:
    """Write `data` (post-stretch float, mostly in [0,1]) as a 16-bit TIFF.

    Returns a Layout token so the inverse function can rebuild the FITS in
    the same channel layout.
    """
    arr = data.astype(np.float32, copy=False)
    arr = np.clip(arr, 0.0, 1.0)
    if arr.ndim == 2:
        layout = _Layout("mono")
    elif arr.ndim == 3 and arr.shape[0] in (3, 4):
        layout = _Layout("chw")
        arr = np.moveaxis(arr, 0, -1)  # CHW -> HWC for tifffile
    elif arr.ndim == 3 and arr.shape[-1] in (3, 4):
        layout = _Layout("hwc")
    else:
        raise RuntimeError(f"starnet_extract: unsupported FITS shape {arr.shape}")
    u16 = (arr * 65535.0 + 0.5).astype(np.uint16)
    tifffile.imwrite(dst, u16)
    return layout


def _tiff_uint16_to_fits_array(src: Path, layout: _Layout, dtype: np.dtype) -> np.ndarray:
    """Read a 16-bit TIFF and reshape it to match the original FITS layout."""
    arr = tifffile.imread(src)
    out = arr.astype(np.float32) / 65535.0
    if layout.kind == "chw":
        # tifffile stores RGB as HWC; flip back to CHW for FITS.
        out = np.moveaxis(out, -1, 0)
    elif layout.kind == "mono" and out.ndim == 3:
        # If StarNet decided to write a 3-channel TIFF for a mono input
        # (rare, but possible), collapse via the average.
        out = out.mean(axis=-1)
    return out.astype(dtype, copy=False)


# ---- io / locate -------------------------------------------------------------


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
            display_ready=True,
        ),
        "stars": Ref(
            node_hash="",
            port="stars",
            path=stars,
            type=PortType.IMAGE_FITS,
            display_ready=True,
        ),
    }
