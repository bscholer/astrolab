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
    assert t.version == 5
    kinds = [n.kind for n in t.nodes]
    # Phase 3 chain: calibrate -> resample (draft-mode toggle) -> offset
    # (pedestal) -> bg_extract -> register -> stack -> stretch -> save.
    assert kinds == [
        "convert_lights",
        "calibrate",
        "seq_resample",
        "seq_offset",
        "seq_bg_extract",
        "seq_register",
        "seq_stack",
        "stretch",
        "save_image",
    ]
    # Public outputs: a viewable PNG at `image`, plus intermediates so the UI
    # can preview the linear stack and the stretched FITS independently.
    assert t.outputs == {
        "image": "save.image",
        "stacked": "stack.image",
        "stretched": "stretch.image",
    }


def test_load_unknown_template_raises() -> None:
    import pytest

    from server.templates import TemplateNotFound

    with pytest.raises(TemplateNotFound):
        load_template("does-not-exist")
