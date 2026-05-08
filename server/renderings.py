"""Rendering layer: a live, editable view of a stack pipeline.

A Rendering is the user-facing unit of work in the UI. It owns:
  - a source (one or more catalog sessions),
  - a Template (the chain of nodes, fixed at creation time),
  - a base Job (external inputs + calibration choice),
  - a linear history of (param_overrides snapshot, job_id, label) tuples,
  - a `current_seq` pointer indicating which history entry the UI is looking
    at right now.

Editing a slider in the UI maps to RenderingManager.patch(): the new param
overrides are merged into the current state, a fresh Job is submitted, and a
history entry is appended at the new tail. The content-addressed cache makes
upstream nodes (calibrate / register / stack) skip immediately when only a
downstream param changed, so re-renders feel cheap.

Undo/redo simply moves `current_seq`; the corresponding history entry's
job_id is the artifact to display. We do NOT truncate forward history on edit
from a reverted state — older entries become unreachable through normal
undo/redo but remain in the DB for "compare" or "branch" affordances later.

This module owns persistence to the catalog DB; it does NOT own job execution
(that's JobManager's job). RenderingManager calls JobManager.submit and
records the resulting job id.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .catalog.db import connect as open_catalog_db
from .jobs import JobManager
from .models import Job, Template

log = logging.getLogger("astrolab.renderings")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _deep_merge_overrides(
    base: dict[str, dict[str, Any]],
    patch: dict[str, dict[str, Any] | None],
) -> dict[str, dict[str, Any]]:
    """Merge `patch` into `base` at the (node_id, param) granularity.

    A None value in patch[node_id][param] resets that param to the template
    default (we drop the key from the merged result). A None for a whole node
    drops that node's overrides entirely.
    """
    out: dict[str, dict[str, Any]] = {nid: dict(p) for nid, p in base.items()}
    for nid, partial in patch.items():
        if partial is None:
            out.pop(nid, None)
            continue
        cur = out.setdefault(nid, {})
        for k, v in partial.items():
            if v is None:
                cur.pop(k, None)
            else:
                cur[k] = v
        if not cur:
            out.pop(nid, None)
    return out


def _diff_label(
    prev: dict[str, dict[str, Any]],
    curr: dict[str, dict[str, Any]],
    *,
    max_changes: int = 3,
) -> str:
    """Auto-generate a short label describing what changed from prev to curr.

    Examples:
      'stretch.midtones 0.5 → 0.3'
      'stretch.method autostretch → mtf, stretch.midtones 0.5 → 0.3'
      'stretch.midtones, +1 more'

    Returns 'no changes' if the override sets are identical (used when the
    caller forces a re-run without param edits).
    """
    changes: list[str] = []
    nodes = set(prev) | set(curr)
    for nid in sorted(nodes):
        p = prev.get(nid, {})
        c = curr.get(nid, {})
        keys = set(p) | set(c)
        for k in sorted(keys):
            pv = p.get(k, "<default>")
            cv = c.get(k, "<default>")
            if pv != cv:
                changes.append(f"{nid}.{k} {_fmt(pv)} → {_fmt(cv)}")
    if not changes:
        return "no changes"
    if len(changes) <= max_changes:
        return ", ".join(changes)
    return f"{', '.join(changes[:max_changes])}, +{len(changes) - max_changes} more"


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, str):
        return v
    return str(v)


@dataclass
class HistoryEntry:
    seq: int
    job_id: str
    overrides: dict[str, dict[str, Any]]
    label: str | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "job_id": self.job_id,
            "overrides": self.overrides,
            "label": self.label,
            "created_at": self.created_at,
        }


@dataclass
class Rendering:
    id: str
    name: str
    template: Template
    base_job: Job
    current_seq: int
    draft_mode: bool
    source_session_ids: list[str]
    created_at: str
    updated_at: str
    history: list[HistoryEntry] = field(default_factory=list)

    def current_entry(self) -> HistoryEntry:
        # current_seq is always a valid index into history; we never let it drift.
        return next(e for e in self.history if e.seq == self.current_seq)

    def current_overrides(self) -> dict[str, dict[str, Any]]:
        return self.current_entry().overrides

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "template_id": self.template.id,
            "template_version": self.template.version,
            "template": self.template.model_dump(mode="json"),
            "base_job": self.base_job.model_dump(mode="json"),
            "current_seq": self.current_seq,
            "draft_mode": self.draft_mode,
            "source_session_ids": self.source_session_ids,
            "history": [h.to_dict() for h in self.history],
            "current_job_id": self.current_entry().job_id,
            "current_overrides": self.current_overrides(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class RenderingNotFound(LookupError):
    pass


class RenderingManager:
    """Owns Rendering persistence and routes edits through JobManager.

    Threading: every public method takes a coarse lock for in-memory map
    consistency, then drops it before submitting jobs (which can take a
    millisecond or two for the queue write). Readers see a consistent
    snapshot via `to_public_dict()`.
    """

    def __init__(self, jobs: JobManager, *, db_path: Path | None = None) -> None:
        self._jobs = jobs
        self._db_path = db_path
        self._records: dict[str, Rendering] = {}
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def rehydrate(self) -> None:
        """Load persisted renderings from the DB. Idempotent."""
        try:
            conn = self._conn()
        except Exception:  # pragma: no cover  (defensive: DB might not exist yet)
            log.exception("could not open catalog DB for rendering rehydration")
            return
        try:
            rows = conn.execute(
                "SELECT * FROM renderings ORDER BY updated_at DESC"
            ).fetchall()
        except sqlite3.Error:
            log.exception("DB read failed during rendering rehydration")
            conn.close()
            return

        with self._lock:
            self._records.clear()
            for row in rows:
                try:
                    rendering = self._row_to_rendering(conn, row)
                except Exception:
                    log.exception("could not rehydrate rendering %s; skipping", row["id"])
                    continue
                self._records[rendering.id] = rendering
        conn.close()
        if rows:
            log.info("rehydrated %d rendering(s) from catalog DB", len(rows))

    def reset_for_tests(self, *, db_path: Path | None = None) -> None:
        with self._lock:
            self._records.clear()
        if db_path is not None:
            self._db_path = db_path

    # -- API ---------------------------------------------------------------

    def create(
        self,
        *,
        name: str,
        template: Template,
        base_job: Job,
        source_session_ids: list[str],
    ) -> Rendering:
        """Create a Rendering and submit its initial job.

        `base_job` should carry external inputs and calibration but no
        param_overrides; the initial history entry uses an empty override
        dict so the template defaults are what runs first.
        """
        if base_job.param_overrides:
            # We want history-entry overrides to be the source of truth, not
            # the seed Job. Move them into the initial entry.
            initial_overrides = dict(base_job.param_overrides)
            base_job = base_job.model_copy(update={"param_overrides": {}})
        else:
            initial_overrides = {}

        rid = str(uuid.uuid4())
        now = _now()
        rendering = Rendering(
            id=rid,
            name=name,
            template=template,
            base_job=base_job,
            current_seq=0,
            draft_mode=False,
            source_session_ids=source_session_ids,
            created_at=now,
            updated_at=now,
        )

        # Submit the initial job and append history.
        job_id = self._submit_with_overrides(rendering, initial_overrides)
        entry = HistoryEntry(
            seq=0,
            job_id=job_id,
            overrides=initial_overrides,
            label="initial render",
            created_at=now,
        )
        rendering.history.append(entry)

        with self._lock:
            self._records[rid] = rendering
        self._persist_rendering(rendering, kind="insert")
        self._persist_history_entry(rid, entry)
        log.info("rendering created: %s name=%r template=%s", rid, name, template.id)
        return rendering

    def get(self, rendering_id: str) -> Rendering | None:
        with self._lock:
            return self._records.get(rendering_id)

    def list(self) -> list[Rendering]:
        with self._lock:
            return list(self._records.values())

    def patch(
        self,
        rendering_id: str,
        *,
        overrides: dict[str, dict[str, Any] | None] | None = None,
        draft_mode: bool | None = None,
        label: str | None = None,
        force: bool = False,
    ) -> Rendering:
        """Apply param overrides (and optionally toggle draft_mode), submit a
        new job, and append the resulting state to history.

        `overrides` is a partial dict; values are merged into the rendering's
        current overrides. Pass {node_id: None} to drop a node's overrides
        entirely; pass {node_id: {param: None}} to reset a single param.

        `force=True` bypasses the cache for this submission (debug rerun).
        Reruns with `force=True` and no param changes still append a history
        entry so the run is visible in the timeline.

        Cancels the rendering's currently-active job before submitting the
        new one: an in-flight pipeline that's about to be superseded by
        fresh edits is wasted work, so we tell it to wind up cooperatively.
        The cancelled job lands in history with status 'interrupted' and
        the user can resume it later by re-submitting its overrides.

        Raises RenderingNotFound if the id is unknown.
        """
        with self._lock:
            rendering = self._records.get(rendering_id)
        if rendering is None:
            raise RenderingNotFound(rendering_id)

        prev_overrides = rendering.current_overrides()
        new_overrides = (
            _deep_merge_overrides(prev_overrides, overrides)
            if overrides is not None
            else dict(prev_overrides)
        )

        if draft_mode is not None:
            rendering.draft_mode = draft_mode

        # Drop the prior active job before queuing a new one. Cooperative
        # cancellation: the worker drains the cancel into 'interrupted'
        # status; we don't block here.
        prev_job_id = rendering.current_entry().job_id
        self._jobs.cancel(prev_job_id)

        # Submit a fresh job. Even if overrides == prev_overrides AND not
        # forced, we still go through the submit path so the timeline records
        # an explicit user action; the cache will short-circuit it.
        job_id = self._submit_with_overrides(rendering, new_overrides, force=force)

        next_seq = max((h.seq for h in rendering.history), default=-1) + 1
        derived_label = label
        if derived_label is None:
            if force and new_overrides == prev_overrides:
                derived_label = "Reprocess (cache bypass)"
            else:
                derived_label = _diff_label(prev_overrides, new_overrides)

        entry = HistoryEntry(
            seq=next_seq,
            job_id=job_id,
            overrides=new_overrides,
            label=derived_label,
            created_at=_now(),
        )
        rendering.history.append(entry)
        rendering.current_seq = next_seq
        rendering.updated_at = _now()

        self._persist_rendering(rendering, kind="update")
        self._persist_history_entry(rendering_id, entry)
        log.info(
            "rendering patched: %s seq=%d job=%s label=%r",
            rendering_id, next_seq, job_id, derived_label,
        )
        return rendering

    def forget(self, rendering_id: str) -> bool:
        """Drop a rendering from the in-memory map. Used by the API after
        the storage layer has already deleted the DB row + cache entries.
        Returns True if the id was known."""
        with self._lock:
            return self._records.pop(rendering_id, None) is not None

    def revert(self, rendering_id: str, seq: int) -> Rendering:
        """Move the current pointer to `seq`. Does not submit a new job; the
        prior history entry's job_id is what the UI displays.

        Subsequent edits append at the tail; intermediate entries between
        `seq` and the prior tail remain in the DB but become unreachable
        through normal undo/redo. We keep them for future "branch" support.
        """
        with self._lock:
            rendering = self._records.get(rendering_id)
        if rendering is None:
            raise RenderingNotFound(rendering_id)
        seqs = {h.seq for h in rendering.history}
        if seq not in seqs:
            raise ValueError(f"rendering {rendering_id} has no history seq {seq}")
        rendering.current_seq = seq
        rendering.updated_at = _now()
        self._persist_rendering(rendering, kind="update")
        log.info("rendering reverted: %s -> seq %d", rendering_id, seq)
        return rendering

    # -- internals ---------------------------------------------------------

    def _submit_with_overrides(
        self,
        rendering: Rendering,
        overrides: dict[str, dict[str, Any]],
        *,
        force: bool = False,
    ) -> str:
        """Build a Job from the rendering's base_job + given overrides, submit
        it via JobManager, and return the new job id."""
        job = rendering.base_job.model_copy(
            update={"param_overrides": overrides}
        )
        return self._jobs.submit(rendering.template, job, force=force)

    def _conn(self) -> sqlite3.Connection:
        return open_catalog_db(self._db_path)

    def _persist_rendering(self, rendering: Rendering, *, kind: str) -> None:
        try:
            conn = self._conn()
        except Exception:
            log.exception("could not open catalog DB for rendering persistence")
            return
        try:
            with conn:
                if kind == "insert":
                    conn.execute(
                        """
                        INSERT INTO renderings
                        (id, name, template_id, template_version, template_json,
                         base_job_json, current_seq, draft_mode, source_session_ids,
                         created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            rendering.id,
                            rendering.name,
                            rendering.template.id,
                            rendering.template.version,
                            json.dumps(rendering.template.model_dump(mode="json")),
                            json.dumps(rendering.base_job.model_dump(mode="json")),
                            rendering.current_seq,
                            int(rendering.draft_mode),
                            json.dumps(rendering.source_session_ids),
                            rendering.created_at,
                            rendering.updated_at,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE renderings
                        SET name=?, current_seq=?, draft_mode=?, updated_at=?
                        WHERE id=?
                        """,
                        (
                            rendering.name,
                            rendering.current_seq,
                            int(rendering.draft_mode),
                            rendering.updated_at,
                            rendering.id,
                        ),
                    )
        except sqlite3.Error:
            log.exception("DB write failed for rendering %s", rendering.id)
        finally:
            conn.close()

    def _persist_history_entry(self, rendering_id: str, entry: HistoryEntry) -> None:
        try:
            conn = self._conn()
        except Exception:
            return
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO rendering_history
                    (rendering_id, seq, job_id, overrides_json, label, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rendering_id,
                        entry.seq,
                        entry.job_id,
                        json.dumps(entry.overrides),
                        entry.label,
                        entry.created_at,
                    ),
                )
        except sqlite3.Error:
            log.exception("DB write failed for history on rendering %s", rendering_id)
        finally:
            conn.close()

    def _row_to_rendering(
        self, conn: sqlite3.Connection, row: sqlite3.Row
    ) -> Rendering:
        template = Template.model_validate(json.loads(row["template_json"]))
        base_job = Job.model_validate(json.loads(row["base_job_json"]))
        sessions: list[str] = json.loads(row["source_session_ids"])
        rendering = Rendering(
            id=row["id"],
            name=row["name"],
            template=template,
            base_job=base_job,
            current_seq=row["current_seq"],
            draft_mode=bool(row["draft_mode"]),
            source_session_ids=sessions,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        history_rows = conn.execute(
            "SELECT * FROM rendering_history WHERE rendering_id = ? ORDER BY seq ASC",
            (row["id"],),
        ).fetchall()
        for hr in history_rows:
            rendering.history.append(
                HistoryEntry(
                    seq=hr["seq"],
                    job_id=hr["job_id"],
                    overrides=json.loads(hr["overrides_json"]),
                    label=hr["label"],
                    created_at=hr["created_at"],
                )
            )
        return rendering
