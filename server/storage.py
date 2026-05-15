"""Storage accounting + cache eviction.

Three things live here:

1. **Size accounting**: walk the cache root, sum bytes per entry, expose
   totals via `system_storage()`.

2. **Reachability mapping**: for each project, look at every history
   entry's job, pull that job's persisted `node_hashes` set, and fold
   that into a {cache_hash -> set(project_id)} reverse index. This lets
   us answer "what does this project own vs share?" without re-walking
   the event log per request.

3. **Eviction**: order entries by (orphan, tier, oldest-owner-updated-at)
   and evict until the cache fits under the configured budget. Orphans
   go first, then bulk-tier entries (pre-stack sequence data) oldest
   project first, then keep-tier entries (stack output and post-stack
   work) only if budget pressure is still on. Entries with a live
   in-use marker from a running job are skipped — see ContentCache's
   lockfile protocol.

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

    tier: str = "bulk"
    """Storage tier of the producing node. `bulk` (pre-stack sequence data)
    or `keep` (stack output and everything downstream). Bulk evicts before
    keep. Defaults to bulk so an unknown tier doesn't accidentally protect
    a huge entry from eviction."""
    oldest_owner_updated_at: str | None = None
    """ISO timestamp of the least-recently-updated project that owns this
    entry. Used within a tier to evict oldest-project entries first."""


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

    # Map job_id -> {node_id: hash}. Pull every job we have on file; some
    # may belong to projects we've since deleted (orphan job rows), but
    # that's fine, we only walk history below, which references current
    # job_ids. Legacy list-shaped payloads are kept verbatim and resolved
    # below using the project's template node order, matching the old
    # storage behavior so old records don't silently change meaning.
    job_hashes_raw: dict[str, dict[str, str] | list[str]] = {}
    for j in conn.execute("SELECT id, node_hashes_json FROM jobs").fetchall():
        if not j["node_hashes_json"]:
            continue
        try:
            raw = json.loads(j["node_hashes_json"])
        except (ValueError, TypeError):
            continue
        if isinstance(raw, dict):
            job_hashes_raw[j["id"]] = {
                str(k): str(v) for k, v in raw.items() if isinstance(v, str)
            }
        elif isinstance(raw, list):
            job_hashes_raw[j["id"]] = [h for h in raw if isinstance(h, str)]

    def _node_tier_map(template_json: str) -> dict[str, str]:
        try:
            t = Template.model_validate(json.loads(template_json))
        except Exception:
            return {}
        out: dict[str, str] = {}
        for spec in t.nodes:
            try:
                cls: type[Node] = registry_lookup(spec.kind, spec.variant)
                out[spec.id] = cls.tier
            except KeyError:
                continue
        return out

    # We don't have a stored mapping from node_hash to its (kind, variant),
    # so we approximate: walk each history entry, infer per-node tier from
    # the template, and assign each hash the tier of any node that produced
    # it. Multiple projects that touched the same hash always run the same
    # node implementation, so tier is stable; if a tier mismatch ever shows
    # up we promote to `keep` to avoid evicting something a downstream user
    # is counting on.
    tier_rank = {"bulk": 0, "keep": 1}

    for pid, p in projects.items():
        tier_map = _node_tier_map(p["template_json"])
        template = Template.model_validate(json.loads(p["template_json"]))
        # Look up specs by node_id rather than list position so YAML
        # declaration order can differ from topo execution order without
        # mis-attributing tier and oldest-owner timestamps to the wrong
        # node.
        spec_by_id = {spec.id: spec for spec in template.nodes}
        project_updated = p["updated_at"]
        history = conn.execute(
            "SELECT seq, job_id, created_at FROM project_history "
            "WHERE project_id = ? ORDER BY seq ASC",
            (pid,),
        ).fetchall()
        for h in history:
            raw = job_hashes_raw.get(h["job_id"])
            if raw is None:
                continue
            if isinstance(raw, list):
                # Legacy list payload: pair by template list index, matching
                # the pre-fix behavior so old records keep their existing
                # (possibly imperfect) attribution rather than going dark.
                pairs: list[tuple[str, str]] = [
                    (template.nodes[i].id, hash_str)
                    for i, hash_str in enumerate(raw)
                    if i < len(template.nodes)
                ]
            else:
                pairs = list(raw.items())
            for node_id, hash_str in pairs:
                if hash_str not in entries:
                    continue
                entry = entries[hash_str]
                entry.owners.add(pid)
                spec = spec_by_id.get(node_id)
                if spec is not None:
                    tier = tier_map.get(spec.id, "bulk")
                    if tier_rank.get(tier, 0) > tier_rank.get(entry.tier, 0):
                        entry.tier = tier
                if entry.oldest_owner_updated_at is None or (
                    project_updated < entry.oldest_owner_updated_at
                ):
                    entry.oldest_owner_updated_at = project_updated

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
        alive = _alive_job_ids(conn)

    reason = "project-delete-keep-outputs" if keep_outputs else "project-delete"
    candidates = [
        h for h, e in entries.items()
        if e.owners == {project_id} and h not in keep_hashes
    ]
    log.info(
        "purge_project_cache start: project=%s keep_outputs=%s candidates=%d alive_jobs=%d",
        project_id,
        keep_outputs,
        len(candidates),
        len(alive),
    )
    evicted = 0
    freed = 0
    skipped_in_use = 0
    for h in candidates:
        if cache.is_in_use(h, alive):
            skipped_in_use += 1
            log.warning(
                "purge_project_cache skip in-use: project=%s hash=%s held_by=%s",
                project_id,
                h[:12],
                sorted(cache.in_use_by(h) & alive),
            )
            continue
        freed += cache.evict(h, reason=reason)
        evicted += 1
    log.info(
        "purge_project_cache done: project=%s evicted=%d freed=%d skipped_in_use=%d",
        project_id,
        evicted,
        freed,
        skipped_in_use,
    )
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
# Eviction ordering
# ---------------------------------------------------------------------------


# Pre-stack bulk dwarfs everything downstream by orders of magnitude (think
# 1 TB of registered FITS vs ~100 MB of post-stack edits). Eviction picks
# tiers in this order: orphans (no owners) regardless of tier, then bulk
# entries oldest-project-first, then keep entries only if budget is still
# blown. Within each tier we evict the entry whose oldest-owning project
# has the stalest updated_at first, so an iterative-stretch project doesn't
# hand its register cache to an abandoned target's history.
_TIER_ORDER = {"bulk": 0, "keep": 1}


def _eviction_key(entry: CacheEntryInfo) -> tuple[int, int, str]:
    """Sort key: lower = evict sooner.

    Position 0: orphan-flag (0 = no owners, 1 = has owners) so unreachable
    entries always go first.
    Position 1: tier rank (bulk=0, keep=1).
    Position 2: oldest-owner timestamp ascending; unknown sorts after
    populated values so we don't accidentally prioritize an undated entry.
    """
    has_owners = 1 if entry.owners else 0
    tier_rank = _TIER_ORDER.get(entry.tier, 0)
    age_key = entry.oldest_owner_updated_at or "￿"
    return (has_owners, tier_rank, age_key)


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
    skipped_in_use_count: int = 0
    """How many entries the sweep wanted to evict but couldn't because a
    live job was reading or writing them. Surfaced so the UI can hint
    'waiting on running jobs' when over_budget is True for that reason."""


def _alive_job_ids(conn: sqlite3.Connection) -> set[str]:
    """Return job_ids currently in queued/running state.

    Eviction uses this to decide which `_inuse_*` markers in the cache
    are still load-bearing vs stale (worker crashed without releasing).
    """
    rows = conn.execute(
        "SELECT id FROM jobs WHERE status IN ('queued', 'running')"
    ).fetchall()
    return {r["id"] for r in rows}


def run_cleanup(
    cache: ContentCache,
    *,
    max_bytes: int,
    db_path: Path | None = None,
) -> CleanupResult:
    """Evict cache entries until total usage is <= max_bytes.

    Eviction order (lower = evict first):
      1. Orphans (no project owns them) regardless of tier.
      2. Bulk-tier entries (pre-stack sequence data), oldest project first.
      3. Keep-tier entries (stack output and post-stack work). Only touched
         when 1 and 2 didn't reclaim enough.

    Entries with a live in-use marker are skipped regardless of position;
    the lockfile protocol guarantees a running job's reads/writes survive
    a concurrent cleanup.
    """
    with _conn(db_path) as conn:
        entries, _ = build_reachability(conn, cache)
        alive = _alive_job_ids(conn)

    total = sum(e.bytes for e in entries.values())
    if total <= max_bytes:
        log.info(
            "run_cleanup skipped: under budget (total=%d max=%d entries=%d)",
            total,
            max_bytes,
            len(entries),
        )
        return CleanupResult(
            evicted_count=0,
            bytes_freed=0,
            bytes_remaining=total,
            over_budget=False,
        )

    ordered = sorted(entries.values(), key=_eviction_key)
    log.info(
        "run_cleanup start: total=%d max=%d over_by=%d entries=%d alive_jobs=%d",
        total,
        max_bytes,
        total - max_bytes,
        len(entries),
        len(alive),
    )

    evicted_count = 0
    bytes_freed = 0
    skipped_in_use = 0
    for e in ordered:
        if total - bytes_freed <= max_bytes:
            break
        if cache.is_in_use(e.node_hash, alive):
            skipped_in_use += 1
            log.warning(
                "run_cleanup skip in-use: hash=%s bytes=%d held_by=%s",
                e.node_hash[:12],
                e.bytes,
                sorted(cache.in_use_by(e.node_hash) & alive),
            )
            continue
        if not e.owners:
            reason = "orphan"
        elif e.tier == "keep":
            reason = "over-budget-keep"
        else:
            reason = "over-budget-bulk"
        log.info(
            "run_cleanup pick: hash=%s reason=%s bytes=%d tier=%s owners=%d "
            "oldest_owner=%s",
            e.node_hash[:12],
            reason,
            e.bytes,
            e.tier,
            len(e.owners),
            e.oldest_owner_updated_at or "never",
        )
        bytes_freed += cache.evict(e.node_hash, reason=reason)
        evicted_count += 1

    over_budget = (total - bytes_freed) > max_bytes
    log.info(
        "run_cleanup done: evicted=%d freed=%d remaining=%d over_budget=%s skipped_in_use=%d",
        evicted_count,
        bytes_freed,
        total - bytes_freed,
        over_budget,
        skipped_in_use,
    )
    return CleanupResult(
        evicted_count=evicted_count,
        bytes_freed=bytes_freed,
        bytes_remaining=total - bytes_freed,
        over_budget=over_budget,
        skipped_in_use_count=skipped_in_use,
    )


# ---------------------------------------------------------------------------
# Per-operation disk headroom
# ---------------------------------------------------------------------------


class InsufficientStorageError(RuntimeError):
    """Raised when a node needs more bytes than we can possibly free up.

    Carries structured fields so the UI can render a clean failure card
    instead of dumping a traceback. The runtime wraps this in node_failed
    with a human-readable message.
    """

    def __init__(self, *, need_bytes: int, free_bytes: int, evicted_bytes: int):
        self.need_bytes = need_bytes
        self.free_bytes = free_bytes
        self.evicted_bytes = evicted_bytes
        super().__init__(self._format())

    def _format(self) -> str:
        def _gb(n: int) -> str:
            return f"{n / 1_000_000_000:.1f} GB"
        return (
            f"not enough disk space: need {_gb(self.need_bytes)}, "
            f"only {_gb(self.free_bytes)} free after evicting "
            f"{_gb(self.evicted_bytes)} from cache"
        )


def ensure_disk_headroom(
    cache: ContentCache,
    *,
    need_bytes: int,
    cache_max_bytes: int,
    db_path: Path | None = None,
) -> CleanupResult | None:
    """Make sure `need_bytes` will fit before a node writes.

    Headroom is bounded by both the physical disk free space AND the
    configured cache budget: a 1 TB cache cap doesn't help if only 20 GB
    are left on the partition. Both constraints apply:
      - cache_used' must leave (cache_max_bytes - cache_used') >= need_bytes
      - disk_free + (cache_used - cache_used') >= need_bytes

    Returns the CleanupResult if a sweep ran, or None if we were already
    under threshold. Raises InsufficientStorageError when even an
    aggressive sweep can't get us there.
    """
    disk = _disk_usage(cache.root)
    with _conn(db_path) as conn:
        entries, _ = build_reachability(conn, cache)
    cache_used = sum(e.bytes for e in entries.values())

    budget_headroom = max(0, cache_max_bytes - cache_used)
    headroom = min(disk.free_bytes, budget_headroom)
    if headroom >= need_bytes:
        return None

    # Sweep enough to satisfy whichever constraint is binding.
    disk_deficit = max(0, need_bytes - disk.free_bytes)
    budget_deficit = max(0, need_bytes - budget_headroom)
    target_max = max(0, min(
        cache_max_bytes - need_bytes,
        cache_used - max(disk_deficit, budget_deficit),
    ))
    log.info(
        "ensure_disk_headroom sweep: need=%d disk_free=%d cache_used=%d "
        "budget=%d target_max=%d",
        need_bytes, disk.free_bytes, cache_used, cache_max_bytes, target_max,
    )
    result = run_cleanup(cache, max_bytes=target_max, db_path=db_path)

    disk_after = _disk_usage(cache.root)
    if disk_after.free_bytes < need_bytes:
        raise InsufficientStorageError(
            need_bytes=need_bytes,
            free_bytes=disk_after.free_bytes,
            evicted_bytes=result.bytes_freed,
        )
    return result


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

SETTING_SITE_LATITUDE = "site_latitude"
SETTING_SITE_LONGITUDE = "site_longitude"
SETTING_SITE_ELEVATION_M = "site_elevation_m"
"""Observer site coordinates for the Tonight planner. Latitude in
decimal degrees (north positive), longitude in decimal degrees (east
positive, IAU convention), elevation in meters above sea level. All
three must be set before /api/tonight will compute alt/az; partial
configs return 400 so the user fixes the gap explicitly instead of
getting silently-wrong sky math from a hardcoded fallback."""

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
