"""Tier-based eviction ordering tests.

Locks in the policy: orphans first, then bulk-tier entries (oldest
project first), then keep-tier as a last resort.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from server.cache import ContentCache
from server.catalog.db import connect as open_catalog_db
from server.storage import CacheEntryInfo, _eviction_key, run_cleanup


def _commit_entry(cache: ContentCache, h: str, payload: int = 1024) -> Path:
    d = cache.reserve(h)
    (d / "data.bin").write_bytes(b"x" * payload)
    (d / "_done").touch()
    (d / "_outputs.json").write_text("{}")
    return d


def _seed_project(
    db_path: Path,
    *,
    project_id: str,
    updated_at: str,
    template_id: str,
    template_nodes: list[dict],
    job_id: str,
    node_hashes: dict[str, str],
) -> None:
    """Insert one project + job + history row with the given node->hash map."""
    open_catalog_db(db_path).close()
    template_json = json.dumps({
        "id": template_id,
        "version": 1,
        "description": "t",
        "nodes": template_nodes,
        "outputs": {},
    })
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO projects (
                id, name, template_id, template_version, template_json,
                base_job_json, current_seq, source_session_ids,
                created_at, updated_at
            ) VALUES (?, ?, ?, 1, ?, '{}', 0, '[]', ?, ?)
            """,
            (project_id, project_id, template_id, template_json, updated_at, updated_at),
        )
        conn.execute(
            """
            INSERT INTO jobs (id, status, template_id, template_version,
                              template_json, job_json, submitted_at,
                              node_hashes_json)
            VALUES (?, 'completed', ?, 1, '{}', '{}', ?, ?)
            """,
            (job_id, template_id, updated_at, json.dumps(node_hashes)),
        )
        conn.execute(
            """
            INSERT INTO project_history (project_id, seq, job_id, overrides_json,
                                         kind, created_at)
            VALUES (?, 1, ?, '{}', 'edit', ?)
            """,
            (project_id, job_id, updated_at),
        )
    conn.close()


def test_eviction_key_orders_orphan_bulk_keep() -> None:
    """Sort key produces the documented tier-aware ordering."""
    orphan = CacheEntryInfo(node_hash="o", bytes=10, owners=set())
    bulk_old = CacheEntryInfo(
        node_hash="b1", bytes=10, owners={"p1"},
        tier="bulk", oldest_owner_updated_at="2026-01-01",
    )
    bulk_new = CacheEntryInfo(
        node_hash="b2", bytes=10, owners={"p2"},
        tier="bulk", oldest_owner_updated_at="2026-05-01",
    )
    keep_old = CacheEntryInfo(
        node_hash="k1", bytes=10, owners={"p1"},
        tier="keep", oldest_owner_updated_at="2026-01-01",
    )

    ordered = sorted([keep_old, bulk_new, orphan, bulk_old], key=_eviction_key)
    assert [e.node_hash for e in ordered] == ["o", "b1", "b2", "k1"]


def test_run_cleanup_evicts_bulk_before_keep(tmp_path: Path) -> None:
    cache = ContentCache(root=tmp_path / "cache")
    db_path = tmp_path / "catalog.sqlite"

    _commit_entry(cache, "h_bulk", payload=10_000)
    _commit_entry(cache, "h_keep", payload=10_000)
    # One project owns both: seq_register (bulk) and seq_stack (keep).
    _seed_project(
        db_path,
        project_id="p1",
        updated_at="2026-05-01T00:00:00Z",
        template_id="t",
        template_nodes=[
            {"id": "reg", "kind": "seq_register", "params": {}, "inputs": {}},
            {
                "id": "stk",
                "kind": "seq_stack",
                "params": {},
                "inputs": {"sequence": "reg.sequence"},
            },
        ],
        job_id="j1",
        node_hashes={"reg": "h_bulk", "stk": "h_keep"},
    )

    # Budget: room for one entry only. Must drop bulk, keep keep.
    result = run_cleanup(cache, max_bytes=12_000, db_path=db_path)

    assert result.evicted_count == 1
    assert not cache.entry_dir("h_bulk").exists()
    assert cache.entry_dir("h_keep").exists()


def test_run_cleanup_within_bulk_oldest_project_first(tmp_path: Path) -> None:
    cache = ContentCache(root=tmp_path / "cache")
    db_path = tmp_path / "catalog.sqlite"

    _commit_entry(cache, "h_old", payload=10_000)
    _commit_entry(cache, "h_new", payload=10_000)
    # Two projects, both with one bulk register entry. h_old belongs to
    # the older project; eviction should pick it first.
    bulk_template = [
        {"id": "reg", "kind": "seq_register", "params": {}, "inputs": {}},
    ]
    _seed_project(
        db_path,
        project_id="p_old",
        updated_at="2026-01-01T00:00:00Z",
        template_id="t_old",
        template_nodes=bulk_template,
        job_id="j_old",
        node_hashes={"reg": "h_old"},
    )
    _seed_project(
        db_path,
        project_id="p_new",
        updated_at="2026-05-01T00:00:00Z",
        template_id="t_new",
        template_nodes=bulk_template,
        job_id="j_new",
        node_hashes={"reg": "h_new"},
    )

    result = run_cleanup(cache, max_bytes=12_000, db_path=db_path)

    assert result.evicted_count == 1
    assert not cache.entry_dir("h_old").exists()
    assert cache.entry_dir("h_new").exists()


def test_run_cleanup_falls_through_to_keep_when_bulk_exhausted(tmp_path: Path) -> None:
    cache = ContentCache(root=tmp_path / "cache")
    db_path = tmp_path / "catalog.sqlite"

    _commit_entry(cache, "h_bulk", payload=10_000)
    _commit_entry(cache, "h_keep", payload=10_000)
    _seed_project(
        db_path,
        project_id="p1",
        updated_at="2026-05-01T00:00:00Z",
        template_id="t",
        template_nodes=[
            {"id": "reg", "kind": "seq_register", "params": {}, "inputs": {}},
            {
                "id": "stk",
                "kind": "seq_stack",
                "params": {},
                "inputs": {"sequence": "reg.sequence"},
            },
        ],
        job_id="j1",
        node_hashes={"reg": "h_bulk", "stk": "h_keep"},
    )

    # Budget = 0: have to drop both bulk and keep to comply.
    result = run_cleanup(cache, max_bytes=0, db_path=db_path)

    assert result.evicted_count == 2
    assert not cache.entry_dir("h_bulk").exists()
    assert not cache.entry_dir("h_keep").exists()
