"""In-process job manager for the astrolab control plane.

Phase 2 keeps live job state in memory but persists submission metadata and
the per-job event log to the catalog DB so jobs survive restarts. The
in-memory map is rehydrated from disk on startup; jobs whose status was
'queued' or 'running' when the server died are marked 'interrupted' and
their record is fixed up with a synthetic event so the UI can render them.

A single worker is fine for now: most nodes are subprocess-bound and
contention happens inside Siril, not in Python. Concurrency tuning lands when
we have a good measurement of what 'expensive' nodes need.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

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

# Cap how much history we buffer per job so a runaway noisy node can't OOM the
# server. New events past the cap are still forwarded live but the replay is
# truncated.
MAX_BUFFERED_EVENTS = 2000


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
    """Whether this submission should bypass the cache (set on Reprocess)."""
    cancel_event: threading.Event = field(default_factory=threading.Event)
    """Cooperative cancel token. Set externally to abort a running job; the
    runtime checks between nodes and SirilRuntime watchdogs the subprocess.
    Not persisted: the token is only meaningful for the current process."""
    node_hashes: dict[str, str] = field(default_factory=dict)
    """Map of node_id -> cache hash for every node this job touched (committed
    or hit). Persisted on terminal events so the storage layer can map cache
    entries back to the renderings that own them. Keyed by node_id (not list
    position) so out-of-topo-order template authoring still attributes cost
    class and last-used timestamps to the correct node."""
    events: list[JobEvent] = field(default_factory=list)
    _subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = field(
        default_factory=list
    )

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
    # Import locally so this module stays free of the nodes-import cycle at
    # module load time (registry contents accrete via decorator side-effects
    # when nodes.basic is imported by the API layer).
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


class JobManager:
    """Owns a thread-pool worker plus per-job event fan-out, with SQLite persistence."""

    def __init__(
        self,
        cache: ContentCache | None = None,
        *,
        max_workers: int = 1,
        db_path: Path | None = None,
    ) -> None:
        self._cache = cache if cache is not None else ContentCache()
        self._db_path = db_path
        self._records: dict[str, JobRecord] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="astrolab-job"
        )
        # Per-job sequence counter for ordered event persistence.
        self._event_seq: dict[str, int] = {}
        # Rehydrate is opt-in (called from lifespan startup) so tests can wire
        # up a clean tmp DB before any disk read happens.

    def rehydrate(self) -> None:
        """Load persisted jobs from the DB. Idempotent and safe to call on
        a brand-new schema with no jobs."""
        self._load_persisted()

    def reset_for_tests(self, *, db_path: Path | None = None) -> None:
        """Wipe in-memory state and (optionally) point at a fresh DB.

        Used by test fixtures that share the module-level JobManager — keeps
        production state separate from tmp test data.

        Order matters: we cancel + drain the previous executor on the
        *current* db_path before swapping in a new one. Without that, a
        leftover background job from the previous test races to write its
        terminal event into the new test's DB (FK violation, since the
        job_id only exists in the old DB), or worse, confuses migrate by
        running concurrent CREATE TABLE statements.
        """
        with self._lock:
            for record in self._records.values():
                # Cooperative; nodes that read ctx.cancel will exit
                # promptly, nodes that don't will run to completion. Either
                # way the executor.shutdown(wait=True) below blocks until
                # they're done writing.
                record.cancel_event.set()
        old = self._executor
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="astrolab-job-test"
        )
        old.shutdown(wait=True)

        with self._lock:
            self._records.clear()
            self._event_seq.clear()
        if db_path is not None:
            self._db_path = db_path

    @property
    def cache(self) -> ContentCache:
        return self._cache

    @property
    def db_path(self) -> Path | None:
        """Where this manager persists. None means 'use platform default'.
        Exposed so other modules (storage accounting) can read from the
        same DB without round-tripping through env vars."""
        return self._db_path

    # -- public API --------------------------------------------------------

    def submit(self, template: Template, job: Job, *, force: bool = False) -> str:
        job_id = str(uuid.uuid4())
        record = JobRecord(
            id=job_id,
            status="queued",
            template=template,
            job=job,
            submitted_at=_now(),
            force=force,
        )
        with self._lock:
            self._records[job_id] = record
            self._event_seq[job_id] = 0
        self._persist_record(record, kind="insert")
        self._emit(record, JobEvent(type="job_queued", timestamp=_now()))
        self._executor.submit(self._run, record)
        return job_id

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            return self._records.get(job_id)

    def list_jobs(self) -> list[JobRecord]:
        with self._lock:
            return list(self._records.values())

    def cancel(self, job_id: str) -> bool:
        """Set the cancel token for `job_id`. Returns True if the job was
        live (queued/running) and signaled, False if no such job or it was
        already terminal.

        The runtime checks the token between nodes and SirilRuntime
        watchdogs the subprocess; cancellation is cooperative, so a node
        that ignores the token (or is already past every checkpoint) will
        run to completion. JobCancelled is then translated into the
        'interrupted' status by _run."""
        record = self.get(job_id)
        if record is None:
            return False
        if record.status not in ("queued", "running"):
            return False
        record.cancel_event.set()
        return True

    def subscribe(self, job_id: str) -> asyncio.Queue | None:
        """Async-side: get a queue that receives buffered events + new ones live.

        Returns None if the job does not exist. Caller must call unsubscribe
        when done. The queue is bounded; callers should drain it promptly or
        risk dropping events on a slow link.
        """
        record = self.get(job_id)
        if record is None:
            return None
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_BUFFERED_EVENTS)
        # Replay buffered history first so the subscriber has the full picture.
        with self._lock:
            for ev in record.events:
                try:
                    queue.put_nowait(ev)
                except asyncio.QueueFull:
                    log.warning("subscribe replay overflowed for job %s", job_id)
                    break
            record._subscribers.append((loop, queue))
        return queue

    def unsubscribe(self, job_id: str, queue: asyncio.Queue) -> None:
        record = self.get(job_id)
        if record is None:
            return
        with self._lock:
            record._subscribers = [
                (loop, q) for (loop, q) in record._subscribers if q is not queue
            ]

    # -- worker side -------------------------------------------------------

    def _run(self, record: JobRecord) -> None:
        # If the job was cancelled while still queued, short-circuit before
        # advertising it as running. Saves a node_started event the UI
        # would only have to walk back.
        if record.cancel_event.is_set():
            self._interrupt(record, "cancelled while queued")
            return

        with self._lock:
            record.status = "running"
            record.started_at = _now()
        self._persist_record(record, kind="update")
        self._emit(record, JobEvent(type="job_started", timestamp=_now()))

        def event_sink(payload: dict[str, Any]) -> None:
            # Capture node_hashes keyed by node_id as they're announced by
            # the runtime; we persist the rolled-up map on the terminal
            # event so the storage layer can look up "what did this job
            # touch?" without walking the event log. Pairing the hash with
            # its node_id (vs the old bare list of hashes) keeps cost-class
            # and last-used attribution correct when YAML order disagrees
            # with topological order.
            h = payload.get("hash")
            nid = payload.get("node_id")
            if isinstance(h, str) and isinstance(nid, str):
                record.node_hashes[nid] = h
            ev = JobEvent(
                type=payload["type"],  # type: ignore[arg-type]
                timestamp=_now(),
                node_id=payload.get("node_id"),
                fraction=payload.get("fraction"),
                message=payload.get("message"),
                error=payload.get("error"),
                extra={k: v for k, v in payload.items()
                       if k not in {"type", "node_id", "fraction", "message", "error"}},
            )
            self._emit(record, ev)

        try:
            outputs = run_job(
                record.template,
                record.job,
                cache=self._cache,
                events=event_sink,
                force=record.force,
                cancel=record.cancel_event,
            )
            with self._lock:
                record.status = "completed"
                record.outputs = outputs
                record.finished_at = _now()
            self._persist_record(record, kind="update")
            self._emit(record, JobEvent(type="job_completed", timestamp=_now()))
        except JobCancelled as exc:
            # Cancellation is a first-class outcome, not a failure: the
            # rendering's history entry stays around with status 'interrupted'
            # and the user can hit Resume to re-submit those overrides (cache
            # makes already-completed nodes a free skip).
            self._interrupt(
                record, f"cancelled at node {exc.node_id!r}" if exc.node_id else "cancelled"
            )
        except RunError as exc:
            self._fail(record, str(exc))
        except Exception as exc:  # pragma: no cover  (defensive)
            log.exception("job %s crashed", record.id)
            self._fail(record, f"{type(exc).__name__}: {exc}")

    def _fail(self, record: JobRecord, message: str) -> None:
        with self._lock:
            record.status = "failed"
            record.error = message
            record.finished_at = _now()
        self._persist_record(record, kind="update")
        self._emit(record, JobEvent(type="job_failed", timestamp=_now(), error=message))

    def _interrupt(self, record: JobRecord, message: str) -> None:
        with self._lock:
            record.status = "interrupted"
            record.error = message
            record.finished_at = _now()
        self._persist_record(record, kind="update")
        self._emit(record, JobEvent(type="job_interrupted", timestamp=_now(), error=message))

    def _emit(self, record: JobRecord, event: JobEvent) -> None:
        with self._lock:
            if len(record.events) < MAX_BUFFERED_EVENTS:
                record.events.append(event)
            seq = self._event_seq.get(record.id, 0)
            self._event_seq[record.id] = seq + 1
            subscribers = list(record._subscribers)
        self._persist_event(record.id, seq, event)
        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:
                # Loop is closed; subscriber is gone. Best-effort cleanup.
                self.unsubscribe(record.id, queue)

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait)

    # -- persistence -------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        # New connection per call: SQLite is fast for this and avoids
        # cross-thread sharing issues between the worker and request handlers.
        return open_catalog_db(self._db_path)

    def _close(self, conn: sqlite3.Connection) -> None:
        conn.close()

    def _persist_record(self, record: JobRecord, *, kind: str) -> None:
        """Insert or update a job row. Best-effort: log and continue on DB errors."""
        try:
            conn = self._conn()
        except Exception:
            log.exception("could not open catalog DB for job persistence")
            return
        try:
            with conn:
                if kind == "insert":
                    conn.execute(
                        """
                        INSERT INTO jobs
                        (id, status, template_id, template_version, template_json,
                         job_json, outputs_json, error, submitted_at, started_at, finished_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record.id,
                            record.status,
                            record.template.id,
                            record.template.version,
                            json.dumps(record.template.model_dump(mode="json")),
                            json.dumps(record.job.model_dump(mode="json")),
                            _outputs_to_json(record.outputs),
                            record.error,
                            record.submitted_at,
                            record.started_at,
                            record.finished_at,
                        ),
                    )
                else:  # update
                    conn.execute(
                        """
                        UPDATE jobs
                        SET status=?, outputs_json=?, error=?, started_at=?, finished_at=?,
                            node_hashes_json=?
                        WHERE id=?
                        """,
                        (
                            record.status,
                            _outputs_to_json(record.outputs),
                            record.error,
                            record.started_at,
                            record.finished_at,
                            json.dumps(dict(record.node_hashes)) if record.node_hashes else None,
                            record.id,
                        ),
                    )
        except sqlite3.Error:
            log.exception("DB write failed for job %s", record.id)
        finally:
            self._close(conn)

    def _persist_event(self, job_id: str, seq: int, event: JobEvent) -> None:
        try:
            conn = self._conn()
        except Exception:
            return
        try:
            with conn:
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
        except sqlite3.Error:
            log.exception("DB write failed for event on job %s", job_id)
        finally:
            self._close(conn)

    def _load_persisted(self) -> None:
        """Hydrate _records from disk and mark crashed jobs as interrupted."""
        try:
            conn = self._conn()
        except Exception:
            log.exception("could not open catalog DB for job rehydration")
            return
        try:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY submitted_at ASC"
            ).fetchall()
        except sqlite3.Error:
            log.exception("DB read failed during rehydration")
            self._close(conn)
            return

        for row in rows:
            try:
                template = Template.model_validate(json.loads(row["template_json"]))
                job = Job.model_validate(json.loads(row["job_json"]))
                outputs = _outputs_from_json(row["outputs_json"])
            except Exception:
                log.exception("could not rehydrate job %s; skipping", row["id"])
                continue

            status: JobStatus = row["status"]  # type: ignore[assignment]
            error = row["error"]
            finished_at = row["finished_at"]
            if status in ("queued", "running"):
                # Server crashed mid-run. Mark as interrupted; ev sequence
                # continues from whatever was last persisted.
                status = "interrupted"
                error = error or "server interrupted before this job finished"
                finished_at = finished_at or _now()

            node_hashes = _node_hashes_from_json(row["node_hashes_json"], template)
            # Backfill for jobs that ran before we started persisting hashes:
            # walk the event log, collect (node_id, hash) pairs from
            # node_started / node_cached events, and persist them so the
            # storage layer can attribute their cache back to the right
            # rendering. One-time cost on the first rehydrate after the
            # upgrade; subsequent rehydrates see the column already filled.
            backfilled = False
            if not node_hashes:
                hash_rows = conn.execute(
                    "SELECT node_id, extra_json FROM job_events "
                    "WHERE job_id = ? AND type IN ('node_started', 'node_cached')",
                    (row["id"],),
                ).fetchall()
                for hr in hash_rows:
                    if not hr["extra_json"]:
                        continue
                    try:
                        extra = json.loads(hr["extra_json"])
                    except (ValueError, TypeError):
                        continue
                    h = extra.get("hash")
                    nid = hr["node_id"]
                    if isinstance(h, str) and isinstance(nid, str):
                        node_hashes[nid] = h
                if node_hashes:
                    backfilled = True
            record = JobRecord(
                id=row["id"],
                status=status,
                template=template,
                job=job,
                submitted_at=row["submitted_at"],
                started_at=row["started_at"],
                finished_at=finished_at,
                outputs=outputs,
                error=error,
                node_hashes=node_hashes,
            )

            # Pull the persisted event log so /events HTTP endpoint and
            # WebSocket replay both see history across restarts.
            event_rows = conn.execute(
                "SELECT * FROM job_events WHERE job_id = ? ORDER BY seq ASC",
                (row["id"],),
            ).fetchall()
            for er in event_rows:
                record.events.append(
                    JobEvent(
                        type=er["type"],
                        timestamp=er["timestamp"],
                        node_id=er["node_id"],
                        fraction=er["fraction"],
                        message=er["message"],
                        error=er["error"],
                        extra=json.loads(er["extra_json"]) if er["extra_json"] else {},
                    )
                )

            with self._lock:
                self._records[record.id] = record
                self._event_seq[record.id] = len(record.events)

            # If we promoted to interrupted, persist the new state and add a
            # marker event so the UI can show *why* it isn't running.
            if status == "interrupted" and row["status"] in ("queued", "running"):
                self._persist_record(record, kind="update")
                self._emit(
                    record,
                    JobEvent(
                        type="job_interrupted",
                        timestamp=_now(),
                        error="server interrupted before this job finished",
                    ),
                )
            elif backfilled:
                # We reconstructed node_hashes from events; persist them
                # so the storage layer (which reads the column directly)
                # can attribute this job's cache going forward.
                self._persist_record(record, kind="update")

        self._close(conn)
        if rows:
            log.info("rehydrated %d job(s) from catalog DB", len(rows))
