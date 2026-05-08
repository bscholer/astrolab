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
