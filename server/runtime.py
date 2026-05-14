"""DAG runner.

Resolves a Template + Job into a topologically-sorted execution plan, hashes
each node, skips cached entries, and runs dirty nodes one at a time. Phase 0
is single-threaded and synchronous; concurrency comes when expensive Siril
nodes land and we need a subprocess pool.

Edge resolution is explicit: NodeSpec.inputs maps input_port to
'<source_node_id>.<source_port>'. Implicit chaining sugar (each node's first
input wires to the previous node's primary output) is deferred.

Lazy upstream re-run: cache misses don't automatically force their producing
node to execute. After computing the hash + cache state for every node, we
walk the DAG backward and mark a miss as `must-run` only if some downstream
consumer is itself a must-run miss, OR if it directly produces a declared
template output. The rest of the misses are skipped — their files would
never be read anyway (every downstream consumer is a cache hit and
rehydrates from manifest without touching upstream files). This shows up
when iterating post-stack: tweaking a stretch parameter no longer forces
register/calibrate to re-execute just because their cached entries were
evicted somewhere along the way.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


def _successors(template: Template) -> dict[str, set[str]]:
    """Reverse adjacency: producer node id -> set of consumer node ids."""
    out: dict[str, set[str]] = {n.id: set() for n in template.nodes}
    for node in template.nodes:
        for src_ref in node.inputs.values():
            src_nid = src_ref.split(".", 1)[0]
            if src_nid in out:
                out[src_nid].add(node.id)
    return out


def _resolve_inputs(
    nid: str,
    spec: Any,
    node_cls: type[Node],
    refs: dict[str, Ref | list[Ref]],
) -> dict[str, Ref | list[Ref]]:
    """Build the inputs dict the node sees, pulling from the current `refs`.

    Same rules as the original inline resolver: declared edges win; ports
    without an edge fall back to job.inputs[<nid>.<port>]; optional inputs
    may be absent. Used both for hashing in the planning pass and for
    actual execution in the run pass.
    """
    resolved: dict[str, Ref | list[Ref]] = {}
    for in_port in node_cls.inputs:
        src = spec.inputs.get(in_port)
        external_key = f"{nid}.{in_port}"
        if src is None:
            if external_key in refs:
                resolved[in_port] = refs[external_key]
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
            resolved[in_port] = refs[src]
    return resolved


@dataclass
class _NodePlan:
    """Planning-time facts about one node, computed before any execution."""

    nid: str
    spec: Any
    node_cls: type[Node]
    h: str
    is_hit: bool
    params: Any


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
    job_id: str | None = None,
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

    `job_id`: when set, each node that actually runs (cache miss + must-run)
    stamps an in-use marker on its output entry dir and every input entry
    dir for the duration of the job. Cache eviction skips marked entries so
    a concurrent cleanup can't rmtree the inputs while a node is mid-read.
    Caller is responsible for clearing the marks at job termination via
    `ContentCache.release_job_marks(job_id)`.
    """
    cache_obj: ContentCache = cache if cache is not None else ContentCache()
    on_progress: ProgressFn = progress if progress is not None else _noop_progress
    on_event: EventFn = events if events is not None else _noop_events
    cancel_event: threading.Event = cancel if cancel is not None else threading.Event()

    by_id = {n.id: n for n in template.nodes}
    order = _topo_order(template)
    successors_of = _successors(template)
    template_output_sources: set[str] = {
        internal.split(".", 1)[0] for internal in template.outputs.values()
    }

    # Maps "<node_id>.<port>" -> Ref or list[Ref], growing as we plan and run.
    # List values are reserved for list-typed ports (ports.LIST_PORTS); scalar
    # outputs from nodes always produce a single Ref.
    refs: dict[str, Ref | list[Ref]] = dict(job.inputs)

    # ---- Pass 1: plan. Compute each node's hash and cache state, and stuff
    # a placeholder Ref into refs so downstream hashing finds something at the
    # expected key. Placeholder paths are filler; for non-external Refs the
    # path doesn't enter the canonical hash (see _encode_ref) and the
    # placeholder gets overwritten by a real Ref in pass 3 for any node that
    # is either a cache hit or a must-run miss.
    plan: dict[str, _NodePlan] = {}
    for nid in order:
        spec = by_id[nid]
        node_cls: type[Node] = registry_lookup(spec.kind, spec.variant)
        resolved_inputs = _resolve_inputs(nid, spec, node_cls, refs)
        params = _resolved_params(
            nid, spec.kind, spec.params, profile, job, node_cls.params_schema,
        )
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
        is_hit = (not force) and cache_obj.is_committed(h)
        plan[nid] = _NodePlan(
            nid=nid, spec=spec, node_cls=node_cls, h=h, is_hit=is_hit, params=params,
        )
        # Placeholder for downstream hash computation. Don't shadow real
        # external inputs (those are keyed `<nid>.<input_port>` and pre-loaded
        # from job.inputs; placeholders are keyed `<nid>.<output_port>`).
        for port, port_type in node_cls.outputs.items():
            key = f"{nid}.{port}"
            if key in refs:
                continue
            refs[key] = Ref(
                node_hash=h,
                port=port,
                path=cache_obj.entry_dir(h) / port,
                type=port_type,
            )

    # ---- Pass 2: must-run set. A miss must run iff it directly produces a
    # template output OR some downstream must-run consumer needs its files.
    # Cache hits never need to run; they rehydrate from manifest.
    must_run: set[str] = set()
    for nid in reversed(order):
        p = plan[nid]
        if p.is_hit:
            continue
        if nid in template_output_sources:
            must_run.add(nid)
            continue
        if any(succ in must_run for succ in successors_of[nid]):
            must_run.add(nid)

    # ---- Pass 3: execute. ----
    for nid in order:
        if cancel_event.is_set():
            raise JobCancelled(nid)
        p = plan[nid]
        spec = p.spec
        node_cls = p.node_cls
        h = p.h
        on_event({"type": "node_started", "node_id": nid, "kind": spec.kind, "hash": h})

        if p.is_hit:
            log.info("cache hit: %s -> %s", nid, h[:12])
            on_progress(0.0, f"{nid}: cached")
            on_event({"type": "node_cached", "node_id": nid, "hash": h})
            cached_dir = cache_obj.entry_dir(h)
            # Preferred path: rehydrate Refs from the per-entry manifest so
            # nodes that don't follow the <port>.<ext> filename convention
            # (eg narrowband_extract writes `r_results_ha.fit` for port `ha`)
            # still cache-hit correctly. Falls back to the old convention-
            # based probe for legacy entries committed before the manifest
            # existed.
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
                    f"cached entry {h} missing output(s) {sorted(missing)!r}",
                )
            continue

        if nid not in must_run:
            # Lazy skip: this node is a cache miss but every downstream
            # consumer is a cache hit, so its files would never actually
            # be read. Leave the placeholder Ref in `refs`; the only thing
            # downstream consumers needed from it was its node_hash, and
            # that was correct in the placeholder.
            log.info("lazy skip: %s -> %s (no must-run consumer)", nid, h[:12])
            on_event({"type": "node_skipped", "node_id": nid, "hash": h})
            continue

        # Must-run miss: reserve, mark in-use, run, commit. Re-resolve
        # inputs against the live `refs` so upstream must-run nodes that
        # already executed in this pass contribute their committed paths,
        # not the placeholder filler.
        resolved_inputs = _resolve_inputs(nid, spec, node_cls, refs)
        out_dir = cache_obj.reserve(h, force=force)

        # Stamp in-use markers on the output dir and every input we're
        # about to read so a concurrent storage cleanup can't rmtree them
        # out from under this node. Markers are released in bulk when the
        # job terminates; per-node release would be over-engineered for
        # the cost (empty marker files).
        if job_id is not None:
            cache_obj.mark_in_use(h, job_id)
            for input_hash in _input_node_hashes(resolved_inputs):
                cache_obj.mark_in_use(input_hash, job_id)

        def _node_progress(f: float, m: str, _nid: str = nid) -> None:
            on_progress(f, f"{_nid}: {m}")
            on_event({"type": "node_progress", "node_id": _nid, "fraction": f, "message": m})

        node_inst: Node = node_cls()
        with tempfile.TemporaryDirectory(prefix=f"astrolab-{nid}-") as td:
            ctx = RunContext(
                tmpdir=Path(td),
                progress=_node_progress,
                log=log.getChild(nid),
                cancel=cancel_event,
            )
            try:
                # Node.run still types `inputs` as dict[str, Ref] because the
                # vast majority of nodes only consume scalar ports. Nodes that
                # declare a list-typed input (ports.LIST_PORTS) receive a
                # list[Ref] at that key and must widen the type locally.
                produced = node_inst.run(
                    resolved_inputs,  # type: ignore[arg-type]
                    p.params,
                    ctx,
                    out_dir,
                )
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
                display_ready=ref.display_ready,
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
        src_nid = internal.split(".", 1)[0]
        p = plan.get(src_nid)
        # Lazy-skip safety check: every template output source must be either
        # a cache hit or a must-run miss (we forced that in pass 2). If we
        # somehow returned a placeholder, the caller would get a Ref pointing
        # at a non-existent file. Fail loudly instead.
        if p is not None and not p.is_hit and src_nid not in must_run:
            raise RunError(
                src_nid,
                f"template output '{public_name}' resolved to a skipped node "
                f"(internal bug in lazy-rerun planner)",
            )
        # Templates only expose scalar outputs as public; list-typed refs come
        # from job.inputs and never end up declared as template.outputs.
        ref = refs[internal]
        if isinstance(ref, list):
            raise RunError(
                "<template>",
                f"declared output '{public_name}' resolved to a list of refs; "
                "list-typed ports cannot be exposed as public outputs",
            )
        public[public_name] = ref
    return public


def _input_node_hashes(resolved: dict[str, Ref | list[Ref]]) -> set[str]:
    """Collect the unique upstream entry hashes a node will read from.

    Used by the in-use marker plumbing so eviction can't rmtree an input
    dir while the consuming node is mid-stream.
    """
    out: set[str] = set()
    for v in resolved.values():
        if isinstance(v, list):
            out.update(r.node_hash for r in v)
        else:
            out.add(v.node_hash)
    return out
