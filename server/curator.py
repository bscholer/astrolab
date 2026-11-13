"""Decide which capture sessions are worth keeping, and purge the scrap.

Every few minutes:

1. Triage new failed_* frames (server/catalog/triage.py).
2. Give each session a verdict once it can have one:
   - keep at once: it has SCRAP_BELOW_S of good frames, a project uses it, or
     it is short-exposure work (Moon, planets) that star counts can't judge;
   - otherwise wait until it has settled (no new frame for SETTLE_S) and its
     failed frames are triaged, then keep it if good + rescued frames reach
     SCRAP_BELOW_S, else call it scrap.
3. Purge scrap capture folders when the purge setting is on: delete them
   from the capture root and the NAS, and record a tombstone so the scope
   mirror never copies them back (the scope cleaner may then free them from
   the scope's card).

The library lists only kept sessions, and the NAS copy only takes folders
whose sessions are all kept, so scrap is copied off the scope but never
reaches either.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from server.catalog.db import connect as connect_catalog
from server.catalog.triage import triage_pending
from server.telemetry import traced

log = logging.getLogger(__name__)

SCRAP_BELOW_S = 600.0
"""Sessions with less usable exposure than this (10 minutes) are scrap."""
SETTLE_S = 3600.0
"""A session with no new frame for this long is finished."""
MIN_EXPTIME_S = 5.0
"""Shorter subs (Moon, planets) are kept: star counts say nothing about them."""
EVERY_S = 600
TRIAGE_BUDGET_S = 480
FOLDER_PREFIX = "DWARF_RAW_"


@dataclass
class _Session:
    id: int
    exptime: float | None
    newest_mtime: float = 0.0
    good: int = 0
    rescued: int = 0
    untriaged: int = 0
    folders: set[str] = field(default_factory=set)

    @property
    def usable_s(self) -> float:
        return (self.good + self.rescued) * (self.exptime or 0.0)


def _sessions(conn: sqlite3.Connection) -> dict[int, _Session]:
    out: dict[int, _Session] = {}
    for r in conn.execute(
        """
        SELECT s.id, s.exptime, f.path, f.mtime, f.quality, f.rescued
        FROM sessions s
        JOIN session_frames sf ON sf.session_id = s.id
        JOIN frames f ON f.id = sf.frame_id
        WHERE f.image_type = 'LIGHT'
        """
    ):
        s = out.setdefault(r[0], _Session(r[0], r[1]))
        s.newest_mtime = max(s.newest_mtime, r[3] or 0.0)
        s.folders.add(str(Path(r[2]).parent))
        if r[4] != "failed":
            s.good += 1
        elif r[5] == 1:
            s.rescued += 1
        elif r[5] is None:
            s.untriaged += 1
    return out


def _project_sessions(conn: sqlite3.Connection) -> set[int]:
    used: set[int] = set()
    for (raw,) in conn.execute("SELECT source_session_ids FROM projects"):
        try:
            used.update(int(x) for x in json.loads(raw or "[]"))
        except (ValueError, TypeError):
            continue
    return used


def decide(conn: sqlite3.Connection, now: float | None = None) -> dict[str, int]:
    """Give a verdict to every session that can have one. Returns counts of new verdicts."""
    now = time.time() if now is None else now
    sessions = _sessions(conn)
    conn.execute(
        "DELETE FROM session_verdicts WHERE session_id NOT IN (SELECT id FROM sessions)"
    )
    # A scrap session that gained frames since (the scope came back to the
    # target) is judged again rather than purged with its new frames.
    for sid, decided_at in conn.execute(
        "SELECT session_id, decided_at FROM session_verdicts WHERE verdict = 'scrap'"
    ).fetchall():
        if sid in sessions and sessions[sid].newest_mtime > decided_at:
            conn.execute("DELETE FROM session_verdicts WHERE session_id = ?", (sid,))
    decided = {r[0] for r in conn.execute("SELECT session_id FROM session_verdicts")}
    in_projects = _project_sessions(conn)
    counts = {"keep": 0, "scrap": 0, "waiting": 0}
    for s in sessions.values():
        if s.id in decided:
            continue
        verdict: str | None
        if (
            s.exptime is None
            or s.exptime < MIN_EXPTIME_S
            or s.good * s.exptime >= SCRAP_BELOW_S
            or s.id in in_projects
        ):
            verdict = "keep"
        elif now - s.newest_mtime < SETTLE_S or s.untriaged:
            verdict = None
        else:
            verdict = "keep" if s.usable_s >= SCRAP_BELOW_S else "scrap"
        if verdict is None:
            counts["waiting"] += 1
            continue
        conn.execute(
            "INSERT INTO session_verdicts (session_id, verdict, usable_s, decided_at)"
            " VALUES (?, ?, ?, ?)",
            (s.id, verdict, s.usable_s, now),
        )
        counts[verdict] += 1
    conn.commit()
    return counts


def _verdicts(conn: sqlite3.Connection) -> dict[int, str]:
    return dict(conn.execute("SELECT session_id, verdict FROM session_verdicts").fetchall())


def kept_folders(conn: sqlite3.Connection) -> set[str]:
    """Capture folders (absolute paths) whose every session is kept."""
    verdicts = _verdicts(conn)
    by_folder: dict[str, set[str | None]] = defaultdict(set)
    for s in _sessions(conn).values():
        for folder in s.folders:
            by_folder[folder].add(verdicts.get(s.id))
    return {f for f, v in by_folder.items() if v == {"keep"}}


def _du(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


@dataclass
class ScrapFolder:
    folder: str
    """Absolute path under the capture root."""
    name: str
    sessions: list[int]
    usable_s: float
    good: int
    rescued: int
    failed: int
    bytes: int
    nas_path: str | None
    nas_bytes: int

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def scrap_plan(
    conn: sqlite3.Connection, captures: Path | None, nas: Path | None
) -> list[ScrapFolder]:
    """Folders that purging would delete: scope folders whose every session is scrap."""
    verdicts = _verdicts(conn)
    sessions = _sessions(conn)
    by_folder: dict[str, list[_Session]] = defaultdict(list)
    for s in sessions.values():
        for folder in s.folders:
            by_folder[folder].append(s)
    plan: list[ScrapFolder] = []
    for folder, members in sorted(by_folder.items()):
        path = Path(folder)
        if not path.name.startswith(FOLDER_PREFIX):
            continue
        if any(verdicts.get(s.id) != "scrap" for s in members):
            continue
        nas_path = None
        if nas is not None and captures is not None:
            try:
                nas_path = nas / "captures" / path.relative_to(captures)
            except ValueError:
                nas_path = None
        prefix = folder + "/"
        frames = conn.execute(
            "SELECT quality, rescued FROM frames"
            " WHERE image_type = 'LIGHT' AND substr(path, 1, ?) = ?",
            (len(prefix), prefix),
        ).fetchall()
        plan.append(
            ScrapFolder(
                folder=folder,
                name=path.name,
                sessions=sorted(s.id for s in members),
                usable_s=sum(s.usable_s for s in members),
                good=sum(1 for q, _ in frames if q != "failed"),
                rescued=sum(1 for q, r in frames if q == "failed" and r == 1),
                failed=sum(1 for q, r in frames if q == "failed" and r != 1),
                bytes=_du(path),
                nas_path=str(nas_path) if nas_path is not None and nas_path.exists() else None,
                nas_bytes=_du(nas_path) if nas_path is not None else 0,
            )
        )
    return plan


def purge(conn: sqlite3.Connection, item: ScrapFolder) -> None:
    """Delete one scrap folder everywhere and remember it, so the mirror leaves it be."""
    conn.execute(
        "INSERT OR REPLACE INTO purged_folders (name, bytes, usable_s, purged_at)"
        " VALUES (?, ?, ?, ?)",
        (item.name, item.bytes, item.usable_s, time.time()),
    )
    conn.commit()  # tombstone first: a crash mid-delete must not let the mirror recopy it
    shutil.rmtree(item.folder, ignore_errors=True)
    if item.nas_path:
        shutil.rmtree(item.nas_path, ignore_errors=True)
    prefix = item.folder + "/"
    conn.execute(
        "DELETE FROM session_frames WHERE frame_id IN"
        " (SELECT id FROM frames WHERE substr(path, 1, ?) = ?)",
        (len(prefix), prefix),
    )
    conn.execute("DELETE FROM frames WHERE substr(path, 1, ?) = ?", (len(prefix), prefix))
    conn.execute(
        "DELETE FROM sessions WHERE id NOT IN (SELECT DISTINCT session_id FROM session_frames)"
    )
    conn.execute(
        "DELETE FROM session_verdicts WHERE session_id NOT IN (SELECT id FROM sessions)"
    )
    conn.commit()
    log.warning(
        "purged scrap folder %s (%d MB, %.0f s usable, sessions %s)",
        item.name, item.bytes // 1_000_000, item.usable_s, item.sessions,
    )


def purged_names(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM purged_folders")}


class Curator:
    def __init__(
        self,
        db_path: Path,
        get_captures: Callable[[], Path | None],
        get_nas: Callable[[], Path | None],
        purge_enabled: Callable[[], bool],
    ) -> None:
        self._db_path = db_path
        self._captures = get_captures
        self._nas = get_nas
        self._purge_enabled = purge_enabled
        self._lock = threading.Lock()
        self.last: dict = {}

    @traced("curator.run")
    def run(self) -> dict:
        captures = self._captures()
        if captures is None or not captures.is_dir():
            return {}
        with self._lock:
            conn = connect_catalog(self._db_path)
            try:
                result: dict = {"at": time.time()}
                # Decide first (cheap) so sessions that need no triage reach the
                # library at once, then again for those triage just settled.
                first = decide(conn)
                result["triage"] = triage_pending(conn, budget_s=TRIAGE_BUDGET_S)
                second = decide(conn)
                result["verdicts"] = {
                    "keep": first["keep"] + second["keep"],
                    "scrap": first["scrap"] + second["scrap"],
                    "waiting": second["waiting"],
                }
                if self._purge_enabled():
                    plan = scrap_plan(conn, captures, self._nas())
                    for item in plan:
                        purge(conn, item)
                    result["purged"] = len(plan)
                log.info("curator: %s", result)
                self.last = result
                return result
            finally:
                conn.close()

    def plan(self) -> list[ScrapFolder]:
        conn = connect_catalog(self._db_path)
        try:
            return scrap_plan(conn, self._captures(), self._nas())
        finally:
            conn.close()

    def start(self) -> None:
        def loop() -> None:
            while True:
                try:
                    self.run()
                except Exception:
                    log.exception("curator pass failed")
                time.sleep(EVERY_S)

        threading.Thread(target=loop, name="astrolab-curator", daemon=True).start()
