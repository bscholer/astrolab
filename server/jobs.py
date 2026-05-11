"""Job orchestration, split across two processes.

JobManager runs inside the FastAPI process. It only writes to the catalog
DB - no threads, no executor, no in-memory state. submit() inserts a queued
row; cancel() flips a column the worker polls; get/list/get_events read
straight from disk.

JobWorker runs inside the dedicated worker process (`python -m
server.worker`). It claims queued rows, runs them through the runtime, and
emits progress events back to the same catalog DB. The API surfaces those
events to the UI by tailing job_events by seq.

The split exists so a deploy-restart of astrolab-api doesn't SIGTERM
in-flight Siril subprocesses. Real symptom that forced the split: a 3-
session render of C 34 (8c7c9db0-...) died at ~72% of seq_resample because
a PR merge restarted the API mid-job.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

from .cache import ContentCache
from .catalog.db import connect as open_catalog_db
from .models import Job, Ref, Template
from .ports import PortType
from .runtime import JobCancelled, RunError, run_job

log = logging.getLogger("astrolab.jobs")

JobStatus = Literal["queued", "running", "completed", "failed", "interrupted"]
EventType = Literal[
    "job_queued",
    "job_started",
    "node_started",
    "node_progress",
    "node_cached",
    "node_completed",
    "node_failed",
    "job_completed",
    "job_failed",
    "job_interrupted",
]


@dataclass
class JobEvent:
    type: EventType
    timestamp: str
    node_id: str | None = None
    fraction: float | None = None
    message: str | None = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.type, "timestamp": self.timestamp}
        if self.node_id is not None:
            d["node_id"] = self.node_id
        if self.fraction is not None:
            d["fraction"] = self.fraction
        if self.message is not None:
            d["message"] = self.message
        if self.error is not None:
            d["error"] = self.error
        if self.extra:
            d.update(self.extra)
        return d


@dataclass
class JobRecord:
    id: str
    status: JobStatus
    template: Template
    job: Job
    submitted_at: str
    started_at: str | None = None
    finished_at: str | None = None
    outputs: dict[str, Ref] | None = None
    error: str | None = None
    force: bool = False
    """Whether this submission should bypass the cache (set on Reprocess).
    Now persisted on the jobs row so the API can set it from submit() and
    the worker can read it back when it claims the job."""
    node_hashes: dict[str, str] = field(default_factory=dict)
    """Map of node_id -> cache hash for every node this job touched
    (committed or hit). Persisted on terminal events so the storage layer
    can map cache entries back to the projects that own them. Keyed by
    node_id (not list position) so out-of-topo-order template authoring
    still attributes cost class and last-used timestamps to the right
    node."""
    events: list[JobEvent] = field(default_factory=list)
    """Optional materialized event list. Production `get()` does NOT
    populate this (use `get_events()` instead); kept on the dataclass for
    legacy test code that synthesises records with inline events."""

    def public_dict(self, *, include_template: bool = False) -> dict[str, Any]:
        """Serializable summary for GET /api/jobs[/{id}]."""
        d: dict[str, Any] = {
            "id": self.id,
            "status": self.status,
            "template_id": self.template.id,
            "template_version": self.template.version,
            "submitted_at": self.submitted_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "outputs": (
                {k: {"path": str(v.path), "type": str(v.type), "node_hash": v.node_hash}
                 for k, v in self.outputs.items()}
                if self.outputs is not None
                else None
            ),
        }
        if include_template:
            # Pydantic model_dump gives the canonical Template JSON shape that
            # the UI can graph without further translation. We tack each
            # node's declared output ports onto the spec dict so the UI can
            # route preview requests to the right port name without keeping
            # a parallel registry of conventions (the jobs page used to guess
            # `image` for everything, which 404s for narrowband_extract,
            # starnet_extract, and the seq_* nodes that emit `sequence`).
            tpl = self.template.model_dump(mode="json")
            for node_dict, spec in zip(tpl.get("nodes", []), self.template.nodes, strict=False):
                node_dict["outputs"] = _node_outputs(spec.kind, spec.variant)
            d["template"] = tpl
        return d


def _node_outputs(kind: str, variant: str | None) -> dict[str, str]:
    """Return the registered Node's `outputs` map as {port: type_string}.

    Empty dict when the registry doesn't recognise the (kind, variant) pair.
    Keeps the API total when a persisted job references an obsolete node.
    """
    from .registry import lookup as registry_lookup

    try:
        cls = registry_lookup(kind, variant)
    except KeyError:
        return {}
    return {port: str(port_type) for port, port_type in cls.outputs.items()}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _outputs_to_json(outputs: dict[str, Ref] | None) -> str | None:
    if outputs is None:
        return None
    return json.dumps(
        {
            k: {"path": str(v.path), "type": str(v.type), "node_hash": v.node_hash, "port": v.port}
            for k, v in outputs.items()
        }
    )


def _outputs_from_json(blob: str | None) -> dict[str, Ref] | None:
    if blob is None:
        return None
    raw = json.loads(blob)
    return {
        k: Ref(node_hash=v["node_hash"], port=v["port"], path=Path(v["path"]),
               type=PortType(v["type"]))
        for k, v in raw.items()
    }


def _node_hashes_from_json(blob: str | None, template: Template) -> dict[str, str]:
    """Decode the persisted node_hashes payload.

    Current shape is a dict {node_id: hash}. Older records were stored as a
    bare list of hashes; storage was already indexing those by
    template.nodes[i], so we backfill on read by pairing each legacy hash
    with the same template node we'd have hit before. New writes use the
    dict shape and stop being sensitive to YAML-vs-topo declaration order.
    """
    if not blob:
        return {}
    try:
        raw = json.loads(blob)
    except (ValueError, TypeError):
        return {}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}
    if isinstance(raw, list):
        out: dict[str, str] = {}
        for i, h in enumerate(raw):
            if not isinstance(h, str):
                continue
            if i < len(template.nodes):
                out[template.nodes[i].id] = h
        return out
    return {}


# --------------------------------------------------------------------- helpers


def _persist_event(
    conn: sqlite3.Connection,
    job_id: str,
    event: JobEvent,
    seq: int | None = None,
) -> int:
    """Insert a row in `job_events`. Returns the assigned seq.

    When `seq` is None we assign it as MAX(seq)+1 atomically using SQL so
    two writers can't collide on the (job_id, seq) index. job_queued at
    submit() time passes seq=0 explicitly so the first event is
    deterministic.
    """
    if seq is None:
        row = conn.execute(
            """
            INSERT INTO job_events
                (job_id, seq, type, timestamp, node_id, fraction, message, error, extra_json)
            VALUES (
                ?,
                COALESCE((SELECT MAX(seq) FROM job_events WHERE job_id = ?), -1) + 1,
                ?, ?, ?, ?, ?, ?, ?
            )
            RETURNING seq
            """,
            (
                job_id,
                job_id,
                event.type,
                event.timestamp,
                event.node_id,
                event.fraction,
                event.message,
                event.error,
                json.dumps(event.extra) if event.extra else None,
            ),
        ).fetchone()
        return int(row[0])
    conn.execute(
        """
        INSERT INTO job_events
            (job_id, seq, type, timestamp, node_id, fraction, message, error, extra_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            seq,
            event.type,
            event.timestamp,
            event.node_id,
            event.fraction,
            event.message,
            event.error,
            json.dumps(event.extra) if event.extra else None,
        ),
    )
    return seq


def _row_to_record(row: sqlite3.Row) -> JobRecord:
    template = Template.model_validate(json.loads(row["template_json"]))
    job = Job.model_validate(json.loads(row["job_json"]))
    outputs = _outputs_from_json(row["outputs_json"])
    node_hashes = _node_hashes_from_json(row["node_hashes_json"], template)
    # `force` is migration 12; older rows opened by tests pointing at a
    # pre-migrated DB may not have the column. sqlite3.Row's __contains__
    # iterates values (not column names), so .keys() is the right probe;
    # SIM118 here would be wrong.
    force = bool(row["force"]) if "force" in row.keys() else False  # noqa: SIM118
    return JobRecord(
        id=row["id"],
        status=cast(JobStatus, row["status"]),
        template=template,
        job=job,
        submitted_at=row["submitted_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        outputs=outputs,
        error=row["error"],
        force=force,
        node_hashes=node_hashes,
    )


def _load_events(
    conn: sqlite3.Connection, job_id: str, *, after_seq: int = -1
) -> list[JobEvent]:
    rows = conn.execute(
        "SELECT * FROM job_events WHERE job_id = ? AND seq > ? ORDER BY seq ASC",
        (job_id, after_seq),
    ).fetchall()
    out: list[JobEvent] = []
    for er in rows:
        out.append(
            JobEvent(
                type=cast(EventType, er["type"]),
                timestamp=er["timestamp"],
                node_id=er["node_id"],
                fraction=er["fraction"],
                message=er["message"],
                error=er["error"],
                extra=json.loads(er["extra_json"]) if er["extra_json"] else {},
            )
        )
    return out


def _pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ----------------------------------------------------------------- JobManager


class JobManager:
    """API-side surface. DB-only - no threads, no in-memory record cache.

    submit() inserts a queued row and a seq=0 job_queued event. cancel()
    flips the cancel_requested column; the worker's monitor thread polls
    that column and forwards to the runtime cancel token. get/list/
    get_events all read from disk.
    """

    def __init__(
        self,
        cache: ContentCache | None = None,
        *,
        db_path: Path | None = None,
    ) -> None:
        self._cache = cache if cache is not None else ContentCache()
        self._db_path = db_path

    @property
    def cache(self) -> ContentCache:
        return self._cache

    @property
    def db_path(self) -> Path | None:
        return self._db_path

    def rehydrate(self) -> None:
        """No-op on the API side.

        The worker is responsible for reclaiming stale 'running' rows from
        a previous worker crash; the API no longer holds in-memory state to
        rebuild. Kept for compatibility with the lifespan startup hook.
        """
        return

    def reset_for_tests(self, *, db_path: Path | None = None) -> None:
        """Repoint at a fresh DB.

        There's no in-process executor to drain anymore; the test fixture
        owns the worker thread lifecycle via the `background_worker`
        helper.
        """
        if db_path is not None:
            self._db_path = db_path

    def shutdown(self, wait: bool = True) -> None:
        """No-op on the API side; preserved for lifespan-hook compatibility."""
        _ = wait

    # -- public API --------------------------------------------------------

    def submit(self, template: Template, job: Job, *, force: bool = False) -> str:
        job_id = str(uuid.uuid4())
        submitted_at = _now()
        conn = open_catalog_db(self._db_path)
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO jobs
                    (id, status, template_id, template_version, template_json,
                     job_json, outputs_json, error, submitted_at, started_at,
                     finished_at, force)
                    VALUES (?, 'queued', ?, ?, ?, ?, NULL, NULL, ?, NULL, NULL, ?)
                    """,
                    (
                        job_id,
                        template.id,
                        template.version,
                        json.dumps(template.model_dump(mode="json")),
                        json.dumps(job.model_dump(mode="json")),
                        submitted_at,
                        1 if force else 0,
                    ),
                )
                _persist_event(
                    conn,
                    job_id,
                    JobEvent(type="job_queued", timestamp=submitted_at),
                    seq=0,
                )
        finally:
            conn.close()
        return job_id

    def cancel(self, job_id: str) -> bool:
        """Flip cancel_requested. The worker's monitor thread sees it and
        sets its threading.Event. Returns True when the row was live
        (queued or running) and the flag took effect."""
        conn = open_catalog_db(self._db_path)
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE jobs SET cancel_requested = 1 "
                    "WHERE id = ? AND status IN ('queued', 'running')",
                    (job_id,),
                )
                return cur.rowcount > 0
        finally:
            conn.close()

    def get(self, job_id: str) -> JobRecord | None:
        conn = open_catalog_db(self._db_path)
        try:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            return _row_to_record(row)
        finally:
            conn.close()

    def get_events(self, job_id: str, *, after_seq: int = -1) -> list[JobEvent]:
        conn = open_catalog_db(self._db_path)
        try:
            return _load_events(conn, job_id, after_seq=after_seq)
        finally:
            conn.close()

    def list_jobs(self) -> list[JobRecord]:
        conn = open_catalog_db(self._db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY submitted_at ASC"
            ).fetchall()
            return [_row_to_record(r) for r in rows]
        finally:
            conn.close()


# ------------------------------------------------------------------ JobWorker


class JobWorker:
    """Worker-side: claims queued rows and runs them.

    Lives in its own process (`python -m server.worker`) so an API restart
    can't SIGTERM in-flight Siril subprocesses.
    """

    STALE_HEARTBEAT_SECONDS = 120
    HEARTBEAT_SECONDS = 5
    CANCEL_POLL_SECONDS = 0.5

    def __init__(
        self,
        cache: ContentCache | None = None,
        *,
        db_path: Path | None = None,
        worker_pid: int | None = None,
    ) -> None:
        self._cache = cache if cache is not None else ContentCache()
        self._db_path = db_path
        self._worker_pid = worker_pid if worker_pid is not None else os.getpid()

    @property
    def cache(self) -> ContentCache:
        return self._cache

    @property
    def db_path(self) -> Path | None:
        return self._db_path

    def reclaim_stale(self) -> int:
        """Mark abandoned 'running' rows as interrupted.

        A row is stale if its worker_pid is dead OR its heartbeat is older
        than STALE_HEARTBEAT_SECONDS. Both halves matter: a crash kills the
        PID without a chance to flip status, and a hung Python (deadlock,
        OOM-on-malloc) keeps the PID alive but stops heartbeating.
        """
        cutoff = datetime.now(UTC) - timedelta(seconds=self.STALE_HEARTBEAT_SECONDS)
        reclaimed = 0
        conn = open_catalog_db(self._db_path)
        try:
            rows = conn.execute(
                "SELECT id, worker_pid, heartbeat_at, started_at "
                "FROM jobs WHERE status = 'running'"
            ).fetchall()
            for r in rows:
                last_seen_raw = r["heartbeat_at"] or r["started_at"]
                stale_by_time = True
                if last_seen_raw:
                    try:
                        last_seen = datetime.fromisoformat(last_seen_raw)
                        stale_by_time = last_seen < cutoff
                    except ValueError:
                        stale_by_time = True
                if _pid_alive(r["worker_pid"]) and not stale_by_time:
                    continue
                finished = _now()
                error = "worker crashed before this job finished"
                with conn:
                    conn.execute(
                        "UPDATE jobs SET status = 'interrupted', error = ?, "
                        "finished_at = ? WHERE id = ? AND status = 'running'",
                        (error, finished, r["id"]),
                    )
                    _persist_event(
                        conn,
                        r["id"],
                        JobEvent(
                            type="job_interrupted",
                            timestamp=finished,
                            error=error,
                        ),
                    )
                reclaimed += 1
        finally:
            conn.close()
        return reclaimed

    def claim_next_queued(self) -> JobRecord | None:
        """Atomic claim of the oldest queued row. Returns the claimed
        record (with status='running') or None if the queue is empty."""
        conn = open_catalog_db(self._db_path)
        try:
            with conn:
                row = conn.execute(
                    "SELECT id FROM jobs WHERE status = 'queued' "
                    "ORDER BY submitted_at ASC LIMIT 1"
                ).fetchone()
                if row is None:
                    return None
                now = _now()
                cur = conn.execute(
                    "UPDATE jobs SET status = 'running', started_at = ?, "
                    "worker_pid = ?, heartbeat_at = ? "
                    "WHERE id = ? AND status = 'queued'",
                    (now, self._worker_pid, now, row["id"]),
                )
                if cur.rowcount != 1:
                    # Another worker (or the same worker on a retry) beat
                    # us to this row. Bail; the loop will retry next tick.
                    return None
                _persist_event(
                    conn,
                    row["id"],
                    JobEvent(type="job_started", timestamp=now),
                )
                claimed = conn.execute(
                    "SELECT * FROM jobs WHERE id = ?", (row["id"],)
                ).fetchone()
            return _row_to_record(claimed) if claimed is not None else None
        finally:
            conn.close()

    def run_one_pending(self) -> str | None:
        """Convenience: claim+run one job, return its id (or None when idle)."""
        record = self.claim_next_queued()
        if record is None:
            return None
        self._run(record)
        return record.id

    # -- internals ---------------------------------------------------------

    def _run(self, record: JobRecord) -> None:
        # If cancel was set while the job sat in the queue, short-circuit
        # before pretending we ran anything. The runtime never gets called.
        if self._read_cancel(record.id):
            self._terminate(record, status="interrupted", error="cancelled while queued")
            return

        cancel_event = threading.Event()
        monitor_stop = threading.Event()

        def monitor() -> None:
            # Two duties: heartbeat the row so reclaim_stale doesn't kick
            # us, and poll cancel_requested so the API can ask us to stop.
            # Heartbeat cadence is the coarser of the two.
            heartbeat_at = 0.0
            while not monitor_stop.is_set():
                now = time.monotonic()
                if now - heartbeat_at >= self.HEARTBEAT_SECONDS:
                    self._heartbeat(record.id)
                    heartbeat_at = now
                if not cancel_event.is_set() and self._read_cancel(record.id):
                    cancel_event.set()
                monitor_stop.wait(self.CANCEL_POLL_SECONDS)

        monitor_thread = threading.Thread(
            target=monitor, name=f"job-monitor-{record.id[:8]}", daemon=True
        )
        monitor_thread.start()

        def event_sink(payload: dict[str, Any]) -> None:
            # node_hashes is keyed by node_id (not list-appended) so cost
            # accounting stays correct regardless of YAML declaration order.
            h = payload.get("hash")
            nid = payload.get("node_id")
            if isinstance(h, str) and isinstance(nid, str):
                record.node_hashes[nid] = h
            ev = JobEvent(
                type=cast(EventType, payload["type"]),
                timestamp=_now(),
                node_id=payload.get("node_id"),
                fraction=payload.get("fraction"),
                message=payload.get("message"),
                error=payload.get("error"),
                extra={k: v for k, v in payload.items()
                       if k not in {"type", "node_id", "fraction", "message", "error"}},
            )
            self._emit_event(record.id, ev)

        try:
            outputs = run_job(
                record.template,
                record.job,
                cache=self._cache,
                events=event_sink,
                force=record.force,
                cancel=cancel_event,
            )
            record.outputs = outputs
            self._terminate(record, status="completed")
        except JobCancelled as exc:
            msg = (
                f"cancelled at node {exc.node_id!r}" if exc.node_id else "cancelled"
            )
            self._terminate(record, status="interrupted", error=msg)
        except RunError as exc:
            self._terminate(record, status="failed", error=str(exc))
        except Exception as exc:  # pragma: no cover (defensive)
            log.exception("job %s crashed", record.id)
            self._terminate(
                record, status="failed", error=f"{type(exc).__name__}: {exc}"
            )
        finally:
            monitor_stop.set()
            monitor_thread.join(timeout=5)

    def _terminate(
        self,
        record: JobRecord,
        *,
        status: JobStatus,
        error: str | None = None,
    ) -> None:
        finished = _now()
        record.status = status
        record.error = error
        record.finished_at = finished
        # Explicit if/elif so pyright can narrow event.type to the Literal
        # union; a dict lookup keyed on `status` collapses to a plain str.
        if status == "completed":
            terminal: JobEvent = JobEvent(type="job_completed", timestamp=finished)
        elif status == "failed":
            terminal = JobEvent(type="job_failed", timestamp=finished, error=error)
        elif status == "interrupted":
            terminal = JobEvent(
                type="job_interrupted", timestamp=finished, error=error
            )
        else:
            # No other status reaches _terminate; this branch keeps the
            # type checker happy.
            terminal = JobEvent(type="job_failed", timestamp=finished, error=error)

        conn = open_catalog_db(self._db_path)
        try:
            with conn:
                conn.execute(
                    """
                    UPDATE jobs
                       SET status = ?,
                           outputs_json = ?,
                           error = ?,
                           finished_at = ?,
                           node_hashes_json = ?
                     WHERE id = ?
                    """,
                    (
                        status,
                        _outputs_to_json(record.outputs),
                        error,
                        finished,
                        json.dumps(record.node_hashes) if record.node_hashes else None,
                        record.id,
                    ),
                )
                _persist_event(conn, record.id, terminal)
        finally:
            conn.close()

    def _heartbeat(self, job_id: str) -> None:
        conn = open_catalog_db(self._db_path)
        try:
            with conn:
                conn.execute(
                    "UPDATE jobs SET heartbeat_at = ? WHERE id = ?",
                    (_now(), job_id),
                )
        finally:
            conn.close()

    def _read_cancel(self, job_id: str) -> bool:
        conn = open_catalog_db(self._db_path)
        try:
            row = conn.execute(
                "SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            return bool(row["cancel_requested"]) if row is not None else False
        finally:
            conn.close()

    def _emit_event(self, job_id: str, event: JobEvent) -> None:
        conn = open_catalog_db(self._db_path)
        try:
            with conn:
                _persist_event(conn, job_id, event)
        finally:
            conn.close()
