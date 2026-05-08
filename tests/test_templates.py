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
    assert t.version == 2
    kinds = [n.kind for n in t.nodes]
    # Naztronomy-aligned 5-step pipeline.
    assert kinds == [
        "convert_lights",
        "calibrate",
        "seq_bg_extract",
        "seq_register",
        "seq_stack",
    ]
    # Output port wired through to the stacker.
    assert t.outputs == {"image": "stack.image"}


def test_load_unknown_template_raises() -> None:
    import pytest

    from server.templates import TemplateNotFound

    with pytest.raises(TemplateNotFound):
        load_template("does-not-exist")
