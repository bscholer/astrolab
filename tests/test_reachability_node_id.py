"""Reachability attribution by node_id, not YAML position.

`server.storage.build_reachability` used to correlate a job's persisted
node_hashes (topo execution order) to template.nodes (YAML declaration
order) by list index. When a template is authored out of topo order, that
mismatch attributes cost class and last-used timestamps to the wrong
nodes, and the eviction scorer makes bad decisions.

These tests pin the fix: hashes carry their node_id, and storage looks
up specs by id. Legacy list-shaped payloads still attribute through the
old (index-based) path so we don't go dark on records written before the
migration.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import nodes.basic  # noqa: F401  registers seq_register (bulk) and seq_stack (keep)
from server.catalog.db import connect as open_catalog_db
from server.models import Job, NodeSpec, Ref, Template
from server.ports import PortType
from server.storage import build_reachability


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "catalog.sqlite"
    open_catalog_db(db).close()  # run migrations
    return db


def _yaml_out_of_topo_template() -> Template:
    """Template whose YAML order disagrees with topological order.

    YAML order:    [seq_stack, seq_register]   (keep, bulk)
    Topo order:    [seq_register, seq_stack]   (bulk feeds keep)

    If storage indexes hashes by YAML position, the keep-tier node's
    hash gets attributed to the bulk-tier node and vice versa — exactly
    the bug we're fixing. Pairing a bulk node with a keep node gives the
    assertion something to discriminate on.
    """
    return Template(
        id="ooo",
        version=1,
        description="YAML order != topo order",
        nodes=[
            NodeSpec(
                id="downstream",
                kind="seq_stack",
                params={},
                inputs={"sequence": "upstream.sequence"},
            ),
            NodeSpec(id="upstream", kind="seq_register", params={}),
        ],
        outputs={"final": "downstream.image"},
    )


def _seed_project(
    db: Path,
    project_id: str,
    template: Template,
    job_id: str,
    *,
    node_hashes_payload: str,
    created_at: str,
) -> None:
    """Insert a project + history row + job row with the given node_hashes
    payload (raw JSON string, so tests can write either dict or list shape)."""
    job = Job(
        template_id=template.id,
        template_version=template.version,
        inputs={
            "upstream.sequence": Ref(
                node_hash="ext",
                port="sequence",
                path=Path("/dev/null"),
                type=PortType.SEQUENCE_FITS,
            ),
        },
    )
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO jobs
                (id, status, template_id, template_version, template_json,
                 job_json, submitted_at, started_at, finished_at,
                 node_hashes_json)
                VALUES (?, 'completed', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    template.id,
                    template.version,
                    json.dumps(template.model_dump(mode="json")),
                    json.dumps(job.model_dump(mode="json")),
                    created_at,
                    created_at,
                    created_at,
                    node_hashes_payload,
                ),
            )
            conn.execute(
                """
                INSERT INTO projects
                (id, name, template_id, template_version, template_json,
                 base_job_json, current_seq, draft_mode, source_session_ids,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, 0, '[]', ?, ?)
                """,
                (
                    project_id,
                    "test",
                    template.id,
                    template.version,
                    json.dumps(template.model_dump(mode="json")),
                    json.dumps(job.model_dump(mode="json")),
                    created_at,
                    created_at,
                ),
            )
            conn.execute(
                """
                INSERT INTO project_history
                (project_id, seq, job_id, overrides_json, created_at)
                VALUES (?, 1, ?, '{}', ?)
                """,
                (project_id, job_id, created_at),
            )
    finally:
        conn.close()


class _StubCache:
    """Pretends two committed hashes exist on disk so build_reachability
    has something to attribute costs to."""

    def __init__(self, hashes: dict[str, int]) -> None:
        self._hashes = hashes
        self.root = Path("/tmp/_stub_cache_not_used")

    def all_committed_hashes(self) -> list[str]:
        return list(self._hashes)

    def entry_size(self, h: str) -> int:
        return self._hashes.get(h, 0)


def test_dict_payload_attributes_by_node_id(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    template = _yaml_out_of_topo_template()
    cache = _StubCache({"hash-upstream": 1000, "hash-downstream": 500})
    created = datetime.now(UTC).isoformat()

    payload = json.dumps({"upstream": "hash-upstream", "downstream": "hash-downstream"})
    _seed_project(
        db,
        project_id="proj-dict",
        template=template,
        job_id="job-dict",
        node_hashes_payload=payload,
        created_at=created,
    )

    conn = open_catalog_db(db)
    try:
        entries, _ = build_reachability(conn, cache)  # type: ignore[arg-type]
    finally:
        conn.close()

    # The fix: tier follows node identity, not YAML position.
    assert entries["hash-upstream"].tier == "bulk"  # seq_register
    assert entries["hash-downstream"].tier == "keep"  # seq_stack
    # Owners populated for both.
    assert entries["hash-upstream"].owners == {"proj-dict"}
    assert entries["hash-downstream"].owners == {"proj-dict"}


def test_legacy_list_payload_still_attributes(tmp_path: Path) -> None:
    """Old records were a bare list of hashes in declaration-ish order.
    Storage was already indexing by template.nodes[i], so the backfill
    must keep that same pairing: same (possibly imperfect) result as
    before, but no crash and no silently-dark records."""
    db = _make_db(tmp_path)
    template = _yaml_out_of_topo_template()
    cache = _StubCache({"hash-a": 1000, "hash-b": 500})
    created = datetime.now(UTC).isoformat()

    # List shape: [downstream's hash, upstream's hash] in YAML order.
    payload = json.dumps(["hash-a", "hash-b"])
    _seed_project(
        db,
        project_id="proj-list",
        template=template,
        job_id="job-list",
        node_hashes_payload=payload,
        created_at=created,
    )

    conn = open_catalog_db(db)
    try:
        entries, _ = build_reachability(conn, cache)  # type: ignore[arg-type]
    finally:
        conn.close()

    # Both hashes get owned (no crash, no silent dropouts on the legacy path).
    assert entries["hash-a"].owners == {"proj-list"}
    assert entries["hash-b"].owners == {"proj-list"}
    # Pre-fix attribution by list index: template.nodes[0]=downstream(seq_stack,
    # keep), template.nodes[1]=upstream(seq_register, bulk). The legacy
    # path keeps that pairing exactly so old records don't change meaning.
    assert entries["hash-a"].tier == "keep"
    assert entries["hash-b"].tier == "bulk"
