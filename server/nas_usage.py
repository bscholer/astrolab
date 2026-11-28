"""What takes up space on the NAS, by kind, for the Library's storage tab.

Walking the share over NFS takes a minute or more (45k files took 75 s), so it runs
in a background thread every few hours and the last result is kept in settings.
Kinds follow the backup layout in server/nas_backup.py:

    captures/DWARF_RAW_*/failed_*      frames the scope could not register
    captures/DWARF_RAW_*/*.fit(s)      light frames
    captures/DWARF_RAW_*/other         the scope's own stacks, previews, thumbnails
    captures/<anything else>           calibration (darks, CALI_FRAME, ...)
    <app>/images                       finished pictures and FITS
    <app>/db                           database snapshots
    everything else on the share       other files
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from server.paths import nas_app, nas_captures
from server.scope_mirror import SESSION_PREFIX
from server.telemetry import traced

log = logging.getLogger(__name__)

EVERY_S = 6 * 3600

KINDS: list[tuple[str, str]] = [
    ("lights", "Light frames"),
    ("failed", "Failed frames"),
    ("scope_outputs", "Scope stacks and previews"),
    ("calibration", "Calibration"),
    ("images", "Finished images"),
    ("db", "Database snapshots"),
    ("other", "Other files"),
]
"""Kind id and label, in a fixed order so each kind keeps its color."""

FRAME_EXTS = (".fits", ".fit", ".fts")


def _kind_in_captures(rel: tuple[str, ...]) -> str:
    """rel is the path under the captures folder, file name last."""
    if len(rel) < 2:
        return "other"
    if not rel[0].startswith(SESSION_PREFIX):
        return "calibration"
    name = rel[-1].lower()
    if name.startswith("failed_"):
        return "failed"
    if name.endswith(FRAME_EXTS):
        return "lights"
    return "scope_outputs"


def _kind_in_app(rel: tuple[str, ...]) -> str:
    if len(rel) >= 2 and rel[0] in ("images", "db"):
        return rel[0]
    return "other"


def _walk(top: Path, visit: Callable[[tuple[str, ...], int], None], skip: set[Path]) -> None:
    stack: list[tuple[Path, tuple[str, ...]]] = [(top, ())]
    while stack:
        here, rel = stack.pop()
        try:
            entries = list(os.scandir(here))
        except OSError as exc:
            log.debug("skipping %s: %s", here, exc)
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    p = Path(e.path)
                    if p not in skip:
                        stack.append((p, (*rel, e.name)))
                elif e.is_file(follow_symlinks=False):
                    visit((*rel, e.name), e.stat(follow_symlinks=False).st_size)
            except OSError:
                continue


def measure(root: Path) -> dict[str, Any]:
    """Walk the share and sum bytes and files per kind."""
    sums = {k: {"bytes": 0, "files": 0} for k, _ in KINDS}

    def add(kind: str, size: int) -> None:
        sums[kind]["bytes"] += size
        sums[kind]["files"] += 1

    captures, app = nas_captures(root), nas_app(root)
    started = time.monotonic()
    if captures.is_dir() and captures != root:
        _walk(captures, lambda rel, n: add(_kind_in_captures(rel), n), set())
    if app.is_dir() and app != root:
        _walk(app, lambda rel, n: add(_kind_in_app(rel), n), {captures})
    # The rest of the share. When captures or the app folder is the root itself,
    # walk it here and sort each file by where it sits.
    def rest(rel: tuple[str, ...], n: int) -> None:
        if captures == root:
            add(_kind_in_captures(rel), n)
        elif app == root:
            add(_kind_in_app(rel), n)
        else:
            add("other", n)

    _walk(root, rest, {p for p in (captures, app) if p != root})

    disk = shutil.disk_usage(root)
    return {
        "root": str(root),
        "measured_at": datetime.now(UTC).isoformat(),
        "seconds": round(time.monotonic() - started, 1),
        "kinds": [{"id": k, "label": label, **sums[k]} for k, label in KINDS],
        "disk": {"total_bytes": disk.total, "used_bytes": disk.used, "free_bytes": disk.free},
    }


class NasUsage:
    """Keeps the last measurement and refreshes it in the background."""

    def __init__(
        self,
        get_root: Callable[[], Path | None],
        load: Callable[[], dict | None],
        save: Callable[[dict], None],
    ) -> None:
        self._root = get_root
        self._load = load
        self._save = save
        self._lock = threading.Lock()
        self._running = False
        self.error: str | None = None

    @property
    def running(self) -> bool:
        return self._running

    def snapshot(self) -> dict[str, Any]:
        root = self._root()
        return {
            "configured": root is not None,
            "measuring": self._running,
            "error": self.error,
            "last": self._load() if root is not None else None,
        }

    @traced("nas.usage")
    def run(self) -> dict | None:
        root = self._root()
        if root is None:
            return None
        with self._lock:
            if self._running:
                return None
            self._running = True
        try:
            result = measure(root)
            self._save(result)
            self.error = None
            log.info("NAS usage measured in %ss", result["seconds"])
            return result
        except Exception as exc:
            log.warning("NAS usage walk failed: %s", exc)
            self.error = str(exc)
            return None
        finally:
            self._running = False

    def refresh(self) -> bool:
        """Start a walk in the background. False when one is running or no NAS is set."""
        if self._root() is None or self._running:
            return False
        threading.Thread(target=self.run, name="astrolab-nas-usage", daemon=True).start()
        return True

    def start(self) -> None:
        # Always loop: the NAS can be set in Settings after startup.
        def loop() -> None:
            while True:
                if self._root() is None:
                    time.sleep(60)
                    continue
                last = self._load() or {}
                try:
                    age = (
                        datetime.now(UTC) - datetime.fromisoformat(last["measured_at"])
                    ).total_seconds()
                except (KeyError, TypeError, ValueError):
                    age = EVERY_S
                if age >= EVERY_S:
                    self.run()
                    age = 0
                time.sleep(max(60.0, EVERY_S - age))

        threading.Thread(target=loop, name="astrolab-nas-usage-loop", daemon=True).start()
