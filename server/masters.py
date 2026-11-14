"""Target masters: one project per target that stacks every kept night.

A master is an ordinary project on the stack_per_night template, so the
project page, history, compare and finishing controls all work on it. This
module creates masters and adds nights to them:

- Sessions join a master once the curator has kept them and the target's
  night has settled (no new frame for an hour), so a master is rebuilt once
  per night rather than after every session.
- Sessions group by target and the fields a stack must share (instrument,
  camera, filter, gain, binning). Short subs (Moon, planets) get no master.
- A new master starts from the recipe for its object type
  (server/recipes.py). Adding a night only stacks that night; the other
  nights are cache hits and the combine is cheap.
- Masters are only created for targets shot in the last NEW_WITHIN_DAYS;
  older targets were already processed by hand.
- Each session is offered to a master once (target_masters.seen), so a night taken
  out by hand stays out, and a deleted master isn't rebuilt.

This replaces the old per-session auto-stack, behind the same setting.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from server.catalog.db import connect as connect_catalog
from server.catalog.openngc import enrich
from server.curator import MIN_EXPTIME_S, SETTLE_S, _night, _sessions
from server.job_builder import JobBuildError, build_from_sessions, canon_instrument
from server.recipes import recipe_for
from server.telemetry import traced

log = logging.getLogger(__name__)

TEMPLATE_ID = "stack_per_night"
NEW_WITHIN_DAYS = 45
EVERY_S = 600


@dataclass
class Group:
    key: str
    target_id: int
    target: str
    filter: str | None
    ready: list[int] = field(default_factory=list)
    """Kept sessions whose night has settled."""
    waiting: list[int] = field(default_factory=list)
    """Kept sessions whose night is still going."""
    usable_s: dict[int, float] = field(default_factory=dict)
    nights: dict[int, str] = field(default_factory=dict)
    newest_night: str = ""


@dataclass
class Action:
    key: str
    target: str
    filter: str | None
    action: str
    """create, add, up_to_date, waiting, too_old, retired, or error."""
    project_id: str | None = None
    add: list[int] = field(default_factory=list)
    add_s: float = 0.0
    total_s: float = 0.0
    label: str = ""
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _key(r: sqlite3.Row) -> str:
    inst = canon_instrument(r["instrument"]) if r["instrument"] else ""
    return "|".join(
        str(x) for x in (r["target_id"], inst, r["camera"], r["filter"], r["gain"], r["binning"])
    )


def groups(conn: sqlite3.Connection, now: float | None = None) -> dict[str, Group]:
    now = time.time() if now is None else now
    frames = _sessions(conn)
    # A night is settled when no session of its target got a frame for SETTLE_S.
    newest: dict[tuple[int | None, str], float] = defaultdict(float)
    for s in frames.values():
        newest[s.night] = max(newest[s.night], s.newest_mtime)
    out: dict[str, Group] = {}
    for r in conn.execute(
        """
        SELECT s.id, s.target_id, t.name AS target, s.instrument, s.camera, s.filter,
               s.gain, s.binning, s.exptime, s.started_at, v.usable_s
        FROM sessions s
        JOIN targets t ON t.id = s.target_id
        JOIN session_verdicts v ON v.session_id = s.id AND v.verdict = 'keep'
        ORDER BY s.started_at
        """
    ):
        if (r["exptime"] or 0) < MIN_EXPTIME_S or r["id"] not in frames:
            continue
        night = _night(r["started_at"])
        g = out.setdefault(_key(r), Group(_key(r), r["target_id"], r["target"], r["filter"]))
        g.usable_s[r["id"]] = r["usable_s"]
        g.nights[r["id"]] = night
        g.newest_night = max(g.newest_night, night)
        if night and now - newest[(r["target_id"], night)] < SETTLE_S:
            g.waiting.append(r["id"])
        else:
            g.ready.append(r["id"])
    return out


def _label(g: Group, add: list[int], first: bool) -> str:
    hours = sum(g.usable_s.get(s, 0.0) for s in add) / 3600
    nights = sorted({g.nights[s] for s in add if g.nights.get(s)})
    days = ", ".join(datetime.fromisoformat(n).strftime("%b %-d") for n in nights[-3:])
    if len(nights) > 3:
        days = f"{len(nights)} nights to {days.split(', ')[-1]}"
    if first:
        return f"master: {hours:.1f} h from {days}" if days else f"master: {hours:.1f} h"
    return f"+{hours:.1f} h from {days}" if days else f"+{hours:.1f} h"


def plan(
    conn: sqlite3.Connection,
    get_project: Callable[[str], Any | None],
    now: float | None = None,
) -> list[Action]:
    now = time.time() if now is None else now
    rows = {r["key"]: r for r in conn.execute("SELECT * FROM target_masters")}
    cutoff = (datetime.fromtimestamp(now) - timedelta(days=NEW_WITHIN_DAYS)).date().isoformat()
    out: list[Action] = []
    for g in groups(conn, now).values():
        a = Action(g.key, g.target, g.filter, "up_to_date")
        row = rows.get(g.key)
        if row is None:
            a.add = list(g.ready)
            if not g.ready:
                a.action = "waiting"
            elif g.newest_night < cutoff:
                a.action = "too_old"
            else:
                a.action = "create"
                a.label = _label(g, a.add, first=True)
            a.add_s = a.total_s = sum(g.usable_s[s] for s in a.add)
            out.append(a)
            continue
        a.project_id = row["project_id"]
        project = get_project(row["project_id"])
        if project is None:
            a.action = "retired"
            out.append(a)
            continue
        seen = set(json.loads(row["seen"] or "[]"))
        current = {int(s) for s in project.source_session_ids}
        a.add = [s for s in g.ready if s not in seen and s not in current]
        a.add_s = sum(g.usable_s[s] for s in a.add)
        a.total_s = sum(g.usable_s.get(s, 0.0) for s in current) + a.add_s
        if a.add:
            a.action = "add"
            a.label = _label(g, a.add, first=False)
        elif g.waiting and not set(g.waiting) <= current:
            a.action = "waiting"
        out.append(a)
    out.sort(key=lambda a: (a.target, a.filter or ""))
    return out


def apply(conn: sqlite3.Connection, projects: Any, action: Action, now: float | None = None) -> str:
    """Create or extend one master. Returns the project id."""
    from server.templates import load_template

    now = time.time() if now is None else now
    snapshot = {"added": action.add, "added_s": action.add_s, "total_s": action.total_s}
    if action.action == "create":
        template = load_template(TEMPLATE_ID)
        target_id = int(action.key.split("|", 1)[0])
        name = conn.execute("SELECT name FROM targets WHERE id = ?", (target_id,)).fetchone()[0]
        entry = enrich(name)
        recipe = recipe_for(entry.object_type if entry else None, action.filter)
        job = build_from_sessions(conn, action.add, template)
        job = job.model_copy(
            update={"param_overrides": {**job.param_overrides, **recipe.overrides}}
        )
        siblings = conn.execute(
            "SELECT COUNT(*) FROM target_masters WHERE target_id = ?", (target_id,)
        ).fetchone()[0]
        title = name if not siblings else f"{name} ({action.filter})"
        project = projects.create(
            name=title,
            template=template,
            base_job=job,
            source_session_ids=[str(s) for s in sorted(action.add)],
            label=f"{action.label}, recipe: {recipe.label.lower()}",
            snapshot={**snapshot, "recipe": recipe.id},
        )
        with conn:
            conn.execute(
                "INSERT INTO target_masters (key, target_id, project_id, recipe, seen, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (action.key, target_id, project.id, recipe.id, json.dumps(action.add), now),
            )
        return project.id
    if action.action == "add":
        project = projects.get(action.project_id)
        ids = sorted({int(s) for s in project.source_session_ids} | set(action.add))
        job = build_from_sessions(conn, ids, project.template)
        projects.swap_sessions(
            action.project_id,
            new_base_job=job,
            new_session_ids=[str(s) for s in ids],
            snapshot=snapshot,
            label=action.label,
        )
        row = conn.execute(
            "SELECT seen FROM target_masters WHERE key = ?", (action.key,)
        ).fetchone()
        seen = sorted(set(json.loads(row[0] or "[]")) | set(action.add))
        with conn:
            conn.execute(
                "UPDATE target_masters SET seen = ? WHERE key = ?", (json.dumps(seen), action.key)
            )
        return action.project_id
    raise ValueError(f"nothing to apply for {action.action}")


def master_projects(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """project_id -> {key, target_id, recipe} for every master."""
    return {
        r["project_id"]: {"key": r["key"], "target_id": r["target_id"], "recipe": r["recipe"]}
        for r in conn.execute("SELECT * FROM target_masters")
    }


class MasterKeeper:
    def __init__(self, db_path: Path, projects: Any, enabled: Callable[[], bool]) -> None:
        self._db_path = db_path
        self._projects = projects
        self._enabled = enabled
        self._lock = threading.Lock()
        self.last: dict[str, Any] | None = None

    def plan(self) -> list[Action]:
        conn = connect_catalog(self._db_path)
        try:
            return plan(conn, self._projects.get)
        finally:
            conn.close()

    @traced("masters.run")
    def run(self) -> dict[str, Any]:
        with self._lock:
            conn = connect_catalog(self._db_path)
            done: list[dict[str, Any]] = []
            try:
                for a in plan(conn, self._projects.get):
                    if a.action not in ("create", "add"):
                        continue
                    try:
                        a.project_id = apply(conn, self._projects, a)
                        log.info("master %s: %s (%s)", a.target, a.action, a.label)
                    except JobBuildError as exc:
                        a.action, a.error = "error", str(exc)
                        log.warning("master %s: %s", a.target, exc)
                    done.append(a.as_dict())
            finally:
                conn.close()
            self.last = {"at": time.time(), "actions": done}
            return self.last

    def start(self) -> None:
        def loop() -> None:
            while True:
                time.sleep(EVERY_S)
                if not self._enabled():
                    continue
                try:
                    self.run()
                except Exception:
                    log.exception("master keeper pass failed")

        threading.Thread(target=loop, name="astrolab-masters", daemon=True).start()
