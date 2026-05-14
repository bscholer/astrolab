"""Content-addressed cache.

The cache stores node outputs under `<root>/<node_hash>/`. The directory
layout for an output port is whatever the node wrote (commonly
`<port>.<ext>` or a `<port>/` subdirectory, but nodes are free to use any
filename). Presence of a `_done` marker file inside the directory means
the entry is committed; absence means it's incomplete (e.g. a crashed
run) and should be ignored. A `_outputs.json` manifest sits next to
`_done` and records the port -> relative-path mapping the node returned,
so the runtime can re-hydrate Refs on a cache hit without guessing at
filenames.

Phase 0 is filesystem-only. The README mentions a SQLite reverse index
`(node_id, params_hash) -> node_hash` for fast lookup; we defer that until
the catalog lands in Phase 1, since for the Phase 0 demo a directory probe
is plenty.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
from collections.abc import Mapping
from pathlib import Path

from .models import Ref
from .paths import cache_root
from .ports import PortType

log = logging.getLogger("astrolab.cache")

DONE_MARKER: str = "_done"
OUTPUTS_MANIFEST: str = "_outputs.json"
INUSE_PREFIX: str = "_inuse_"
"""An empty file `_inuse_<job_id>` inside an entry dir means a live job is
reading or writing that entry. Eviction must skip directories with any
live in-use marker — see is_in_use() / release_job_marks(). Markers are
left behind by jobs that crash without running their finally cleanup;
the eviction path treats markers whose job_id is no longer in the
queued/running set as stale and removes them opportunistically."""


class ContentCache:
    """Filesystem-backed content-addressed cache."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else cache_root()
        self.root.mkdir(parents=True, exist_ok=True)

    def entry_dir(self, node_hash: str) -> Path:
        return self.root / node_hash

    def is_committed(self, node_hash: str) -> bool:
        return (self.entry_dir(node_hash) / DONE_MARKER).exists()

    def lookup(self, node_hash: str) -> Path | None:
        """Return the entry directory if the cache holds a committed entry, else None."""
        if self.is_committed(node_hash):
            return self.entry_dir(node_hash)
        return None

    def reserve(self, node_hash: str, *, force: bool = False) -> Path:
        """Create (or reset) the entry directory and return its path.

        If a half-written entry exists (no _done marker), it gets cleared.
        A committed entry is normally left alone; pass force=True to wipe
        it (used by the 'Reprocess' flow when bypassing cache lookup).
        """
        d = self.entry_dir(node_hash)
        if d.exists() and (force or not self.is_committed(node_hash)):
            reason = "force-rerun" if force and self.is_committed(node_hash) else "half-written"
            log.info(
                "cache reserve wipe: hash=%s reason=%s path=%s",
                node_hash[:12],
                reason,
                d,
            )
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def commit(self, node_hash: str, outputs: Mapping[str, Ref]) -> dict[str, Ref]:
        """Mark an entry as complete and return the committed Refs.

        Caller is responsible for having written each output file under the
        entry directory before calling commit. Writes a `_outputs.json`
        manifest alongside `_done` so the lookup path can rehydrate the
        Refs without guessing at filenames: multi-output nodes can use
        whatever naming scheme they like (eg narrowband_extract writes
        `r_results_ha.fit` for port `ha`).
        """
        d = self.entry_dir(node_hash)
        if not d.exists():
            raise RuntimeError(f"cache: cannot commit non-reserved entry {node_hash}")
        manifest: dict[str, dict[str, str | bool]] = {}
        for port, ref in outputs.items():
            if not ref.path.exists():
                raise RuntimeError(f"cache: output '{port}' missing at {ref.path} for {node_hash}")
            try:
                rel = ref.path.resolve().relative_to(d.resolve())
            except ValueError as exc:
                raise RuntimeError(
                    f"cache: output '{port}' path {ref.path} escapes entry dir {d}"
                ) from exc
            manifest[port] = {
                "path": rel.as_posix(),
                "type": str(ref.type),
                "display_ready": ref.display_ready,
            }
        # Write manifest before _done so a crash mid-commit leaves an
        # incomplete entry (no _done marker), not a committed one with a
        # missing manifest.
        (d / OUTPUTS_MANIFEST).write_text(json.dumps(manifest, indent=2))
        (d / DONE_MARKER).touch()
        return dict(outputs)

    def load_outputs(self, node_hash: str) -> dict[str, Ref] | None:
        """Return the committed Refs for an entry from its manifest.

        Returns None when the entry is missing, uncommitted, or predates
        the manifest format (legacy entries that only have `_done`). The
        runtime falls back to convention-based lookup in that case so
        users don't have to nuke their cache after upgrading.
        """
        d = self.entry_dir(node_hash)
        manifest_path = d / OUTPUTS_MANIFEST
        if not self.is_committed(node_hash) or not manifest_path.exists():
            return None
        try:
            raw = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        out: dict[str, Ref] = {}
        for port, entry in raw.items():
            rel = entry.get("path")
            type_str = entry.get("type")
            if not isinstance(rel, str) or not isinstance(type_str, str):
                return None
            try:
                port_type = PortType(type_str)
            except ValueError:
                return None
            out[port] = Ref(
                node_hash=node_hash,
                port=port,
                path=d / rel,
                type=port_type,
                display_ready=bool(entry.get("display_ready", False)),
            )
        return out

    def evict(self, node_hash: str, *, reason: str = "unspecified") -> int:
        """Remove a committed cache entry. Returns bytes freed (best-effort
        rollup of file sizes within the entry dir before deletion).

        No-op if the entry doesn't exist. Used by storage cleanup; the
        runtime never calls this on a live entry because entries are only
        evicted when no live job references them.

        `reason` is logged so a forensic trail exists when a job later
        complains its inputs vanished. Pass e.g. "orphan", "over-budget",
        "project-delete", "session-swap".
        """
        d = self.entry_dir(node_hash)
        if not d.exists():
            log.debug("cache evict no-op: hash=%s reason=%s (path missing)", node_hash[:12], reason)
            return 0
        bytes_freed = 0
        for path in d.rglob("*"):
            if path.is_file() and not path.is_symlink():
                with contextlib.suppress(OSError):
                    bytes_freed += path.stat().st_size
        log.info(
            "cache evict: hash=%s reason=%s bytes=%d path=%s",
            node_hash[:12],
            reason,
            bytes_freed,
            d,
        )
        shutil.rmtree(d, ignore_errors=True)
        return bytes_freed

    def all_committed_hashes(self) -> list[str]:
        """List every committed node_hash directory under the cache root.

        Skips half-written entries (no _done marker). Used by storage
        accounting to enumerate everything currently on disk.
        """
        if not self.root.exists():
            return []
        out: list[str] = []
        for child in self.root.iterdir():
            if not child.is_dir():
                continue
            if (child / DONE_MARKER).exists():
                out.append(child.name)
        return out

    def mark_in_use(self, node_hash: str, job_id: str) -> None:
        """Stamp a cache entry as currently in-use by `job_id`.

        Idempotent. No-op if the entry dir doesn't exist (e.g. the runtime
        decided to mark a hash that hasn't been reserved yet — that's a
        caller bug, not a crash condition).
        """
        d = self.entry_dir(node_hash)
        if not d.exists():
            return
        with contextlib.suppress(OSError):
            (d / f"{INUSE_PREFIX}{job_id}").touch()

    def release_job_marks(self, job_id: str) -> int:
        """Remove every `_inuse_<job_id>` marker under the cache root.

        Returns the count removed. Called from JobWorker._terminate so a
        job's locks evaporate the moment it stops running, regardless of
        success/failure/cancel.
        """
        marker_name = f"{INUSE_PREFIX}{job_id}"
        removed = 0
        if not self.root.exists():
            return 0
        for child in self.root.iterdir():
            if not child.is_dir():
                continue
            m = child / marker_name
            if m.exists():
                with contextlib.suppress(OSError):
                    m.unlink()
                    removed += 1
        if removed:
            log.info("cache release marks: job=%s removed=%d", job_id[:8], removed)
        return removed

    def in_use_by(self, node_hash: str) -> set[str]:
        """Return job_ids currently holding an in-use marker on this entry.

        Pure filesystem read — doesn't check whether the listed jobs are
        actually alive. Callers that need to drop stale markers go through
        is_in_use(alive_job_ids) instead.
        """
        d = self.entry_dir(node_hash)
        if not d.exists():
            return set()
        return {
            p.name[len(INUSE_PREFIX):]
            for p in d.iterdir()
            if p.is_file() and p.name.startswith(INUSE_PREFIX)
        }

    def is_in_use(self, node_hash: str, alive_job_ids: set[str]) -> bool:
        """True if any in-use marker on this entry names a job in
        `alive_job_ids`. Stale markers (job_id not in alive set) are
        deleted opportunistically so the cache self-heals after a worker
        crash.
        """
        d = self.entry_dir(node_hash)
        if not d.exists():
            return False
        live = False
        for p in d.iterdir():
            if not p.is_file() or not p.name.startswith(INUSE_PREFIX):
                continue
            jid = p.name[len(INUSE_PREFIX):]
            if jid in alive_job_ids:
                live = True
                continue
            with contextlib.suppress(OSError):
                p.unlink()
        return live

    def entry_size(self, node_hash: str) -> int:
        """Return the total bytes occupied by a single cache entry. 0 if
        the entry is missing. Hardlinks count toward the size from the
        cache's perspective even though they share inodes — disambiguating
        across-cache hardlink savings would require an inode-set walk we
        don't need yet."""
        d = self.entry_dir(node_hash)
        if not d.exists():
            return 0
        total = 0
        for path in d.rglob("*"):
            if path.is_file() and not path.is_symlink():
                with contextlib.suppress(OSError):
                    total += path.stat().st_size
        return total
