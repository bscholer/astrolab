"""Content-addressed cache.

The cache stores node outputs at `<root>/<node_hash>/<port>.<ext>`. Presence
of a `_done` marker file inside the directory means the entry is committed;
absence means it's incomplete (e.g. a crashed run) and should be ignored.
This avoids ever serving a half-written artifact as a cache hit.

Phase 0 is filesystem-only. The README mentions a SQLite reverse index
`(node_id, params_hash) -> node_hash` for fast lookup; we defer that until
the catalog lands in Phase 1, since for the Phase 0 demo a directory probe
is plenty.
"""

from __future__ import annotations

import contextlib
import shutil
from collections.abc import Mapping
from pathlib import Path

from .models import Ref
from .paths import cache_root

DONE_MARKER: str = "_done"


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
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def commit(self, node_hash: str, outputs: Mapping[str, Ref]) -> dict[str, Ref]:
        """Mark an entry as complete and return the committed Refs.

        Caller is responsible for having written each output file under the
        entry directory before calling commit.
        """
        d = self.entry_dir(node_hash)
        if not d.exists():
            raise RuntimeError(f"cache: cannot commit non-reserved entry {node_hash}")
        for port, ref in outputs.items():
            if not ref.path.exists():
                raise RuntimeError(
                    f"cache: output '{port}' missing at {ref.path} for {node_hash}"
                )
        (d / DONE_MARKER).touch()
        return dict(outputs)

    def evict(self, node_hash: str) -> int:
        """Remove a committed cache entry. Returns bytes freed (best-effort
        rollup of file sizes within the entry dir before deletion).

        No-op if the entry doesn't exist. Used by storage cleanup; the
        runtime never calls this on a live entry because entries are only
        evicted when no live job references them.
        """
        d = self.entry_dir(node_hash)
        if not d.exists():
            return 0
        bytes_freed = 0
        for path in d.rglob("*"):
            if path.is_file() and not path.is_symlink():
                with contextlib.suppress(OSError):
                    bytes_freed += path.stat().st_size
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
