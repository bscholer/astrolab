"""Target masters: one project per target that stacks every kept night.

A master is an ordinary project on the stack_per_night template, so the
project page, history, compare and finishing controls all work on it. This
module creates masters and adds nights to them:

- Sessions join a master once the curator has kept them and the target's
  night has settled (no new frame for an hour), so a master is rebuilt once
  per night rather than after every session.
- Sessions group by the patch of sky they cover and the fields a stack must
  share (instrument, camera, filter, gain, binning). Sessions whose frames
  overlap join one master whatever target name they were shot under, so a
  mosaic panel stacks with the target it covers, and a panel between two
  targets joins them (server/coverage.py). Sessions without a position fall
  back to their target. When a new session joins two masters' areas, the
  bigger master takes in the other's nights; the other stays as a plain
  project. Short subs (Moon, planets) get no master.
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

from server import coverage
from server.catalog.db import connect as connect_catalog
from server.catalog.openngc import enrich
from server.curator import MIN_EXPTIME_S, SETTLE_S, _night, _sessions
from server.job_builder import JobBuildError, build_from_sessions, canon_instrument
from server.recipes import is_duo, recipe_for
from server.telemetry import traced

log = logging.getLogger(__name__)

TEMPLATE_ID = "stack_per_night"
DUO_TEMPLATE_ID = "stack_per_night_duo"
"""Duo-band (H-alpha + OIII) masters split the channels before debayering;
see templates/stack_per_night_duo.yaml for why."""
TEMPLATE_IDS = (TEMPLATE_ID, DUO_TEMPLATE_ID)
DUO_RESET_NODES = ("channel_map", "color_balance", "crop")
"""Moving onto the duo template drops these edits: HOO comes on and SCNR
green removal goes off, as that template sets them, and a crop box drawn on
the old combined frame would point somewhere else on the new one."""
NEW_WITHIN_DAYS = 45
MIN_SESSION_S = 600.0
"""Sessions with less usable exposure stay out of masters: a few frames rarely
register on their own, a failed night fails the whole master job, and they
add almost nothing."""
EVERY_S = 600


@dataclass
class Group:
    key: str
    """Key a new master for this group gets: sky|<stack fields>|<first session>."""
    target_id: int
    """The target with the most usable time: it names the master and picks the recipe."""
    target: str
    filter: str | None
    ready: list[int] = field(default_factory=list)
    """Kept sessions whose night has settled."""
    waiting: list[int] = field(default_factory=list)
    """Kept sessions whose night is still going."""
    usable_s: dict[int, float] = field(default_factory=dict)
    nights: dict[int, str] = field(default_factory=dict)
    newest_night: str = ""

    @property
    def sessions(self) -> set[int]:
        return set(self.ready) | set(self.waiting)


@dataclass
class Action:
    key: str
    target: str
    filter: str | None
    action: str
    """create, add, merge, up_to_date, waiting, too_old, retired, or error."""
    project_id: str | None = None
    target_id: int | None = None
    add: list[int] = field(default_factory=list)
    add_s: float = 0.0
    total_s: float = 0.0
    label: str = ""
    error: str | None = None
    merge: list[str] = field(default_factory=list)
    """Keys of masters whose nights this one takes in."""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def template_for(filter_name: str | None) -> str:
    return DUO_TEMPLATE_ID if is_duo(filter_name) else TEMPLATE_ID


def _fields(r: sqlite3.Row) -> str:
    """What a stack must share: instrument, camera, filter, gain, binning."""
    inst = canon_instrument(r["instrument"]) if r["instrument"] else ""
    return "|".join(str(x) for x in (inst, r["camera"], r["filter"], r["gain"], r["binning"]))


def _key(r: sqlite3.Row) -> str:
    """The key of a master grouped by target, as before sky grouping."""
    return f"{r['target_id']}|{_fields(r)}"


class _Union:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def join(self, a: int, b: int) -> None:
        self.parent[self.find(a)] = self.find(b)


def _region_name(usable_by_target: dict[str, float]) -> str:
    """The two biggest targets of a region by usable time, mosaic panels by their mosaic."""
    totals: dict[str, float] = defaultdict(float)
    for name, s in usable_by_target.items():
        totals[coverage.base_name(name)] += s
    names = sorted(totals, key=lambda n: (-totals[n], n))
    return " + ".join(names[:2])


def groups(conn: sqlite3.Connection, now: float | None = None) -> dict[str, Group]:
    now = time.time() if now is None else now
    frames = _sessions(conn)
    # A night is settled when no session of its target got a frame for SETTLE_S.
    newest: dict[tuple[int | None, str], float] = defaultdict(float)
    for s in frames.values():
        newest[s.night] = max(newest[s.night], s.newest_mtime)
    rows: list[sqlite3.Row] = []
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
        if (r["usable_s"] or 0) < MIN_SESSION_S:
            continue
        rows.append(r)

    # Join sessions of one target, and sessions whose frames overlap on the sky,
    # among sessions that can share a stack.
    feet = coverage.footprints(conn)
    union = _Union()
    by_fields: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        union.find(r["id"])
        by_fields[_fields(r)].append(r)
    for members in by_fields.values():
        first_of_target: dict[int, int] = {}
        for r in members:
            first = first_of_target.setdefault(r["target_id"], r["id"])
            union.join(r["id"], first)
        placed = [(r["id"], feet[r["id"]]) for r in members if r["id"] in feet]
        for i, (a, fa) in enumerate(placed):
            for b, fb in placed[i + 1 :]:
                if coverage.overlaps(fa, fb):
                    union.join(a, b)

    members_of: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        members_of[union.find(r["id"])].append(r)
    out: dict[str, Group] = {}
    for members in members_of.values():
        by_target: dict[int, float] = defaultdict(float)
        names: dict[int, str] = {}
        for r in members:
            by_target[r["target_id"]] += r["usable_s"] or 0
            names[r["target_id"]] = r["target"]
        top = max(by_target, key=lambda t: (by_target[t], -t))
        first = min(members, key=lambda r: r["id"])
        key = f"sky|{_fields(first)}|{first['id']}"
        name = _region_name({names[t]: v for t, v in by_target.items()})
        g = Group(key, top, name, first["filter"])
        for r in members:
            night = _night(r["started_at"])
            g.usable_s[r["id"]] = r["usable_s"]
            g.nights[r["id"]] = night
            g.newest_night = max(g.newest_night, night)
            if night and now - newest[(r["target_id"], night)] < SETTLE_S:
                g.waiting.append(r["id"])
            else:
                g.ready.append(r["id"])
        out[key] = g
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


@dataclass
class _Master:
    key: str
    project_id: str
    target_id: int
    created_at: float
    seen: set[int]
    current: set[int] | None
    """The project's sessions, None when the project was deleted."""
    name: str = ""

    @property
    def sessions(self) -> set[int]:
        return self.seen | (self.current or set())


def _masters(conn: sqlite3.Connection, get_project: Callable[[str], Any | None]) -> list[_Master]:
    out = []
    for row in conn.execute("SELECT * FROM target_masters ORDER BY created_at"):
        project = get_project(row["project_id"])
        out.append(
            _Master(
                row["key"],
                row["project_id"],
                row["target_id"],
                row["created_at"] or 0.0,
                {int(s) for s in json.loads(row["seen"] or "[]")},
                {int(s) for s in project.source_session_ids} if project is not None else None,
                project.name if project is not None else "",
            )
        )
    return out


def plan(
    conn: sqlite3.Connection,
    get_project: Callable[[str], Any | None],
    now: float | None = None,
) -> list[Action]:
    now = time.time() if now is None else now
    cutoff = (datetime.fromtimestamp(now) - timedelta(days=NEW_WITHIN_DAYS)).date().isoformat()
    known = _masters(conn, get_project)
    out: list[Action] = []
    for g in groups(conn, now).values():
        a = Action(g.key, g.target, g.filter, "up_to_date", target_id=g.target_id)
        touching = [m for m in known if m.sessions & g.sessions]
        live = [m for m in touching if m.current is not None]
        # Nights a deleted master had stay out, as the deletion asked.
        blocked = set().union(*(m.seen for m in touching if m.current is None))
        if not live:
            if touching:
                a.action, a.project_id = "retired", touching[0].project_id
                out.append(a)
                continue
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

        usable = g.usable_s
        keep = max(
            live,
            key=lambda m: (sum(usable.get(s, 0.0) for s in m.current or ()), -m.created_at),
        )
        others = [m for m in live if m is not keep]
        current = keep.current or set()
        taken_in = set().union(*(m.current or set() for m in others)) - current
        # Nights taken out of any of these masters by hand stay out.
        out_by_hand = keep.seen.union(*(m.seen for m in others)) | blocked
        new = [s for s in g.ready if s not in out_by_hand and s not in current]
        a.add = sorted(set(new) | taken_in)
        a.key, a.project_id, a.target_id = keep.key, keep.project_id, keep.target_id
        a.merge = [m.key for m in others]
        a.add_s = sum(g.usable_s.get(s, 0.0) for s in a.add)
        a.total_s = sum(g.usable_s.get(s, 0.0) for s in current) + a.add_s
        if others:
            a.action = "merge"
            joined = ", ".join(m.name or m.key for m in others)
            a.label = f"joined {joined}" + (f", {_label(g, a.add, first=False)}" if a.add else "")
        elif a.add:
            a.action = "add"
            a.label = _label(g, a.add, first=False)
        elif g.waiting and not set(g.waiting) <= current:
            a.action = "waiting"
        out.append(a)
    out.sort(key=lambda a: (a.target, a.filter or ""))
    return out


def apply(conn: sqlite3.Connection, projects: Any, action: Action, now: float | None = None) -> str:
    """Create, extend or merge one master. Returns the project id."""
    from server.templates import load_template

    now = time.time() if now is None else now
    snapshot = {"added": action.add, "added_s": action.add_s, "total_s": action.total_s}
    if action.action == "create":
        template = load_template(template_for(action.filter))
        target_id = action.target_id
        if target_id is None:
            target_id = int(action.key.split("|", 1)[0])
        name = conn.execute("SELECT name FROM targets WHERE id = ?", (target_id,)).fetchone()[0]
        entry = enrich(coverage.base_name(name))
        recipe = recipe_for(entry.object_type if entry else None, action.filter)
        job = build_from_sessions(conn, action.add, template)
        job = job.model_copy(
            update={"param_overrides": {**job.param_overrides, **recipe.overrides}}
        )
        siblings = conn.execute(
            "SELECT COUNT(*) FROM target_masters WHERE target_id = ?", (target_id,)
        ).fetchone()[0]
        region = action.target or name
        title = region if not siblings else f"{region} ({action.filter})"
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
    if action.action in ("add", "merge"):
        project = projects.get(action.project_id)
        ids = sorted({int(s) for s in project.source_session_ids} | set(action.add))
        job = build_from_sessions(conn, ids, project.template)
        projects.swap_sessions(
            action.project_id,
            new_base_job=job,
            new_session_ids=[str(s) for s in ids],
            snapshot={**snapshot, "merged": action.merge} if action.merge else snapshot,
            label=action.label,
        )
        seen = set(action.add)
        for key in [action.key, *action.merge]:
            row = conn.execute("SELECT seen FROM target_masters WHERE key = ?", (key,)).fetchone()
            if row:
                seen |= {int(s) for s in json.loads(row[0] or "[]")}
        with conn:
            conn.execute(
                "UPDATE target_masters SET seen = ? WHERE key = ?",
                (json.dumps(sorted(seen)), action.key),
            )
            # The merged masters stay as ordinary projects; they are no longer kept.
            for key in action.merge:
                conn.execute("DELETE FROM target_masters WHERE key = ?", (key,))
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
                for pid in self.upgrade():
                    done.append({"action": "upgrade", "project_id": pid})
                for a in plan(conn, self._projects.get):
                    if a.action not in ("create", "add", "merge"):
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

    def upgrade(self) -> list[str]:
        """Keep masters on the latest template of the right family; they are the
        server's to keep. A duo-band master on the plain template moves to the
        duo one (history and finishing edits kept)."""
        from server.templates import load_template

        latest = {tid: load_template(tid) for tid in TEMPLATE_IDS}
        conn = connect_catalog(self._db_path)
        moved = []
        try:
            for pid, row in master_projects(conn).items():
                project = self._projects.get(pid)
                if project is None or project.template.id not in TEMPLATE_IDS:
                    continue
                parts = row["key"].split("|")
                filt = parts[3] if len(parts) > 3 and parts[3] != "None" else None
                want = latest[template_for(filt)]
                if project.template.id != want.id:
                    ids = [int(s) for s in project.source_session_ids]
                    try:
                        job = build_from_sessions(conn, ids, want)
                    except JobBuildError as exc:
                        log.warning("master %s: cannot move to %s: %s", project.name, want.id, exc)
                        continue
                    self._projects.switch_template(
                        pid,
                        new_template=want,
                        new_base_job=job,
                        label="H-alpha and OIII split before debayering (real HOO colour)",
                        reset_nodes=DUO_RESET_NODES if want.id == DUO_TEMPLATE_ID else (),
                    )
                    moved.append(pid)
                    log.info("master %s moved to %s", project.name, want.id)
                elif project.template.version < want.version:
                    self._projects.upgrade_template(pid, new_template=want)
                    moved.append(pid)
                    log.info("master %s moved to %s v%d", project.name, want.id, want.version)
        finally:
            conn.close()
        return moved

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
