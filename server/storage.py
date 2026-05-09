"""Storage accounting + cache eviction.

Three things live here:

1. **Size accounting**: walk the cache root, sum bytes per entry, expose
   totals via `system_storage()`.

2. **Reachability mapping**: for each project, look at every history
   entry's job, pull that job's persisted `node_hashes` set, and fold
   that into a {cache_hash -> set(project_id)} reverse index. This lets
   us answer "what does this project own vs share?" without re-walking
   the event log per request.

3. **Eviction**: score every reachable cache entry by
   `cost_weight * recency_weight`, sort ascending, evict from the front
   until the cache fits under a configured budget. Dead (unreachable)
   entries always go first regardless of score so a deleted project
   frees its cache without a separate code path.

The module talks to the catalog DB and the ContentCache; it does NOT
import any FastAPI shapes so it stays test-friendly.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nodes.base import Node
from server.cache import ContentCache
from server.catalog.db import connect as open_catalog_db
from server.models import Template
from server.registry import lookup as registry_lookup

log = logging.getLogger("astrolab.storage")


# ---------------------------------------------------------------------------
# Reachability + sizing
# ---------------------------------------------------------------------------


@dataclass
class CacheEntryInfo:
    """One committed cache entry, with everything we need to reason about it."""

    node_hash: str
    bytes: int
    owners: set[str] = field(default_factory=set)
    """Project ids whose history references this entry. Empty = unreachable."""

    # Heuristic metadata used by the scorer. Filled in best-effort: when
    # multiple jobs across multiple renderings touched the same hash, we
    # take the max recency (newest) and the min cost (cheapest to recompute).
    cost: str = "expensive"
    """Cost class of the producing node ("cheap" / "medium" / "expensive").
    Defaults to expensive so we err on the side of keeping when uncertain."""
    last_used_at: str | None = None
    """ISO timestamp of the most recent history entry that references this
    cache hash. Drives the recency weight."""


@dataclass
class ProjectStorage:
    """Per-project storage breakdown surfaced to the UI."""

    project_id: str
    name: str
    updated_at: str
    owned_bytes: int
    """Bytes of cache entries reachable only from this project. Safe to
    purge without affecting other projects."""
    shared_bytes: int
    """Bytes of cache entries this project shares with others. Purging
    these would invalidate other projects too."""
    entry_count: int
    """How many cache entries this project reaches in total."""


@dataclass
class CacheDisk:
    """Disk-usage stats for whatever filesystem the cache root sits on.

    The UI uses this to cap the cache-budget slider at the partition size
    instead of an arbitrary 'current + 200 GiB' guess. `total_bytes` is the
    filesystem's full size; `free_bytes` includes everything not used by
    any file, not just headroom relative to astrolab's own usage.
    """

    total_bytes: int
    used_bytes: int
    free_bytes: int


@dataclass
class StorageSnapshot:
    """Output of `system_storage()` — what the UI renders."""

    total_bytes: int
    entry_count: int
    unreachable_bytes: int
    unreachable_count: int
    cache_root: str
    cache_disk: CacheDisk
    per_project: list[ProjectStorage]


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    # sqlite3.Row iterates VALUES, not keys; .keys() is the documented way
    # to enumerate column names. Ruff's SIM118 auto-fix to `for k in row`
    # is wrong here — silence it explicitly so a future cleanup doesn't
    # re-break this.
    return {k: row[k] for k in row.keys()}  # noqa: SIM118


def build_reachability(
    conn: sqlite3.Connection,
    cache: ContentCache,
) -> tuple[dict[str, CacheEntryInfo], dict[str, dict[str, Any]]]:
    """Return (entries by hash, projects by id).

    `entries` covers every committed cache hash on disk, with owner sets
    populated from project history. `projects` is a side index of basic
    project metadata so callers don't need to re-query.
    """
    entries: dict[str, CacheEntryInfo] = {}
    for h in cache.all_committed_hashes():
        entries[h] = CacheEntryInfo(node_hash=h, bytes=cache.entry_size(h))

    projects: dict[str, dict[str, Any]] = {}
    for r in conn.execute(
        "SELECT id, name, template_json, updated_at FROM projects"
    ).fetchall():
        projects[r["id"]] = _row_to_dict(r)

    # Map job_id -> set(node_hashes). Pull every job we have on file; some
    # may belong to projects we've since deleted (orphan job rows), but
    # that's fine — we only walk history below, which references current
    # job_ids.
    job_hashes: dict[str, list[str]] = {}
    for j in conn.execute("SELECT id, node_hashes_json FROM jobs").fetchall():
        if j["node_hashes_json"]:
            try:
                job_hashes[j["id"]] = json.loads(j["node_hashes_json"])
            except (ValueError, TypeError):
                continue

    def _node_cost_map(template_json: str) -> dict[str, str]:
        try:
            t = Template.model_validate(json.loads(template_json))
        except Exception:
            return {}
        out: dict[str, str] = {}
        for spec in t.nodes:
            try:
                cls: type[Node] = registry_lookup(spec.kind, spec.variant)
                out[spec.id] = cls.cost
            except KeyError:
                continue
        return out

    # We don't have a stored mapping from node_hash to its (kind, variant),
    # so we approximate: walk each history entry, infer per-node cost from
    # the template, and assign the project's per-node cost to each hash
    # that node touched. Multiple visits to the same hash take the cheapest
    # (most replaceable) cost so the eviction scorer doesn't over-protect
    # a hash because some other project classed it as expensive.
    cost_rank = {"cheap": 0, "medium": 1, "expensive": 2}

    for pid, p in projects.items():
        cost_map = _node_cost_map(p["template_json"])
        history = conn.execute(
            "SELECT seq, job_id, created_at FROM project_history "
            "WHERE project_id = ? ORDER BY seq ASC",
            (pid,),
        ).fetchall()
        for h in history:
            hashes_for_this_job = job_hashes.get(h["job_id"], [])
            template = Template.model_validate(json.loads(p["template_json"]))
            for i, hash_str in enumerate(hashes_for_this_job):
                if hash_str not in entries:
                    continue
                entry = entries[hash_str]
                entry.owners.add(pid)
                if i < len(template.nodes):
                    spec = template.nodes[i]
                    cost = cost_map.get(spec.id, "expensive")
                    if cost_rank.get(cost, 2) < cost_rank.get(entry.cost, 2):
                        entry.cost = cost
                created = h["created_at"]
                if entry.last_used_at is None or created > entry.last_used_at:
                    entry.last_used_at = created

    return entries, projects


def system_storage(
    cache: ContentCache,
    db_path: Path | None = None,
) -> StorageSnapshot:
    """Build the snapshot the storage UI consumes."""
    with _conn(db_path) as conn:
        entries, projects = build_reachability(conn, cache)

    total_bytes = sum(e.bytes for e in entries.values())
    unreachable_bytes = sum(e.bytes for e in entries.values() if not e.owners)
    unreachable_count = sum(1 for e in entries.values() if not e.owners)

    per_project: list[ProjectStorage] = []
    for pid, p in projects.items():
        owned = 0
        shared = 0
        count = 0
        for e in entries.values():
            if pid not in e.owners:
                continue
            count += 1
            if e.owners == {pid}:
                owned += e.bytes
            else:
                shared += e.bytes
        per_project.append(
            ProjectStorage(
                project_id=pid,
                name=p["name"],
                updated_at=p["updated_at"],
                owned_bytes=owned,
                shared_bytes=shared,
                entry_count=count,
            )
        )
    per_project.sort(key=lambda p: p.updated_at, reverse=True)

    cache_disk = _disk_usage(cache.root)

    return StorageSnapshot(
        total_bytes=total_bytes,
        entry_count=len(entries),
        unreachable_bytes=unreachable_bytes,
        unreachable_count=unreachable_count,
        cache_root=str(cache.root),
        cache_disk=cache_disk,
        per_project=per_project,
    )


def _disk_usage(path: Path) -> CacheDisk:
    """Stat the filesystem holding `path`. Falls back to zeros if the path
    isn't accessible (eg the cache dir was renamed under us)."""
    import shutil
    try:
        u = shutil.disk_usage(path)
        return CacheDisk(total_bytes=u.total, used_bytes=u.used, free_bytes=u.free)
    except OSError:
        return CacheDisk(total_bytes=0, used_bytes=0, free_bytes=0)


# ---------------------------------------------------------------------------
# Per-rendering purge
# ---------------------------------------------------------------------------


def purge_project_cache(
    cache: ContentCache,
    project_id: str,
    *,
    keep_outputs: bool = False,
    db_path: Path | None = None,
) -> tuple[int, int]:
    """Evict cache entries owned solely by `project_id`.

    Returns (entries_evicted, bytes_freed). Shared entries are left alone
    — purging them would invalidate other projects.

    `keep_outputs=True` preserves the cache hashes that show up as terminal
    outputs of any history entry's job, so the user keeps the saved final
    image but loses the stack/register/etc. intermediates. Used by the
    'Free intermediates' affordance.
    """
    with _conn(db_path) as conn:
        entries, _ = build_reachability(conn, cache)
        keep_hashes = (
            _terminal_output_hashes(conn, project_id) if keep_outputs else set()
        )

    evicted = 0
    freed = 0
    for h, e in entries.items():
        if e.owners != {project_id}:
            continue
        if h in keep_hashes:
            continue
        freed += cache.evict(h)
        evicted += 1
    return evicted, freed


def delete_project(
    project_id: str,
    cache: ContentCache,
    db_path: Path | None = None,
) -> tuple[int, int]:
    """Remove a project and any cache entries it solely owns.

    Returns (entries_evicted, bytes_freed). Caller is responsible for any
    in-memory ProjectManager state; this function only touches the DB
    and the disk cache.
    """
    evicted, freed = purge_project_cache(
        cache, project_id, keep_outputs=False, db_path=db_path
    )
    with _conn(db_path) as conn, conn:
        # project_history cascades via FK ON DELETE CASCADE.
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    return evicted, freed


def _terminal_output_hashes(
    conn: sqlite3.Connection, project_id: str
) -> set[str]:
    """Pull every node_hash that appears as a terminal output across the
    project's history. These are the hashes pointed at by
    `jobs.outputs_json[<port>].node_hash`."""
    rows = conn.execute(
        """
        SELECT j.outputs_json
        FROM project_history h
        JOIN jobs j ON j.id = h.job_id
        WHERE h.project_id = ?
        """,
        (project_id,),
    ).fetchall()
    out: set[str] = set()
    for r in rows:
        if not r["outputs_json"]:
            continue
        try:
            payload = json.loads(r["outputs_json"])
        except (ValueError, TypeError):
            continue
        for ref in payload.values():
            h = ref.get("node_hash")
            if isinstance(h, str):
                out.add(h)
    return out


# ---------------------------------------------------------------------------
# Eviction scorer
# ---------------------------------------------------------------------------


COST_WEIGHT = {"cheap": 1, "medium": 4, "expensive": 16}
"""Multiplier on the score: more expensive = higher score = kept longer."""


def _recency_weight(updated_at: str | None) -> float:
    """Multiplier driven by how recently any history entry referenced an
    entry. Recent = high weight (kept longer).
        <1 day  -> 4×
        <1 week -> 2×
        <1 mo   -> 1×
        else    -> 0.5×
    Unknown timestamps are treated as ancient (0.5×) so we err on the side
    of evicting orphans.
    """
    if not updated_at:
        return 0.5
    try:
        ts = datetime.fromisoformat(updated_at)
    except ValueError:
        return 0.5
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    age_days = (datetime.now(UTC) - ts).total_seconds() / 86400
    if age_days < 1:
        return 4.0
    if age_days < 7:
        return 2.0
    if age_days < 30:
        return 1.0
    return 0.5


def score_entry(entry: CacheEntryInfo) -> float:
    """Higher score = keep longer. Unreachable (no owners) -> 0 so they
    always rank below anything reachable."""
    if not entry.owners:
        return 0.0
    return COST_WEIGHT.get(entry.cost, 16) * _recency_weight(entry.last_used_at)


@dataclass
class CleanupResult:
    """What `run_cleanup` actually did, surfaced to the UI."""

    evicted_count: int
    bytes_freed: int
    bytes_remaining: int
    """Total cache size after the sweep."""
    over_budget: bool
    """True if we couldn't get under the budget (every reachable entry was
    so high-scoring that we'd have to evict load-bearing data to free
    enough). Caller should warn the user."""


def run_cleanup(
    cache: ContentCache,
    *,
    max_bytes: int,
    db_path: Path | None = None,
) -> CleanupResult:
    """Evict cache entries until total usage is <= max_bytes.

    Order: dead entries first (score 0, regardless of size), then
    reachable entries by ascending score. Within ties we don't currently
    sub-sort by size — the scoring already biases away from cheap/old
    pairs, so the ordering is stable enough for now.
    """
    with _conn(db_path) as conn:
        entries, _ = build_reachability(conn, cache)

    total = sum(e.bytes for e in entries.values())
    if total <= max_bytes:
        return CleanupResult(
            evicted_count=0,
            bytes_freed=0,
            bytes_remaining=total,
            over_budget=False,
        )

    ordered = sorted(entries.values(), key=lambda e: (score_entry(e), -e.bytes))

    evicted_count = 0
    bytes_freed = 0
    for e in ordered:
        if total - bytes_freed <= max_bytes:
            break
        bytes_freed += cache.evict(e.node_hash)
        evicted_count += 1

    over_budget = (total - bytes_freed) > max_bytes
    return CleanupResult(
        evicted_count=evicted_count,
        bytes_freed=bytes_freed,
        bytes_remaining=total - bytes_freed,
        over_budget=over_budget,
    )


# ---------------------------------------------------------------------------
# Settings: a thin key/value store for system-wide knobs.
# ---------------------------------------------------------------------------


SETTING_CACHE_MAX_BYTES = "cache_max_bytes"
SETTING_CACHE_ROOT_OVERRIDE = "cache_root"
SETTING_CAPTURE_ROOT = "capture_root"
"""Where on disk the user's raw captures live (Dwarf 3 SD copy, etc.).
Lives server-side rather than in browser localStorage so it survives a
cache clear and works the same from any device pointed at the same
astrolab instance — the scan target is server-local anyway."""

# Static fallback if disk_usage() ever fails (e.g. unmounted scratch). In
# practice we compute the per-disk default at request time instead, via
# `default_cache_max_bytes_for(path)` below.
DEFAULT_CACHE_MAX_BYTES = 200 * 1024 * 1024 * 1024  # 200 GiB
MIN_CACHE_MAX_BYTES = 1024 * 1024 * 1024  # 1 GiB floor (matches API 400)


def default_cache_max_bytes_for(cache_dir: Path) -> int:
    """Half the partition holding `cache_dir`, clamped to at least 1 GiB.

    Used when the user hasn't explicitly set a budget yet; gives every fresh
    install a sane starting point that scales with the host's disk instead
    of a hardcoded 200 GiB that's too big for laptops and too small for the
    Linux box's 2.3 TB scratch.
    """
    try:
        usage = _disk_usage(cache_dir)
    except OSError:
        return DEFAULT_CACHE_MAX_BYTES
    if usage.total_bytes <= 0:
        return DEFAULT_CACHE_MAX_BYTES
    return max(MIN_CACHE_MAX_BYTES, usage.total_bytes // 2)


def get_setting(key: str, default: Any = None, db_path: Path | None = None) -> Any:
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT value_json FROM settings WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value_json"])
        except (ValueError, TypeError):
            return default


def set_setting(key: str, value: Any, db_path: Path | None = None) -> None:
    now = datetime.now(UTC).isoformat()
    with _conn(db_path) as conn, conn:
        conn.execute(
            """
                INSERT INTO settings (key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
            (key, json.dumps(value), now),
        )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


class _Conn:
    """Tiny context manager so callers don't repeat open/close boilerplate."""

    def __init__(self, db_path: Path | None) -> None:
        self._path = db_path
        self._conn: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        self._conn = open_catalog_db(self._path)
        return self._conn

    def __exit__(self, *exc: Any) -> None:
        if self._conn is not None:
            self._conn.close()


def _conn(db_path: Path | None = None) -> _Conn:
    return _Conn(db_path)
