"""Param-override migration for template version bumps.

When a project moves from template version V_old to V_new, its existing
param_overrides (`dict[node_id, partial_params]`) may not all carry over: a
node may have been removed, a param renamed, a default shifted. Most bumps
are pure node-insertion or reorders and need no per-template logic; the
default behavior here keeps every override whose (node_id, param_key) still
exists in the new template's params_schema, and drops the rest.

For the rare case where a param renames or a node renames, register a
template-specific migration with `register_migration(template_id, fn)`. The
function receives `(old_version, new_version, overrides)` and returns a
remapped overrides dict, which is then run through the default filter below
to drop anything that still doesn't match the new schema.

Keep this tiny. Skip the helper unless a future bump actually needs it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .models import Template
from .registry import lookup as registry_lookup

MigrationFn = Callable[[int, int, dict[str, dict[str, Any]]], dict[str, dict[str, Any]]]

_MIGRATIONS: dict[str, MigrationFn] = {}


def register_migration(template_id: str, fn: MigrationFn) -> None:
    """Register a per-template migration. Idempotent on the (template_id, fn)
    pair; later calls overwrite earlier ones so a template can replace its
    own migrator without restarting the process (useful for tests)."""
    _MIGRATIONS[template_id] = fn


def clear_migrations() -> None:
    """Test helper: wipe the registry between cases."""
    _MIGRATIONS.clear()


def migrate_param_overrides(
    new_template: Template,
    old_version: int,
    overrides: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Carry overrides forward through a template version bump.

    Pipeline:
      1. If a migration is registered for `new_template.id`, run it.
         Free to rename nodes, rename params, drop or add entries.
      2. Filter the result against `new_template`: drop overrides for
         node_ids that no longer exist, and drop param keys that aren't on
         the node's params_schema.

    Returns `(new_overrides, dropped_descriptions)`. The dropped list is one
    human-readable line per (node_id, param_key) that didn't survive; the
    upgrade endpoint surfaces it back to the user.
    """
    fn = _MIGRATIONS.get(new_template.id)
    if fn is not None:
        overrides = fn(old_version, new_template.version, overrides)

    nodes_by_id = {n.id: n for n in new_template.nodes}
    survived: dict[str, dict[str, Any]] = {}
    dropped: list[str] = []
    for node_id, params in overrides.items():
        spec = nodes_by_id.get(node_id)
        if spec is None:
            dropped.append(f"{node_id}: node removed in v{new_template.version}")
            continue
        try:
            node_cls = registry_lookup(spec.kind, spec.variant)
        except Exception:
            dropped.append(
                f"{node_id}: kind '{spec.kind}' is not registered in the runtime"
            )
            continue
        schema_fields = set(node_cls.params_schema.model_fields.keys())
        kept = {k: v for k, v in params.items() if k in schema_fields}
        for k in params:
            if k not in schema_fields:
                dropped.append(
                    f"{node_id}.{k}: param removed in v{new_template.version}"
                )
        if kept:
            survived[node_id] = kept
    return survived, dropped
