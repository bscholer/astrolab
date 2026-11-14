"""Copy what can't be remade to the NAS: raw captures, finished images, the database.

Turned on by ASTROLAB_NAS_ROOT (a mounted folder). Runs hourly. Adds and updates files,
never deletes them, except database snapshots older than DB_KEEP_DAYS (the curator
deletes scrap folders it purges). A scope session folder is copied only once the
curator has kept all its sessions (server/curator.py), so scrap never lands here, and
of its failed_* frames only those triage rescued. Layout:

    captures/<session folder>/...         kept sessions' frames, darks, factory masters
    images/<project>/<project> v<seq>.png  every completed version's picture
    images/<project>/<project> v<seq> <port>.fit  stacked and final FITS
    db/astrolab-<date>.sqlite              a daily snapshot

The captures and app (images, db) folders can be moved with ASTROLAB_NAS_CAPTURES_DIR
and ASTROLAB_NAS_APP_DIR; see nas_captures and nas_app.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from server.catalog.db import connect as connect_catalog
from server.catalog.triage import rescued_paths
from server.curator import FOLDER_PREFIX, kept_folders
from server.paths import nas_app, nas_captures
from server.telemetry import note, traced

log = logging.getLogger(__name__)

EVERY_S = 3600
SETTLE_S = 600
"""Leave capture files alone until they are this old; the mirror may still be copying."""
DB_KEEP_DAYS = 30
FITS_PORTS = ("stacked", "recombined", "stretched")
"""The stacked FITS and the first of these finals the template has."""


def nas_root() -> Path | None:
    root = os.environ.get("ASTROLAB_NAS_ROOT", "").strip()
    return Path(root) if root else None


def _copy_if_changed(src: Path, dst: Path) -> int:
    """Copy src to dst unless a same-size copy is there. Returns bytes copied."""
    size = src.stat().st_size
    try:
        if dst.stat().st_size == size:
            return 0
    except FileNotFoundError:
        pass
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    shutil.copyfile(src, tmp)
    tmp.replace(dst)
    return size


def _safe(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|]+", "_", name).strip() or "unnamed"


def copy_captures(
    captures: Path,
    dest: Path,
    now: float | None = None,
    rescued: set[str] | None = None,
    kept: set[str] | None = None,
) -> int:
    """Copy settled capture files. failed_* frames are skipped unless triage rescued them.
    With `kept`, scope session folders (DWARF_RAW_*) are copied only if listed there."""
    now = time.time() if now is None else now
    rescued = rescued or set()
    copied = 0
    for src in captures.rglob("*"):
        if not src.is_file() or src.name.endswith(".part"):
            continue
        if src.name.startswith("failed_") and str(src) not in rescued:
            continue
        if (
            kept is not None
            and src.parent.name.startswith(FOLDER_PREFIX)
            and str(src.parent) not in kept
        ):
            continue
        if now - src.stat().st_mtime < SETTLE_S:
            continue
        copied += _copy_if_changed(src, dest / src.relative_to(captures))
    return copied


def _resolve(ref: dict, cache_root: Path | None) -> Path | None:
    """Find an output on disk. Stored paths can name an old cache root, so look the
    file up under the current root by its hash."""
    path = Path(ref.get("path") or "")
    h = ref.get("node_hash")
    if cache_root is not None and h and h in path.parts:
        moved = cache_root.joinpath(h, *path.parts[path.parts.index(h) + 1 :])
        if moved.is_file():
            return moved
    return path if path.is_file() else None


def copy_images(conn: sqlite3.Connection, dest: Path, cache_root: Path | None = None) -> int:
    rows = conn.execute(
        """
        SELECT p.name, h.seq, j.outputs_json
        FROM project_history h
        JOIN projects p ON p.id = h.project_id
        JOIN jobs j ON j.id = h.job_id
        WHERE j.status = 'completed' AND j.outputs_json IS NOT NULL
        """
    ).fetchall()
    copied = 0
    for name, seq, outputs_json in rows:
        try:
            outputs = json.loads(outputs_json)
        except ValueError:
            continue
        base = _safe(name)
        finals = [p for p in FITS_PORTS[1:] if outputs.get(p)]
        for port in ["image", "stacked", *finals[:1]]:
            ref = outputs.get(port)
            src = _resolve(ref, cache_root) if isinstance(ref, dict) else None
            if src is None:  # evicted from the cache; the NAS keeps what it already got
                continue
            label = "" if port == "image" else f" {port}"
            copied += _copy_if_changed(src, dest / base / f"{base} v{seq}{label}{src.suffix}")
    return copied


def snapshot_db(db_path: Path, dest: Path, today: datetime | None = None) -> bool:
    today = today or datetime.now(UTC)
    dst = dest / f"astrolab-{today:%Y-%m-%d}.sqlite"
    made = False
    if not dst.exists():
        dest.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".part")
        src = sqlite3.connect(db_path)
        out = sqlite3.connect(tmp)
        try:
            src.backup(out)
        finally:
            out.close()
            src.close()
        tmp.replace(dst)
        made = True
    cutoff = f"astrolab-{today - timedelta(days=DB_KEEP_DAYS):%Y-%m-%d}.sqlite"
    for old in dest.glob("astrolab-*.sqlite"):
        if old.name < cutoff:
            old.unlink(missing_ok=True)
    return made


class NasBackup:
    def __init__(
        self,
        get_root: Callable[[], Path | None],
        get_captures: Callable[[], Path | None],
        db_path: Path,
        cache_root: Callable[[], Path | None] = lambda: None,
    ) -> None:
        self._root = get_root
        self._captures = get_captures
        self._db_path = db_path
        self._cache_root = cache_root
        self.last: dict = {}

    @traced("nas.backup")
    def run(self) -> dict:
        root = self._root()
        if root is None:
            return {}
        result: dict = {"at": datetime.now(UTC).isoformat()}
        steps: list[tuple[str, Callable[[], object]]] = [
            ("db", lambda: snapshot_db(self._db_path, nas_app(root) / "db")),
            ("images", self._images),
        ]
        captures = self._captures()
        if captures is not None and captures.is_dir():
            steps.append(("captures", lambda: self._copy_captures(captures, root)))
        for name, step in steps:
            try:
                result[name] = step()
            except Exception as exc:  # one failing part must not stop the others
                log.warning("NAS backup of %s failed: %s", name, exc)
                result[name] = f"failed: {exc}"
        note(**{f"nas.{k}": str(v) for k, v in result.items()})
        log.info("NAS backup: %s", result)
        self.last = result
        return result

    def _copy_captures(self, captures: Path, root: Path) -> int:
        conn = connect_catalog(self._db_path)
        try:
            rescued, kept = rescued_paths(conn), kept_folders(conn)
        finally:
            conn.close()
        return copy_captures(captures, nas_captures(root), rescued=rescued, kept=kept)

    def _images(self) -> int:
        conn = sqlite3.connect(self._db_path)
        try:
            return copy_images(conn, nas_app(self._root()) / "images", self._cache_root())
        finally:
            conn.close()

    def start(self) -> None:
        if self._root() is None:
            return

        def loop() -> None:
            while True:
                self.run()
                time.sleep(EVERY_S)

        threading.Thread(target=loop, name="astrolab-nas", daemon=True).start()
