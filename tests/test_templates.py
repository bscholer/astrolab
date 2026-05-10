"""Template loader: discovers YAML templates and parses to the Pydantic model."""

from __future__ import annotations

import nodes.basic  # noqa: F401  registers nodes referenced by templates
from server.templates import list_templates, load_template


def test_canned_template_parses() -> None:
    templates = list_templates()
    ids = [t.id for t in templates]
    assert "calibrate_register_stack" in ids


def test_load_template_by_id() -> None:
    t = load_template("calibrate_register_stack")
    # Version pin: bump this when the template's `version:` changes. The
    # bump invalidates downstream cache entries (intentional), so a stray
    # increment is something we want to catch in review, not let slip
    # silently.
    assert t.version == 9
    kinds = [n.kind for n in t.nodes]
    # Chain: convert -> calibrate -> resample -> pedestal -> bg_extract ->
    # register -> stack -> auto_crop -> graxpert_bg (linear) ->
    # graxpert_denoise (linear) -> stretch -> starnet_extract (stretched)
    # -> starnet_replace -> starnet_recombine -> crop -> save.
    assert kinds == [
        "convert_lights",
        "calibrate",
        "seq_resample",
        "seq_offset",
        "seq_bg_extract",
        "seq_register",
        "seq_stack",
        "auto_crop",
        "graxpert",
        "graxpert",
        "stretch",
        "starnet_extract",
        "starnet_replace",
        "starnet_recombine",
        "crop",
        "save_image",
    ]
    # Public outputs cover the user-visible save plus a handful of
    # intermediate views the UI surfaces (stack, stretch, AI splits).
    assert t.outputs == {
        "image": "save.image",
        "stacked": "stack.image",
        "stretched": "stretch.image",
        "auto_cropped": "auto_crop.image",
        "starless": "starnet_extract.starless",
        "stars": "starnet_replace.image",
        "recombined": "starnet_recombine.image",
        "cropped": "crop.image",
    }


def test_load_unknown_template_raises() -> None:
    import pytest

    from server.templates import TemplateNotFound

    with pytest.raises(TemplateNotFound):
        load_template("does-not-exist")
