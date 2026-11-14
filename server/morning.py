"""What happened last night: the home page's first card.

For the most recent night with captures: each target shot, how much of it
was kept, and what its master gained (the history entry that added the
night, its render and the render before it, for a before/after compare).
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from server.catalog.common_names import lookup as lookup_common_name
from server.catalog.openngc import enrich
from server.curator import _sessions


def _common_name(name: str | None) -> str | None:
    entry = enrich(name)
    return (entry.common_name if entry else None) or lookup_common_name(name)


def _preview(record: Any) -> dict[str, str] | None:
    if record is None or not record.outputs:
        return None
    port = "image" if "image" in record.outputs else next(iter(record.outputs))
    return {"hash": record.outputs[port].node_hash, "port": port}


def _master_update(
    project: Any, session_ids: set[int], get_job: Callable[[str], Any | None]
) -> dict[str, Any] | None:
    """The newest history entry that added one of `session_ids`, with renders."""
    history = sorted(project.history, key=lambda h: h.seq)
    for i in range(len(history) - 1, -1, -1):
        entry = history[i]
        added = {int(s) for s in (entry.snapshot or {}).get("added", [])}
        if not added & session_ids:
            continue
        record = get_job(entry.job_id)
        before = None
        for prev in reversed(history[:i]):
            before = _preview(get_job(prev.job_id))
            if before:
                break
        snap = entry.snapshot or {}
        total, gained = snap.get("total_s", 0.0), snap.get("added_s", 0.0)
        noise = None
        if before and total > gained > 0:
            # Noise falls with the square root of integration time.
            noise = 1 - math.sqrt((total - gained) / total)
        return {
            "project_id": project.id,
            "name": project.name,
            "seq": entry.seq,
            "label": entry.label,
            "status": record.status if record else "unknown",
            "after": _preview(record),
            "before": before,
            "before_seq": history[i - 1].seq if i > 0 else None,
            "recipe": snap.get("recipe"),
            "added_s": gained,
            "total_s": total,
            "noise_drop": noise,
        }
    return None


def morning(
    conn: sqlite3.Connection,
    get_project: Callable[[str], Any | None],
    get_job: Callable[[str], Any | None],
    night: str | None = None,
) -> dict[str, Any]:
    frames = _sessions(conn)
    nights = sorted({s.night[1] for s in frames.values() if s.night[1]})
    if not nights:
        return {"night": None, "nights": [], "targets": []}
    night = night if night in nights else nights[-1]
    verdicts = {
        r[0]: (r[1], r[2])
        for r in conn.execute("SELECT session_id, verdict, usable_s FROM session_verdicts")
    }
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM targets")}
    masters = defaultdict(list)
    for r in conn.execute("SELECT target_id, project_id FROM target_masters"):
        masters[r[0]].append(r[1])
    started = {
        r[0]: r[1] for r in conn.execute("SELECT id, started_at FROM sessions")
    }

    by_target: dict[int | None, list] = defaultdict(list)
    for s in frames.values():
        if s.night[1] == night:
            by_target[s.night[0]].append(s)
    targets = []
    for tid, sessions in by_target.items():
        kept = scrap = waiting = 0.0
        frames_n = sum(s.good + s.rescued + s.untriaged for s in sessions)
        for s in sessions:
            verdict, usable = verdicts.get(s.id, (None, s.usable_s))
            if verdict == "keep":
                kept += usable
            elif verdict == "scrap":
                scrap += usable
            else:
                waiting += s.usable_s
        ids = {s.id for s in sessions}
        updates = []
        for pid in masters.get(tid, []):
            project = get_project(pid)
            if project is None:
                continue
            u = _master_update(project, ids, get_job)
            if u:
                updates.append(u)
        if updates:
            state = "updated"
        elif waiting:
            state = "judging"
        elif kept and masters.get(tid):
            state = "queued"
        elif kept:
            state = "kept"
        else:
            state = "scrap"
        targets.append({
            "target_id": tid,
            "name": names.get(tid, "Unknown target"),
            "common_name": _common_name(names.get(tid)),
            "sessions": sorted(ids),
            "started_at": min((started.get(i) or "" for i in ids), default=""),
            "frames": frames_n,
            "kept_s": kept,
            "scrap_s": scrap,
            "judging_s": waiting,
            "state": state,
            "masters": updates,
        })
    targets.sort(key=lambda t: t["started_at"])
    return {"night": night, "nights": nights[-14:], "targets": targets}
