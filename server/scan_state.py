"""Thread-safe in-memory scan progress state.

A single process-global ScanState is updated by the scanner thread and
read by the GET /api/scan/status endpoint. The state is intentionally
lost on restart (not persisted to the DB).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class ScanState:
    running: bool = False
    started_at: float | None = None
    finished_at: float | None = None
    current_path: str | None = None
    discovered: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    removed: int = 0
    failed: int = 0
    error: str | None = None
    # Snapshot of the most recent ScanStats counters (set on finish).
    last_stats: dict[str, Any] | None = None


_state = ScanState()
_lock = threading.Lock()


def snapshot() -> dict[str, Any]:
    """Return a copy of the current state as a plain dict."""
    with _lock:
        return {
            "running": _state.running,
            "started_at": _state.started_at,
            "finished_at": _state.finished_at,
            "current_path": _state.current_path,
            "discovered": _state.discovered,
            "inserted": _state.inserted,
            "updated": _state.updated,
            "skipped": _state.skipped,
            "removed": _state.removed,
            "failed": _state.failed,
            "error": _state.error,
            "last_stats": _state.last_stats,
        }


def start() -> bool:
    """Mark a scan as started. Returns False if one is already running."""
    with _lock:
        if _state.running:
            return False
        _state.running = True
        _state.started_at = time.time()
        _state.finished_at = None
        _state.current_path = None
        _state.discovered = 0
        _state.inserted = 0
        _state.updated = 0
        _state.skipped = 0
        _state.removed = 0
        _state.failed = 0
        _state.error = None
        _state.last_stats = None
        return True


def update(discovered: int, current_path: str, stats: Any | None = None) -> None:
    """Update progress counters. Called from inside the scan loop.

    ``discovered`` and ``current_path`` are always provided. The optional
    ``stats`` object carries the finer-grained counters (inserted, updated,
    failed, skipped) when available.
    """
    with _lock:
        _state.current_path = current_path
        _state.discovered = discovered
        if stats is not None:
            _state.inserted = stats.inserted
            _state.updated = stats.updated
            _state.skipped = stats.skipped_unchanged
            _state.failed = stats.failed


def finish(stats: Any | None = None, error: str | None = None) -> None:
    """Mark the scan as finished (success or error)."""
    with _lock:
        _state.running = False
        _state.finished_at = time.time()
        _state.current_path = None
        _state.error = error
        if stats is not None:
            _state.last_stats = {
                "discovered": stats.discovered,
                "inserted": stats.inserted,
                "updated": stats.updated,
                "skipped_unchanged": stats.skipped_unchanged,
                "removed": stats.removed,
                "failed": stats.failed,
                "masters_inserted": stats.masters_inserted,
                "masters_updated": stats.masters_updated,
                "masters_removed": stats.masters_removed,
                "masters_skipped": stats.masters_skipped,
            }
