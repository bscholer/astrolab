"""Template loader: discovers YAML templates and parses to the Pydantic model."""

from __future__ import annotations

import nodes.basic  # noqa: F401  registers nodes referenced by templates
from server.templates import list_templates, load_template


def test_canned_template_parses() -> None:
    templates = list_templates()
    ids = [t.id for t in templates]
    assert "calibrate_register_stack" in ids
    assert "calibrate_register_stack_narrowband" in ids


def test_load_template_by_id() -> None:
    t = load_template("calibrate_register_stack")
    # Version pin: bump this when the template's `version:` changes. The
    # bump invalidates downstream cache entries (intentional), so a stray
    # increment is something we want to catch in review, not let slip
    # silently.
    assert t.version == 16
    kinds = [n.kind for n in t.nodes]
    # Chain: convert -> calibrate -> resample -> pedestal -> bg_extract ->
    # register -> stack -> crop (auto-trims registration border on first run,
    # user box on subsequent) -> graxpert_bg (linear) ->
    # graxpert_denoise (linear) -> color_balance (SCNR green, linear) ->
    # auto_bp_shift (linear pre-stretch BP shift) -> stretch ->
    # starnet_extract (stretched) -> starnet_replace -> starnet_recombine ->
    # save. color_balance lives in the linear domain so the autostretch
    # curve operates on green-neutralized data instead of baking the cast
    # into a permanent stained look post-stretch.
    assert kinds == [
        "convert_lights",
        "calibrate",
        "seq_resample",
        "seq_offset",
        "seq_bg_extract",
        "seq_register",
        "seq_stack",
        "crop",
        "graxpert",
        "graxpert",
        "color_balance",
        "auto_bp_shift",
        "stretch",
        "starnet_extract",
        "starnet_replace",
        "starnet_recombine",
        "save_image",
    ]
    # Public outputs cover the user-visible save plus a handful of
    # intermediate views the UI surfaces (stack, stretch, AI splits).
    assert t.outputs == {
        "image": "save.image",
        "stacked": "stack.image",
        "stretched": "stretch.image",
        "cropped": "crop.image",
        "starless": "starnet_extract.starless",
        "stars": "starnet_replace.image",
        "recombined": "starnet_recombine.image",
    }


def test_load_narrowband_template_by_id() -> None:
    t = load_template("calibrate_register_stack_narrowband")
    # Version pin: bump alongside the YAML's version:. The bump
    # invalidates the cache for any in-flight projects on this template,
    # which is the correct behavior when topology changes.
    assert t.version == 3
    kinds = [n.kind for n in t.nodes]
    # Narrowband shuffle: ... -> narrowband_compose -> crop (auto-trims
    # registration border) -> graxpert_bg -> graxpert_denoise -> stretch ->
    # starnet_* -> save. Same crop-before-stretch rationale as the OSC RGB
    # template.
    assert kinds == [
        "convert_lights",
        "calibrate",
        "seq_resample",
        "narrowband_extract",
        "narrowband_compose",
        "crop",
        "graxpert",
        "graxpert",
        "stretch",
        "starnet_extract",
        "starnet_replace",
        "starnet_recombine",
        "save_image",
    ]


def test_template_edges_resolve() -> None:
    """Every node's inputs.<port> must point at an existing producer node."""
    for template_id in (
        "calibrate_register_stack",
        "calibrate_register_stack_narrowband",
    ):
        t = load_template(template_id)
        ids = {n.id for n in t.nodes}
        for n in t.nodes:
            for port, src in n.inputs.items():
                src_id = src.split(".", 1)[0]
                assert src_id in ids, (
                    f"{template_id}: node {n.id}.{port} references "
                    f"unknown producer {src_id!r}"
                )


def test_crop_runs_before_stretch() -> None:
    """The reordering rule: crop sits right after the stack/compose step
    (handling both auto-trim and user composition), and stretch comes after
    the linear-domain chain. Pin both so a future edit that accidentally
    re-introduces stretch-before-crop fails loudly."""
    osc = load_template("calibrate_register_stack")
    by_id = {n.id: n for n in osc.nodes}
    # OSC: crop is now the first node after stack (no separate auto_crop).
    assert by_id["crop"].inputs["image"] == "stack.image"
    assert by_id["graxpert_bg"].inputs["image"] == "crop.image"
    assert by_id["graxpert_denoise"].inputs["image"] == "graxpert_bg.image"
    assert by_id["color_balance"].inputs["image"] == "graxpert_denoise.image"
    assert by_id["auto_bp_shift"].inputs["image"] == "color_balance.image"
    assert by_id["stretch"].inputs["image"] == "auto_bp_shift.image"
    assert by_id["starnet_extract"].inputs["image"] == "stretch.image"

    nb = load_template("calibrate_register_stack_narrowband")
    by_id = {n.id: n for n in nb.nodes}
    # Narrowband: crop is right after narrowband_compose (no separate auto_crop).
    assert by_id["crop"].inputs["image"] == "narrowband_compose.image"
    assert by_id["graxpert_bg"].inputs["image"] == "crop.image"
    assert by_id["graxpert_denoise"].inputs["image"] == "graxpert_bg.image"
    assert by_id["stretch"].inputs["image"] == "graxpert_denoise.image"
    assert by_id["starnet_extract"].inputs["image"] == "stretch.image"


def test_load_unknown_template_raises() -> None:
    import pytest

    from server.templates import TemplateNotFound

    with pytest.raises(TemplateNotFound):
        load_template("does-not-exist")
