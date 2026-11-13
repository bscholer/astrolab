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
    framing: Literal["max", "min", "first"] = Field(
        default="max",
        description="Output frame: 'max' keeps the union of all nights, 'min' only the "
        "area every night covers, 'first' the first night's frame.",
        json_schema_extra={"ui_section": "advanced"},
    )


@register("combine_stacks")
class CombineStacksNode(Node[CombineStacksParams]):
    id = "combine_stacks"
    version = 1
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

        shutil.rmtree(work, ignore_errors=True)
        ctx.progress(1.0, f"combine_stacks: combined {len(images)} stacks")
        return {"image": image_ref(out_image)}
