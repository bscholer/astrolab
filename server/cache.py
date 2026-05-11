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
import shutil
from collections.abc import Mapping
from pathlib import Path

from .models import Ref
from .paths import cache_root
from .ports import PortType

DONE_MARKER: str = "_done"
OUTPUTS_MANIFEST: str = "_outputs.json"


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
