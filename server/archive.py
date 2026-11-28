"""Offload raw captures to the NAS when the disk runs short, and fetch them back.

With a NAS, the NAS holds the reference copy of every kept capture
(server/nas_backup.py) and the local capture root acts as a cache in front of
it. Without one, nothing here does anything and captures stay local.

Offloading is the last thing disk cleanup does: the cache's own entries go
first (server/storage.py), kept results never (server/results.py). A scope
session folder can go when:

- every session in it is kept (only those reach the NAS),
- its newest file is older than KEEP_LOCAL_DAYS, so recent nights, which the
  masters rebuild from, stay local,
- no queued or running job reads from it,
- every file the backup copies is on the NAS with the same size.

The frames that triage did not rescue (failed_*) are never backed up, so they
go with the folder; their catalog rows stay, so the library, the scheduler's
failed counts and the sky map are unchanged. The folder is recorded in
archived_folders, and stays there after a fetch (local = 1): the scanner keeps
its rows, the scope mirror doesn't copy it back, and the scope cleaner may
free it from the SD card. A fetched folder can be offloaded again.

Fetching happens before a job runs, not as a pipeline step: the worker copies
back every archived folder the job's frames live in (JobWorker._fetch_inputs).
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from server.catalog.triage import rescued_paths
from server.curator import FOLDER_PREFIX, kept_folders
from server.paths import nas_captures

log = logging.getLogger(__name__)

KEEP_LOCAL_DAYS = 7.0


class FetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class Archived:
    folder: str
    nas_path: str
    bytes: int
    files: int
    local: bool
    """Fetched back: on local disk again (without the unrescued failed frames)."""


def archived(conn: sqlite3.Connection) -> dict[str, Archived]:
    return {
        r[0]: Archived(r[0], r[1], int(r[2]), int(r[3]), bool(r[4]))
        for r in conn.execute(
            "SELECT folder, nas_path, bytes, files, local FROM archived_folders"
        )
    }


def archived_names(conn: sqlite3.Connection) -> set[str]:
    """Folder names, which is how the scope mirror and cleaner know sessions."""
    return {Path(f).name for f in archived(conn)}


def _backed_up(src: Path, rescued: set[str]) -> bool:
    """Whether the NAS backup copies this file (copy_captures' rule)."""
    if src.name.endswith(".part"):
        return False
    return not (src.name.startswith("failed_") and str(src) not in rescued)


def verify(folder: Path, nas_folder: Path, rescued: set[str]) -> tuple[int, int] | None:
    """(bytes, files) on the NAS when every backed-up file of `folder` is there
    with the same size, else None."""
    total = files = 0
    for src in folder.rglob("*"):
        if not src.is_file() or not _backed_up(src, rescued):
            continue
        try:
            size = src.stat().st_size
            if (nas_folder / src.relative_to(folder)).stat().st_size != size:
                return None
        except OSError:
            return None
        total += size
        files += 1
    return (total, files) if files else None


def _du(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _newest_mtime(path: Path) -> float:
    return max((p.stat().st_mtime for p in path.rglob("*") if p.is_file()), default=0.0)


def folders_in_use(conn: sqlite3.Connection) -> set[str]:
    """Capture folders that queued or running jobs read frames from."""
    out: set[str] = set()
    for (job_json,) in conn.execute(
        "SELECT job_json FROM jobs WHERE status IN ('queued', 'running')"
    ):
        try:
            inputs = (json.loads(job_json or "{}") or {}).get("inputs") or {}
        except ValueError:
            continue
        out |= _frame_folders(inputs)
    return out


def _frame_folders(inputs: dict) -> set[str]:
    """Folders the frames of a job's light inputs live in. Lights are staged as
    a folder of symlinks (job_builder); a link can point at an offloaded file."""
    out: set[str] = set()
    for key, ref in inputs.items():
        if not key.endswith(".lights") or not isinstance(ref, dict):
            continue
        stage = Path(str(ref.get("path") or ""))
        if not stage.is_dir():
            continue
        for link in stage.iterdir():
            if link.is_symlink():
                out.add(str(Path(link.readlink()).parent))
    return out


def offload(
    conn: sqlite3.Connection,
    captures: Path,
    nas: Path,
    need_bytes: int,
    *,
    protect: set[str] | None = None,
    now: float | None = None,
) -> int:
    """Delete verified, settled capture folders, oldest first, until `need_bytes`
    are freed or none qualify. Returns bytes freed."""
    now = time.time() if now is None else now
    protect = (protect or set()) | folders_in_use(conn)
    done = archived(conn)
    candidates: list[tuple[float, Path]] = []
    for folder in kept_folders(conn):
        path = Path(folder)
        if (
            (folder in done and not done[folder].local)
            or folder in protect
            or path.parent != captures
            or not path.name.startswith(FOLDER_PREFIX)
            or not path.is_dir()
        ):
            continue
        newest = _newest_mtime(path)
        if now - newest < KEEP_LOCAL_DAYS * 86400:
            continue
        candidates.append((newest, path))
    candidates.sort()
    rescued = rescued_paths(conn)
    freed = 0
    for _newest, path in candidates:
        if freed >= need_bytes:
            break
        nas_folder = nas_captures(nas) / path.relative_to(captures)
        on_nas = verify(path, nas_folder, rescued)
        if on_nas is None:
            log.info("offload: %s is not fully on the NAS yet, keeping it", path.name)
            continue
        local = _du(path)
        with conn:  # record first: a crash mid-delete must not lose the frames' rows
            conn.execute(
                "INSERT OR REPLACE INTO archived_folders"
                " (folder, nas_path, bytes, files, archived_at, local)"
                " VALUES (?, ?, ?, ?, ?, 0)",
                (str(path), str(nas_folder), on_nas[0], on_nas[1], now),
            )
        shutil.rmtree(path, ignore_errors=True)
        freed += local
        log.warning("offloaded %s to the NAS (%.1f GB freed)", path.name, local / 1e9)
    return freed


def needed(conn: sqlite3.Connection, inputs: dict) -> list[Archived]:
    """Archived folders a job's frames live in."""
    done = archived(conn)
    return [
        done[f] for f in sorted(_frame_folders(inputs)) if f in done and not done[f].local
    ]


def fetch(
    conn: sqlite3.Connection,
    item: Archived,
    *,
    on_bytes: Callable[[int], None] = lambda _n: None,
    cancel: threading.Event | None = None,
) -> None:
    """Copy one archived folder back from the NAS and mark it local again."""
    src = Path(item.nas_path)
    dst = Path(item.folder)
    if not src.is_dir():
        raise FetchError(f"{dst.name} is not on the NAS at {src}")
    tmp = dst.with_name(dst.name + ".fetching")
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        for path in sorted(src.rglob("*")):
            if cancel is not None and cancel.is_set():
                raise FetchError("cancelled while fetching from the NAS")
            target = tmp / path.relative_to(src)
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)  # keeps mtimes, so the scanner sees old files
            on_bytes(path.stat().st_size)
        if dst.exists():  # files the mirror re-copied meanwhile; the NAS copy is whole
            shutil.rmtree(dst)
        tmp.rename(dst)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    with conn:
        conn.execute("UPDATE archived_folders SET local = 1 WHERE folder = ?", (item.folder,))
    log.info("fetched %s back from the NAS (%.1f GB)", dst.name, item.bytes / 1e9)
