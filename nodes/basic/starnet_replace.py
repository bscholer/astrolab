"""starnet_replace: regenerate the stars layer with Siril findstar+synthstar.

After StarNet has split an image into starless + stars, the 'stars' layer
often carries chunky edge artifacts and incomplete halos. This node
reconstructs the original (starless + stars), runs Siril's `findstar` to
locate every star, then `synthstar` to render perfectly Gaussian PSFs at
those positions. The new stars layer goes downstream to recombine.

Inputs : starless (IMAGE_FITS), stars (IMAGE_FITS)
Output : image (IMAGE_FITS) - the regenerated stars layer

Disabled by default; when off the node passes the input `stars` through
unchanged so the recombine math stays consistent.

We rely on Siril 1.4 already being on the host (the project's main
pipeline does too). No new external dependency.
"""

from __future__ import annotations

import contextlib
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


class StarnetReplaceParams(BaseModel):
    enabled: bool = Field(
        default=False,
        description="Off by default. Flip on when StarNet's stars layer "
        "shows obvious halos / edge artifacts and you'd rather have clean "
        "synthetic Gaussian stars in the final composite.",
    )
    profile: str = Field(
        default="gaussian",
        description="PSF model passed to synthstar. 'gaussian' is the "
        "robust default. Siril also accepts 'moffat' for telescopes whose "
        "real PSF has heavier tails.",
        json_schema_extra={"ui_section": "advanced"},
    )


@register("starnet_replace")
class StarnetReplaceNode(Node[StarnetReplaceParams]):
    id = "starnet_replace"
    version = 1
    cost = "medium"
    uses_siril = True
    preview_display_ready = True

    inputs = {
        "starless": PortType.IMAGE_FITS,
        "stars": PortType.IMAGE_FITS,
    }
    outputs = {"image": PortType.IMAGE_FITS}
    params_schema = StarnetReplaceParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: StarnetReplaceParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out_dir_path = Path(out_dir)  # type: ignore[arg-type]
        starless_src = inputs["starless"].path
        stars_src = inputs["stars"].path
        if not starless_src.exists():
            raise RuntimeError(f"starnet_replace: starless input does not exist: {starless_src}")
        if not stars_src.exists():
            raise RuntimeError(f"starnet_replace: stars input does not exist: {stars_src}")

        out_image = out_dir_path / "image.fit"

        if not params.enabled:
            ctx.progress(0.9, "starnet_replace: disabled — passing through stars")
            _passthrough(stars_src, out_image)
            ctx.progress(1.0, "starnet_replace: pass-through")
            return _result(out_image)

        # Reconstruct the original (starless + stars) so findstar has a real
        # image with stars to detect. We do this in numpy rather than using
        # Siril's `add` command to keep header handling explicit.
        ctx.progress(0.1, "starnet_replace: reconstructing original")
        starless_data, header = _read_fits(starless_src)
        stars_data, _ = _read_fits(stars_src)
        if starless_data.shape != stars_data.shape:
            raise RuntimeError(
                f"starnet_replace: shape mismatch starless={starless_data.shape} "
                f"stars={stars_data.shape}"
            )
        recon = (starless_data.astype(np.float32) + stars_data.astype(np.float32)).astype(
            starless_data.dtype, copy=False
        )
        recon_path = out_dir_path / "_recon.fit"
        fits.PrimaryHDU(data=recon, header=header).writeto(recon_path, overwrite=True)

        # Synthstar: load reconstructed -> findstar -> synthstar -> save.
        # Siril writes the synth result as a full image (synthetic stars on
        # the same starless background), so we subtract starless after to
        # get a stars-only layer.
        synth_full = out_dir_path / "_synth_full.fit"
        synth_stem = synth_full.with_suffix("").name

        ctx.progress(0.3, "starnet_replace: running siril findstar+synthstar")
        commands = [
            f"cd {quote(out_dir_path.resolve())}",
            f"load {quote(recon_path.resolve())}",
            "findstar",
            "synthstar",
            f"save {quote(synth_stem)}",
        ]
        runtime = SirilRuntime()
        result = runtime.run(
            commands,
            working_dir=out_dir_path,
            on_log=make_progress_handler(ctx, low=0.3, high=0.85),
            cancel=ctx.cancel,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"starnet_replace: siril exited {result.returncode}\n"
                f"--- ssf ---\n{result.ssf}\n"
                f"--- stdout (tail) ---\n{result.stdout[-3000:]}\n"
                f"--- stderr ---\n{result.stderr[-1500:]}"
            )
        if not synth_full.exists():
            raise RuntimeError(
                f"starnet_replace: siril returned 0 but {synth_full} missing.\n"
                f"--- stdout (tail) ---\n{result.stdout[-2000:]}"
            )

        # Pull the synthetic-stars layer out: synth_full is starless + new
        # stars; subtracting starless yields the new stars in isolation.
        ctx.progress(0.9, "starnet_replace: deriving synthetic stars layer")
        synth_full_data, synth_header = _read_fits(synth_full)
        new_stars = np.clip(
            synth_full_data.astype(np.float32) - starless_data.astype(np.float32),
            0.0,
            None,
        ).astype(starless_data.dtype, copy=False)
        fits.PrimaryHDU(data=new_stars, header=synth_header).writeto(out_image, overwrite=True)

        # Strip intermediates so the cache entry only holds the output.
        for tmp in (recon_path, synth_full):
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()

        ctx.progress(1.0, "starnet_replace: done")
        return _result(out_image)


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
        raise RuntimeError(f"starnet_replace: no image data in {src}")
    return data, header


def _passthrough(src: Path, dst: Path) -> None:
    data, header = _read_fits(src)
    fits.PrimaryHDU(data=data, header=header).writeto(dst, overwrite=True)


def _result(out_image: Path) -> dict[str, Ref]:
    return {"image": image_ref(out_image, display_ready=True)}
