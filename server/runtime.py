"""DAG runner.

Resolves a Template + Job into a topologically-sorted execution plan, hashes
each node, skips cached entries, and runs dirty nodes one at a time. Phase 0
is single-threaded and synchronous; concurrency comes when expensive Siril
nodes land and we need a subprocess pool.

Edge resolution is explicit: NodeSpec.inputs maps input_port to
'<source_node_id>.<source_port>'. Implicit chaining sugar (each node's first
input wires to the previous node's primary output) is deferred.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from nodes.base import Node

from .cache import ContentCache
from .canonical import node_hash
from .models import Job, Profile, Ref, RunContext, Template
from .registry import lookup as registry_lookup

ProgressFn = Callable[[float, str], None]
EventFn = Callable[[dict], None]
"""Sink for structured runtime events. Each event is a dict with keys:
{type, node_id?, fraction?, message?, error?}. JobManager wraps this to push
events to WebSocket subscribers."""

log = logging.getLogger("astrolab.runtime")


def _noop_progress(_fraction: float, _message: str) -> None:
    pass


def _noop_events(_event: dict) -> None:
    pass


class RunError(RuntimeError):
    """Raised when a job fails. Wraps the failing node id and the cause."""

    def __init__(self, node_id: str, message: str) -> None:
        super().__init__(f"node {node_id!r} failed: {message}")
        self.node_id = node_id


def _resolved_params(
    node_id: str,
    kind: str,
    raw_template_params: dict,
    profile: Profile | None,
    job: Job,
    params_schema: type,
):
    """Merge profile defaults + template params + job overrides, then validate."""
    merged: dict = {}
    if profile is not None:
        # profile defaults can be keyed by node_id or by kind; node_id wins.
        merged.update(profile.pipeline_defaults.get(kind, {}))
        merged.update(profile.pipeline_defaults.get(node_id, {}))
    merged.update(raw_template_params)
    merged.update(job.param_overrides.get(node_id, {}))
    return params_schema.model_validate(merged)


def _topo_order(template: Template) -> list[str]:
    """Topo sort by explicit edges. Raises on cycles."""
    deps: dict[str, set[str]] = {n.id: set() for n in template.nodes}
    for node in template.nodes:
        for src_ref in node.inputs.values():
            src_node = src_ref.split(".", 1)[0]
            if src_node in deps:
                deps[node.id].add(src_node)
    order: list[str] = []
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(nid: str) -> None:
        if nid in visited:
            return
        if nid in visiting:
            raise RunError(nid, "cycle detected in DAG")
        visiting.add(nid)
        for d in deps[nid]:
            visit(d)
        visiting.remove(nid)
        visited.add(nid)
        order.append(nid)

    for node in template.nodes:
        visit(node.id)
    return order


def run_job(
    template: Template,
    job: Job,
    *,
    cache: ContentCache | None = None,
    profile: Profile | None = None,
    progress: ProgressFn | None = None,
    events: EventFn | None = None,
    force: bool = False,
) -> dict[str, Ref]:
    """Execute a Job and return a map of declared template outputs to Refs.

    Returns the public outputs declared on Template.outputs (mapping
    public_name -> Ref). Internal node Refs are reachable through the cache.

    `events` (optional): structured event sink. Receives one dict per
    node-lifecycle transition (node_started/cached/progress/completed/failed)
    so callers can drive a UI. Use `progress` for a simple fraction-and-string
    callback that doesn't care about node lifecycle.

    `force`: when True, skip the cache lookup at every node but still write
    fresh outputs back into it. Used by the 'Reprocess' affordance so users
    can re-run a job from scratch (eg after a node version bump) without
    invalidating the cache for everyone else.
    """
    cache_obj: ContentCache = cache if cache is not None else ContentCache()
    on_progress: ProgressFn = progress if progress is not None else _noop_progress
    on_event: EventFn = events if events is not None else _noop_events

    by_id = {n.id: n for n in template.nodes}
    order = _topo_order(template)

    # Maps "<node_id>.<port>" -> Ref, growing as we run.
    refs: dict[str, Ref] = dict(job.inputs)

    for nid in order:
        spec = by_id[nid]
        node_cls: type[Node] = registry_lookup(spec.kind, spec.variant)
        node_inst: Node = node_cls()

        # Resolve inputs from prior outputs (or from job.inputs for sources).
        # Optional inputs may be omitted; required inputs must resolve.
        resolved_inputs: dict[str, Ref] = {}
        for in_port in node_cls.inputs:
            src = spec.inputs.get(in_port)
            external_key = f"{nid}.{in_port}"
            if src is None:
                if external_key in refs:
                    resolved_inputs[in_port] = refs[external_key]
                elif in_port in node_cls.optional_inputs:
                    continue
                else:
                    raise RunError(
                        nid,
                        f"input port '{in_port}' has no edge and no external input "
                        f"'{external_key}' in job.inputs",
                    )
            else:
                if src not in refs:
                    raise RunError(nid, f"unresolved input edge '{src}' for port '{in_port}'")
                resolved_inputs[in_port] = refs[src]

        params = _resolved_params(
            nid,
            spec.kind,
            spec.params,
            profile,
            job,
            node_cls.params_schema,
        )

        h = node_hash(
            node_id=node_cls.id,
            node_version=node_cls.version,
            inputs=resolved_inputs,
            params=params,
        )

        on_event({"type": "node_started", "node_id": nid, "kind": spec.kind, "hash": h})

        cached_dir = None if force else cache_obj.lookup(h)
        if cached_dir is not None:
            log.info("cache hit: %s -> %s", nid, h[:12])
            on_progress(0.0, f"{nid}: cached")
            on_event({"type": "node_cached", "node_id": nid, "hash": h})
            for out_port, port_type in node_cls.outputs.items():
                # Outputs land at one of two shapes inside the entry dir:
                #   - file: <out_dir>/<port>.<ext>  (downscale, single PNG)
                #   - dir:  <out_dir>/<port>/...    (convert_lights, sequence)
                # Probe for both. Prefer the directory form when present so a
                # node that writes both leaves the dir as the canonical handle.
                dir_match = cached_dir / out_port
                if dir_match.is_dir():
                    refs[f"{nid}.{out_port}"] = Ref(
                        node_hash=h,
                        port=out_port,
                        path=dir_match,
                        type=port_type,
                    )
                    continue
                matches = sorted(cached_dir.glob(f"{out_port}.*"))
                matches = [m for m in matches if m.name != "_done"]
                if not matches:
                    raise RunError(nid, f"cached entry {h} missing output '{out_port}'")
                refs[f"{nid}.{out_port}"] = Ref(
                    node_hash=h,
                    port=out_port,
                    path=matches[0],
                    type=port_type,
                )
            continue

        # Cache miss (or force): run the node, write outputs into the
        # reserved entry dir. force=True wipes any existing committed entry.
        out_dir = cache_obj.reserve(h, force=force)

        def _node_progress(f: float, m: str, _nid: str = nid) -> None:
            on_progress(f, f"{_nid}: {m}")
            on_event({"type": "node_progress", "node_id": _nid, "fraction": f, "message": m})

        with tempfile.TemporaryDirectory(prefix=f"astrolab-{nid}-") as td:
            ctx = RunContext(
                tmpdir=Path(td),
                progress=_node_progress,
                log=log.getChild(nid),
            )
            try:
                produced = node_inst.run(resolved_inputs, params, ctx, out_dir)
            except Exception as exc:
                shutil.rmtree(out_dir, ignore_errors=True)
                on_event(
                    {"type": "node_failed", "node_id": nid,
                     "error": f"{type(exc).__name__}: {exc}"}
                )
                raise RunError(nid, f"{type(exc).__name__}: {exc}") from exc

        # Validate produced ports match declared outputs and live under out_dir.
        if set(produced.keys()) != set(node_cls.outputs.keys()):
            raise RunError(
                nid,
                f"declared outputs {sorted(node_cls.outputs)} but run() produced "
                f"{sorted(produced)}",
            )
        committed: dict[str, Ref] = {}
        for port, ref in produced.items():
            try:
                ref.path.resolve().relative_to(out_dir.resolve())
            except ValueError as exc:
                raise RunError(
                    nid, f"output '{port}' path {ref.path} escapes cache dir {out_dir}"
                ) from exc
            committed[port] = Ref(
                node_hash=h,
                port=port,
                path=ref.path,
                type=node_cls.outputs[port],
            )

        cache_obj.commit(h, committed)
        for port, ref in committed.items():
            refs[f"{nid}.{port}"] = ref
        on_event({"type": "node_completed", "node_id": nid, "hash": h})

    public: dict[str, Ref] = {}
    for public_name, internal in template.outputs.items():
        if internal not in refs:
            raise RunError(
                "<template>",
                f"declared output '{public_name}' references unresolved '{internal}'",
            )
        public[public_name] = refs[internal]
    return public
