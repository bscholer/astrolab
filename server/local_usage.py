"""What takes up the local disk the cache lives on, for the Library's storage tab.

The whole disk, by kind:

    captures   the capture root, when it is on the same disk
    results    kept results (server/results.py), the cache root's _kept folder
    cache      everything else in the cache: pipeline steps that can be rebuilt
    other      the rest of the used space: containerd images, other pods, ...
    reserved   free space jobs never use (storage.disk_floor_bytes)
    free       free space above that

Kept results are hard links to cache entries, so a file in both is counted
once, under results. A walk of a large cache takes a few seconds; it runs in
the background every half hour and the last result is kept in settings.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from server.cache import KEPT_DIR
from server.storage import _disk_usage, disk_floor_bytes
from server.telemetry import traced

log = logging.getLogger(__name__)

EVERY_S = 1800

KINDS: list[tuple[str, str]] = [
    ("captures", "Raw captures"),
    ("results", "Kept results"),
    ("cache", "Pipeline cache"),
    ("other", "Other files on the disk"),
    ("reserved", "Kept free for the node"),
    ("free", "Free"),
]
"""Kind id and label, in a fixed order so each kind keeps its color."""


def _walk(top: Path, skip: set[Path], seen: set[int], visit: Callable[[int], None]) -> None:
    """Sum files under `top`, each hard-linked inode once across walks."""
    stack = [top]
    while stack:
        here = stack.pop()
        try:
            entries = list(os.scandir(here))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    p = Path(e.path)
                    if p not in skip:
                        stack.append(p)
                elif e.is_file(follow_symlinks=False):
                    st = e.stat(follow_symlinks=False)
                    if st.st_nlink > 1:
                        if st.st_ino in seen:
                            continue
                        seen.add(st.st_ino)
                    visit(st.st_size)
            except OSError:
                continue


def measure(cache_root: Path, captures: Path | None) -> dict[str, Any]:
    sums = {k: {"bytes": 0, "files": 0} for k, _ in KINDS}

    def adder(kind: str) -> Callable[[int], None]:
        def add(size: int) -> None:
            sums[kind]["bytes"] += size
            sums[kind]["files"] += 1

        return add

    started = time.monotonic()
    seen: set[int] = set()
    kept = cache_root / KEPT_DIR
    _walk(kept, set(), seen, adder("results"))
    _walk(cache_root, {kept}, seen, adder("cache"))
    on_disk = False
    if captures is not None and captures.is_dir():
        try:
            on_disk = captures.stat().st_dev == cache_root.stat().st_dev
        except OSError:
            on_disk = False
        if on_disk:
            _walk(captures, set(), seen, adder("captures"))

    disk = _disk_usage(cache_root)
    floor = disk_floor_bytes(cache_root)
    ours = sums["results"]["bytes"] + sums["cache"]["bytes"] + sums["captures"]["bytes"]
    sums["other"]["bytes"] = max(0, disk.used_bytes - ours)
    sums["reserved"]["bytes"] = min(floor, disk.free_bytes)
    sums["free"]["bytes"] = max(0, disk.free_bytes - floor)
    return {
        "cache_root": str(cache_root),
        "captures_root": str(captures) if captures is not None else None,
        "captures_on_disk": on_disk,
        "measured_at": datetime.now(UTC).isoformat(),
        "seconds": round(time.monotonic() - started, 1),
        "kinds": [{"id": k, "label": label, **sums[k]} for k, label in KINDS],
        "disk": {
            "total_bytes": disk.total_bytes,
            "used_bytes": disk.used_bytes,
            "free_bytes": disk.free_bytes,
        },
        "floor_bytes": floor,
    }


class LocalUsage:
    """Keeps the last measurement and refreshes it in the background."""

    def __init__(
        self,
        get_cache_root: Callable[[], Path],
        get_captures: Callable[[], Path | None],
        load: Callable[[], dict | None],
        save: Callable[[dict], None],
    ) -> None:
        self._cache_root = get_cache_root
        self._captures = get_captures
        self._load = load
        self._save = save
        self._lock = threading.Lock()
        self._running = False
        self.error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {"measuring": self._running, "error": self.error, "last": self._load()}

    @traced("local.usage")
    def run(self) -> dict | None:
        with self._lock:
            if self._running:
                return None
            self._running = True
        try:
            result = measure(self._cache_root(), self._captures())
            self._save(result)
            self.error = None
            log.info("local disk usage measured in %ss", result["seconds"])
            return result
        except Exception as exc:
            log.warning("local disk usage walk failed: %s", exc)
            self.error = str(exc)
            return None
        finally:
            self._running = False

    def refresh(self) -> bool:
        if self._running:
            return False
        threading.Thread(target=self.run, name="astrolab-local-usage", daemon=True).start()
        return True

    def start(self) -> None:
        def loop() -> None:
            while True:
                self.run()
                time.sleep(EVERY_S)

        threading.Thread(target=loop, name="astrolab-local-usage-loop", daemon=True).start()
