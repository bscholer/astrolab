"""Unit tests for template param-override migration."""

from __future__ import annotations

import nodes.basic  # noqa: F401  registers downscale
from server.models import NodeSpec, Template
from server.template_migrations import (
    clear_migrations,
    migrate_param_overrides,
    register_migration,
)


def _template(version: int, node_ids: list[str]) -> Template:
    return Template(
        id="t",
        version=version,
        nodes=[
            NodeSpec(id=nid, kind="downscale", params={"target_size_px": 512})
            for nid in node_ids
        ],
        outputs={"image": f"{node_ids[-1]}.image"},
    )


def test_keeps_overrides_for_existing_nodes_and_params():
    clear_migrations()
    new = _template(2, ["a", "b"])
    overrides = {"a": {"target_size_px": 256}, "b": {"target_size_px": 128}}
    out, dropped = migrate_param_overrides(new, old_version=1, overrides=overrides)
    assert out == overrides
    assert dropped == []


def test_drops_overrides_for_removed_nodes():
    clear_migrations()
    new = _template(2, ["a"])  # 'b' was removed in v2
    overrides = {"a": {"target_size_px": 256}, "b": {"target_size_px": 128}}
    out, dropped = migrate_param_overrides(new, old_version=1, overrides=overrides)
    assert out == {"a": {"target_size_px": 256}}
    assert len(dropped) == 1
    assert "b" in dropped[0]


def test_drops_overrides_for_removed_param_keys():
    clear_migrations()
    new = _template(2, ["a"])
    overrides = {"a": {"target_size_px": 256, "nonexistent": "x"}}
    out, dropped = migrate_param_overrides(new, old_version=1, overrides=overrides)
    assert out == {"a": {"target_size_px": 256}}
    assert len(dropped) == 1
    assert "nonexistent" in dropped[0]


def test_registered_migration_runs_before_filter():
    clear_migrations()
    new = _template(2, ["renamed"])  # node 'a' renamed to 'renamed' in v2

    def rename(old: int, new_v: int, overs: dict) -> dict:
        return {"renamed" if nid == "a" else nid: p for nid, p in overs.items()}

    register_migration("t", rename)
    overrides = {"a": {"target_size_px": 256}}
    out, dropped = migrate_param_overrides(new, old_version=1, overrides=overrides)
    assert out == {"renamed": {"target_size_px": 256}}
    assert dropped == []
    clear_migrations()


def test_empty_overrides_short_circuit():
    clear_migrations()
    new = _template(2, ["a"])
    out, dropped = migrate_param_overrides(new, old_version=1, overrides={})
    assert out == {}
    assert dropped == []
