"""In-process job manager for the astrolab control plane.

Phase 2 keeps job state in memory. Jobs are submitted via the API, queued onto
a single worker thread, and run synchronously through `run_job`. Lifecycle
events are buffered per-job and fanned out to subscribed asyncio queues so the
WebSocket endpoint can replay history then stream live events.

A single worker is fine for now: most nodes are subprocess-bound and
contention happens inside Siril, not in Python. Concurrency tuning lands when
we have a good measurement of what 'expensive' nodes need.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from .cache import ContentCache
from .models import Job, Ref, Template
from .runtime import RunError, run_job

log = logging.getLogger("astrolab.jobs")

JobStatus = Literal["queued", "running", "completed", "failed"]
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
            # the UI can graph without further translation.
            d["template"] = self.template.model_dump(mode="json")
        return d


def _now() -> str:
    return datetime.now(UTC).isoformat()


class JobManager:
    """Owns a thread-pool worker plus per-job event fan-out to async subscribers."""

    def __init__(self, cache: ContentCache | None = None, *, max_workers: int = 1) -> None:
        self._cache = cache if cache is not None else ContentCache()
        self._records: dict[str, JobRecord] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="astrolab-job"
        )

    @property
    def cache(self) -> ContentCache:
        return self._cache

    def submit(self, template: Template, job: Job) -> str:
        job_id = str(uuid.uuid4())
        record = JobRecord(
            id=job_id,
            status="queued",
            template=template,
            job=job,
            submitted_at=_now(),
        )
        self._emit(record, JobEvent(type="job_queued", timestamp=_now()))
        with self._lock:
            self._records[job_id] = record
        self._executor.submit(self._run, record)
        return job_id

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            return self._records.get(job_id)

    def list_jobs(self) -> list[JobRecord]:
        with self._lock:
            return list(self._records.values())

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
                # Best-effort during replay; if the subscriber is already lagging,
                # bail — they'll see the live tail anyway.
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
        with self._lock:
            record.status = "running"
            record.started_at = _now()
        self._emit(record, JobEvent(type="job_started", timestamp=_now()))

        def event_sink(payload: dict[str, Any]) -> None:
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
            )
            with self._lock:
                record.status = "completed"
                record.outputs = outputs
                record.finished_at = _now()
            self._emit(record, JobEvent(type="job_completed", timestamp=_now()))
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
        self._emit(record, JobEvent(type="job_failed", timestamp=_now(), error=message))

    def _emit(self, record: JobRecord, event: JobEvent) -> None:
        with self._lock:
            if len(record.events) < MAX_BUFFERED_EVENTS:
                record.events.append(event)
            subscribers = list(record._subscribers)
        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:
                # Loop is closed; subscriber is gone. Best-effort cleanup.
                self.unsubscribe(record.id, queue)

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait)
