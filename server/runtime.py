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
import threading
from collections.abc import Callable
from pathlib import Path

from nodes.base import Node

from .cache import ContentCache
from .canonical import node_hash
from .models import Job, Profile, Ref, RunContext, Template
from .registry import lookup as registry_lookup
from .siril import get_siril_version

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


def _locate_port_artifact(cached_dir: Path, port: str) -> Path | None:
    """Convention-based fallback for legacy cache entries with no manifest.

    Outputs land at one of two shapes inside the entry dir:
      - dir:  <entry>/<port>/...    (sequence outputs like convert_lights)
      - file: <entry>/<port>.<ext>  (single artifact like downscale)
    Prefer the directory form so a node that writes both leaves the dir
    as the canonical handle. Returns None when neither matches; the caller
    treats that as a corrupt/incomplete cache entry.
    """
    dir_match = cached_dir / port
    if dir_match.is_dir():
        return dir_match
    matches = sorted(p for p in cached_dir.glob(f"{port}.*") if p.name != "_done")
    return matches[0] if matches else None


class RunError(RuntimeError):
    """Raised when a job fails. Wraps the failing node id and the cause."""

    def __init__(self, node_id: str, message: str) -> None:
        super().__init__(f"node {node_id!r} failed: {message}")
        self.node_id = node_id


class JobCancelled(RuntimeError):
    """Raised when a job is cooperatively cancelled mid-run.

    Distinct from RunError so JobManager can mark the record as
    'interrupted' (resumable) instead of 'failed' (broken). The payload
    carries the node id where cancellation took effect, for telemetry.
    """

    def __init__(self, node_id: str | None = None) -> None:
        super().__init__(
            f"cancelled at node {node_id!r}" if node_id else "cancelled"
        )
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
    cancel: threading.Event | None = None,
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

    `cancel`: a cooperative cancellation Event. Checked between nodes and
    propagated into each node's RunContext so subprocess-wrapping nodes can
    abort mid-run. Set this when the job is superseded (eg the user tweaked
    a slider mid-pipeline) to free the worker for the new job.
    """
    cache_obj: ContentCache = cache if cache is not None else ContentCache()
    on_progress: ProgressFn = progress if progress is not None else _noop_progress
    on_event: EventFn = events if events is not None else _noop_events
    cancel_event: threading.Event = cancel if cancel is not None else threading.Event()

    by_id = {n.id: n for n in template.nodes}
    order = _topo_order(template)

    # Maps "<node_id>.<port>" -> Ref, growing as we run.
    refs: dict[str, Ref] = dict(job.inputs)

    for nid in order:
        if cancel_event.is_set():
            raise JobCancelled(nid)
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

        # Mix in the Siril version for nodes that shell out to Siril so that
        # upgrading Siril invalidates their cached outputs without a manual
        # cache wipe. Non-Siril nodes get no extra key so their hashes are
        # unaffected by Siril installs or upgrades.
        extra: dict[str, str] | None = None
        if node_cls.uses_siril:
            extra = {"siril_version": get_siril_version()}
        h = node_hash(
            node_id=node_cls.id,
            node_version=node_cls.version,
            inputs=resolved_inputs,
            params=params,
            extra_keys=extra,
        )

        on_event({"type": "node_started", "node_id": nid, "kind": spec.kind, "hash": h})

        cached_dir = None if force else cache_obj.lookup(h)
        if cached_dir is not None:
            log.info("cache hit: %s -> %s", nid, h[:12])
            on_progress(0.0, f"{nid}: cached")
            on_event({"type": "node_cached", "node_id": nid, "hash": h})
            # Preferred path: rehydrate Refs from the per-entry manifest so
            # nodes that don't follow the <port>.<ext> filename convention
            # (eg narrowband_extract writes `r_results_ha.fit` for port
            # `ha`) still cache-hit correctly. Falls back to the old
            # convention-based probe for legacy entries committed before
            # the manifest existed.
            manifest = cache_obj.load_outputs(h)
            missing: list[str] = []
            for out_port, port_type in node_cls.outputs.items():
                if manifest is not None and out_port in manifest:
                    entry = manifest[out_port]
                    if not entry.path.exists():
                        missing.append(out_port)
                        continue
                    refs[f"{nid}.{out_port}"] = Ref(
                        node_hash=h,
                        port=out_port,
                        path=entry.path,
                        type=port_type,
                    )
                    continue
                located = _locate_port_artifact(cached_dir, out_port)
                if located is None:
                    missing.append(out_port)
                    continue
                refs[f"{nid}.{out_port}"] = Ref(
                    node_hash=h,
                    port=out_port,
                    path=located,
                    type=port_type,
                )
            if missing:
                raise RunError(
                    nid,
                    f"cached entry {h} missing output(s) "
                    f"{sorted(missing)!r}",
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
                cancel=cancel_event,
            )
            try:
                produced = node_inst.run(resolved_inputs, params, ctx, out_dir)
            except JobCancelled:
                shutil.rmtree(out_dir, ignore_errors=True)
                on_event({"type": "node_failed", "node_id": nid, "error": "cancelled"})
                raise
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
