"""Project layer: a live, editable view of a stack pipeline.

A Project is the user-facing unit of work in the UI. It owns:
  - a source (one or more catalog sessions),
  - a Template (the chain of nodes, fixed at creation time),
  - a base Job (external inputs + calibration choice),
  - a linear history of (param_overrides snapshot, job_id, label) tuples,
  - a `current_seq` pointer indicating which history entry the UI is looking
    at right now.

Editing a slider in the UI maps to ProjectManager.patch(): the new param
overrides are merged into the current state, a fresh Job is submitted, and a
history entry is appended at the new tail. The content-addressed cache makes
upstream nodes (calibrate / register / stack) skip immediately when only a
downstream param changed, so re-renders feel cheap.

Undo/redo simply moves `current_seq`; the corresponding history entry's
job_id is the artifact to display. We do NOT truncate forward history on edit
from a reverted state — older entries become unreachable through normal
undo/redo but remain in the DB for "compare" or "branch" affordances later.

This module owns persistence to the catalog DB; it does NOT own job execution
(that's JobManager's job). ProjectManager calls JobManager.submit and records
the resulting job id.

(Originally called "Rendering"; renamed to "Project" since users think in
terms of "my Wizard Nebula edit" not "this single output".)
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

log = logging.getLogger("astrolab.projects")


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
    """Auto-generate a short label describing what changed from prev to curr."""
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
    published: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "job_id": self.job_id,
            "overrides": self.overrides,
            "label": self.label,
            "created_at": self.created_at,
            "published": self.published,
        }


@dataclass
class Project:
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
    cover_seq: int | None = None
    """User-pinned history seq to render as the project's cover.
    None = auto-pick the latest entry with outputs."""
    description: str | None = None
    """Free-text notes the user attaches to the project. None when no
    note has been saved; the UI suppresses empty labels."""

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
            "cover_seq": self.cover_seq,
            "description": self.description,
        }


class ProjectNotFound(LookupError):
    pass


class ProjectManager:
    """Owns Project persistence and routes edits through JobManager.

    Threading: every public method takes a coarse lock for in-memory map
    consistency, then drops it before submitting jobs (which can take a
    millisecond or two for the queue write). Readers see a consistent
    snapshot via `to_public_dict()`.
    """

    def __init__(self, jobs: JobManager, *, db_path: Path | None = None) -> None:
        self._jobs = jobs
        self._db_path = db_path
        self._records: dict[str, Project] = {}
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def rehydrate(self) -> None:
        """Load persisted projects from the DB. Idempotent."""
        try:
            conn = self._conn()
        except Exception:  # pragma: no cover  (defensive: DB might not exist yet)
            log.exception("could not open catalog DB for project rehydration")
            return
        try:
            rows = conn.execute(
                "SELECT * FROM projects ORDER BY updated_at DESC"
            ).fetchall()
        except sqlite3.Error:
            log.exception("DB read failed during project rehydration")
            conn.close()
            return

        with self._lock:
            self._records.clear()
            for row in rows:
                try:
                    project = self._row_to_project(conn, row)
                except Exception:
                    log.exception("could not rehydrate project %s; skipping", row["id"])
                    continue
                self._records[project.id] = project
        conn.close()
        if rows:
            log.info("rehydrated %d project(s) from catalog DB", len(rows))

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
    ) -> Project:
        """Create a Project and submit its initial job.

        `base_job` should carry external inputs and calibration but no
        param_overrides; the initial history entry uses an empty override
        dict so the template defaults are what runs first.
        """
        if base_job.param_overrides:
            initial_overrides = dict(base_job.param_overrides)
            base_job = base_job.model_copy(update={"param_overrides": {}})
        else:
            initial_overrides = {}

        pid = str(uuid.uuid4())
        now = _now()
        project = Project(
            id=pid,
            name=name,
            template=template,
            base_job=base_job,
            current_seq=0,
            draft_mode=False,
            source_session_ids=source_session_ids,
            created_at=now,
            updated_at=now,
        )

        job_id = self._submit_with_overrides(project, initial_overrides)
        entry = HistoryEntry(
            seq=0,
            job_id=job_id,
            overrides=initial_overrides,
            label="initial render",
            created_at=now,
        )
        project.history.append(entry)

        with self._lock:
            self._records[pid] = project
        self._persist_project(project, kind="insert")
        self._persist_history_entry(pid, entry)
        log.info("project created: %s name=%r template=%s", pid, name, template.id)
        return project

    def get(self, project_id: str) -> Project | None:
        with self._lock:
            return self._records.get(project_id)

    def list(self) -> list[Project]:
        with self._lock:
            return list(self._records.values())

    def patch(
        self,
        project_id: str,
        *,
        overrides: dict[str, dict[str, Any] | None] | None = None,
        draft_mode: bool | None = None,
        label: str | None = None,
        force: bool = False,
    ) -> Project:
        """Apply param overrides (and optionally toggle draft_mode), submit a
        new job, and append the resulting state to history.

        Cancels the project's currently-active job before submitting the new
        one so an in-flight pipeline that's about to be superseded by fresh
        edits doesn't waste cycles. The cancelled job lands in history with
        status 'interrupted'; the user can resume it by re-submitting its
        overrides.

        Raises ProjectNotFound if the id is unknown.
        """
        with self._lock:
            project = self._records.get(project_id)
        if project is None:
            raise ProjectNotFound(project_id)

        prev_overrides = project.current_overrides()
        new_overrides = (
            _deep_merge_overrides(prev_overrides, overrides)
            if overrides is not None
            else dict(prev_overrides)
        )

        if draft_mode is not None:
            project.draft_mode = draft_mode

        prev_job_id = project.current_entry().job_id
        self._jobs.cancel(prev_job_id)

        job_id = self._submit_with_overrides(project, new_overrides, force=force)

        next_seq = max((h.seq for h in project.history), default=-1) + 1
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
        project.history.append(entry)
        project.current_seq = next_seq
        project.updated_at = _now()

        self._persist_project(project, kind="update")
        self._persist_history_entry(project_id, entry)
        log.info(
            "project patched: %s seq=%d job=%s label=%r",
            project_id, next_seq, job_id, derived_label,
        )
        return project

    def forget(self, project_id: str) -> bool:
        """Drop a project from the in-memory map. Used by the API after the
        storage layer has already deleted the DB row + cache entries."""
        with self._lock:
            return self._records.pop(project_id, None) is not None

    def set_cover(self, project_id: str, seq: int | None) -> Project:
        """Pin a specific history seq as the project's cover, or pass
        None to clear (the projects list / gallery then auto-pick the
        latest entry that has outputs)."""
        with self._lock:
            project = self._records.get(project_id)
        if project is None:
            raise ProjectNotFound(project_id)
        if seq is not None:
            seqs = {h.seq for h in project.history}
            if seq not in seqs:
                raise ValueError(f"project {project_id} has no history seq {seq}")
        project.cover_seq = seq
        project.updated_at = _now()
        self._persist_project(project, kind="update")
        log.info("project cover set: %s -> seq=%s", project_id, seq)
        return project

    def set_published(
        self, project_id: str, seq: int, published: bool
    ) -> Project:
        """Toggle the gallery 'published' flag on a single history entry.

        The Gallery view filters to published-only; this is the user's
        opt-in to surface a render publicly. Orthogonal to cover_seq
        (cover is the project's representative thumbnail; published
        promotes the entry to the global gallery feed).
        """
        with self._lock:
            project = self._records.get(project_id)
        if project is None:
            raise ProjectNotFound(project_id)
        entry = next((h for h in project.history if h.seq == seq), None)
        if entry is None:
            raise ValueError(f"project {project_id} has no history seq {seq}")
        entry.published = bool(published)
        project.updated_at = _now()
        self._persist_history_published(project_id, seq, entry.published)
        self._persist_project(project, kind="update")
        log.info(
            "project history published: %s seq=%d -> %s",
            project_id, seq, entry.published,
        )
        return project

    def set_description(self, project_id: str, description: str | None) -> Project:
        """Update the project's free-text notes. Does NOT submit a new
        job (description is metadata, not pipeline-affecting input).

        Empty/whitespace-only strings normalize to None so the "no note"
        state is unambiguous in DTO + UI.
        """
        with self._lock:
            project = self._records.get(project_id)
        if project is None:
            raise ProjectNotFound(project_id)
        normalized: str | None
        if description is None:
            normalized = None
        else:
            stripped = description.strip()
            normalized = stripped if stripped else None
        project.description = normalized
        project.updated_at = _now()
        self._persist_project(project, kind="update")
        log.info(
            "project description set: %s (len=%s)",
            project_id, len(normalized) if normalized else 0,
        )
        return project

    def revert(self, project_id: str, seq: int) -> Project:
        """Move the current pointer to `seq`. Does not submit a new job; the
        prior history entry's job_id is what the UI displays.
        """
        with self._lock:
            project = self._records.get(project_id)
        if project is None:
            raise ProjectNotFound(project_id)
        seqs = {h.seq for h in project.history}
        if seq not in seqs:
            raise ValueError(f"project {project_id} has no history seq {seq}")
        project.current_seq = seq
        project.updated_at = _now()
        self._persist_project(project, kind="update")
        log.info("project reverted: %s -> seq %d", project_id, seq)
        return project

    # -- internals ---------------------------------------------------------

    def _submit_with_overrides(
        self,
        project: Project,
        overrides: dict[str, dict[str, Any]],
        *,
        force: bool = False,
    ) -> str:
        job = project.base_job.model_copy(
            update={"param_overrides": overrides}
        )
        return self._jobs.submit(project.template, job, force=force)

    def _conn(self) -> sqlite3.Connection:
        return open_catalog_db(self._db_path)

    def _persist_project(self, project: Project, *, kind: str) -> None:
        try:
            conn = self._conn()
        except Exception:
            log.exception("could not open catalog DB for project persistence")
            return
        try:
            with conn:
                if kind == "insert":
                    conn.execute(
                        """
                        INSERT INTO projects
                        (id, name, template_id, template_version, template_json,
                         base_job_json, current_seq, draft_mode, source_session_ids,
                         created_at, updated_at, description)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            project.id,
                            project.name,
                            project.template.id,
                            project.template.version,
                            json.dumps(project.template.model_dump(mode="json")),
                            json.dumps(project.base_job.model_dump(mode="json")),
                            project.current_seq,
                            int(project.draft_mode),
                            json.dumps(project.source_session_ids),
                            project.created_at,
                            project.updated_at,
                            project.description,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE projects
                        SET name=?, current_seq=?, draft_mode=?, cover_seq=?,
                            updated_at=?, description=?
                        WHERE id=?
                        """,
                        (
                            project.name,
                            project.current_seq,
                            int(project.draft_mode),
                            project.cover_seq,
                            project.updated_at,
                            project.description,
                            project.id,
                        ),
                    )
        except sqlite3.Error:
            log.exception("DB write failed for project %s", project.id)
        finally:
            conn.close()

    def _persist_history_entry(self, project_id: str, entry: HistoryEntry) -> None:
        try:
            conn = self._conn()
        except Exception:
            return
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO project_history
                    (project_id, seq, job_id, overrides_json, label, created_at,
                     published)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        entry.seq,
                        entry.job_id,
                        json.dumps(entry.overrides),
                        entry.label,
                        entry.created_at,
                        int(entry.published),
                    ),
                )
        except sqlite3.Error:
            log.exception("DB write failed for history on project %s", project_id)
        finally:
            conn.close()

    def _persist_history_published(
        self, project_id: str, seq: int, published: bool
    ) -> None:
        try:
            conn = self._conn()
        except Exception:
            return
        try:
            with conn:
                conn.execute(
                    "UPDATE project_history SET published=? "
                    "WHERE project_id=? AND seq=?",
                    (int(published), project_id, seq),
                )
        except sqlite3.Error:
            log.exception(
                "DB write failed updating published on %s seq=%d",
                project_id, seq,
            )
        finally:
            conn.close()

    def _row_to_project(
        self, conn: sqlite3.Connection, row: sqlite3.Row
    ) -> Project:
        template = Template.model_validate(json.loads(row["template_json"]))
        base_job = Job.model_validate(json.loads(row["base_job_json"]))
        sessions: list[str] = json.loads(row["source_session_ids"])
        project = Project(
            id=row["id"],
            name=row["name"],
            template=template,
            base_job=base_job,
            current_seq=row["current_seq"],
            draft_mode=bool(row["draft_mode"]),
            source_session_ids=sessions,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            # cover_seq is nullable in the DB. Older rows pre-migration
            # land as None which is exactly the 'auto-pick latest' default.
            # sqlite3.Row has no .get(); membership check via keys() is the
            # only safe shape, hence the SIM118 silence.
            cover_seq=(
                row["cover_seq"]
                if "cover_seq" in row.keys()  # noqa: SIM118
                else None
            ),
            # description is nullable; pre-migration rows have no
            # column at all, so we mirror the cover_seq guard pattern.
            description=(
                row["description"]
                if "description" in row.keys()  # noqa: SIM118
                else None
            ),
        )
        history_rows = conn.execute(
            "SELECT * FROM project_history WHERE project_id = ? ORDER BY seq ASC",
            (row["id"],),
        ).fetchall()
        for hr in history_rows:
            # `published` is nullable for pre-v8 rows that predate the
            # column. Default falsy -> unpublished.
            published = (
                bool(hr["published"])
                if "published" in hr.keys()  # noqa: SIM118
                else False
            )
            project.history.append(
                HistoryEntry(
                    seq=hr["seq"],
                    job_id=hr["job_id"],
                    overrides=json.loads(hr["overrides_json"]),
                    label=hr["label"],
                    created_at=hr["created_at"],
                    published=published,
                )
            )
        return project
