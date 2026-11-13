"""Per-session templates: nodes copied once per night, joined by a list port."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from nodes.base import Node
from server.cache import ContentCache
from server.models import Job, NodeSpec, Ref, RunContext, Template
from server.ports import PortType
from server.registry import register
from server.runtime import expand_per_session, run_job


class _P(BaseModel):
    tag: str = "x"


@register("__test_night__")
class _Night(Node[_P]):
    id = "__test_night__"
    version = 1
    inputs = {"lights": PortType.SEQUENCE_FITS}
    outputs = {"image": PortType.IMAGE_FITS}
    params_schema = _P
    runs = 0

    def run(self, inputs, params, ctx: RunContext, out_dir) -> dict[str, Ref]:
        type(self).runs += 1
        out = Path(out_dir) / "image.fit"
        out.write_text(f"{inputs['lights'].path.name}:{params.tag}")
        return {"image": Ref(node_hash="", port="image", path=out, type=PortType.IMAGE_FITS)}


@register("__test_join__")
class _Join(Node[_P]):
    id = "__test_join__"
    version = 1
    inputs = {"images": PortType.IMAGE_FITS_LIST}
    outputs = {"image": PortType.IMAGE_FITS}
    params_schema = _P

    def run(self, inputs, params, ctx: RunContext, out_dir) -> dict[str, Ref]:
        out = Path(out_dir) / "image.fit"
        out.write_text("+".join(sorted(r.path.read_text() for r in inputs["images"])))
        return {"image": Ref(node_hash="", port="image", path=out, type=PortType.IMAGE_FITS)}


TEMPLATE = Template(
    id="t",
    nodes=[
        NodeSpec(id="night", kind="__test_night__", per_session=True),
        NodeSpec(id="join", kind="__test_join__", inputs={"images": "night.image"}),
    ],
    outputs={"image": "join.image"},
)


def _job(tmp_path: Path, keys: list[str], overrides: dict | None = None) -> Job:
    inputs = {}
    for k in keys:
        d = tmp_path / f"night_{k}"
        d.mkdir(exist_ok=True)
        inputs[f"night@{k}.lights"] = Ref(
            node_hash="ext", port="lights", path=d, type=PortType.SEQUENCE_FITS
        )
    return Job(template_id="t", template_version=1, inputs=inputs, param_overrides=overrides or {})


def test_expansion_copies_per_session_nodes_and_lists_their_outputs(tmp_path: Path) -> None:
    out = expand_per_session(TEMPLATE, _job(tmp_path, ["2", "10"]))
    assert [n.id for n in out.nodes] == ["night@2", "night@10", "join"]
    assert out.nodes[2].inputs == {"images": ["night@2.image", "night@10.image"]}


def test_nights_run_once_and_a_new_night_reuses_the_others(tmp_path: Path) -> None:
    cache = ContentCache(root=tmp_path / "cache")
    _Night.runs = 0
    # The template node's override applies to every copy; a copy's own wins.
    job = _job(tmp_path, ["1", "2"], {"night": {"tag": "a"}, "night@2": {"tag": "b"}})
    out = run_job(TEMPLATE, job, cache=cache)
    assert out["image"].path.read_text() == "night_1:a+night_2:b"
    assert _Night.runs == 2

    job3 = _job(tmp_path, ["1", "2", "3"], {"night": {"tag": "a"}, "night@2": {"tag": "b"}})
    out = run_job(TEMPLATE, job3, cache=cache)
    assert out["image"].path.read_text() == "night_1:a+night_2:b+night_3:a"
    assert _Night.runs == 3  # only the new night ran
