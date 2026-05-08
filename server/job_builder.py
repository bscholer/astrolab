"""Build a runtime Job from a catalog session + a canned template.

The Library UI lets users click 'Run' on a session. To turn that into a Job
the runtime can execute, we need to:

1. Look up the session in the catalog and figure out the on-disk folder that
   holds its raw FITS subs (we use the parent dir of any session frame).
2. Find a usable master dark for the session via calibration_matches; bail
   out clearly when none is available rather than silently shipping a stack
   with no calibration.
3. Wire the session folder + master path into the template's external inputs
   (convert.lights and calibrate.dark, by convention).

The wiring is convention-based for now: any unwired SEQUENCE_FITS port named
'lights' gets the session folder, any unwired MASTER_FITS port named 'dark'
gets the matched master. Generalize when a second template needs different
shape (e.g. multi-session stacks, mosaics).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .models import CalibrationSpec, Job, Ref, Template
from .ports import PortType


class JobBuildError(RuntimeError):
    pass


class SessionNotFound(JobBuildError):
    pass


class CalibrationMissing(JobBuildError):
    """Raised when the template needs a master and none is available."""


def session_lights_folder(conn: sqlite3.Connection, session_id: int) -> Path:
    """Return the on-disk directory holding the session's raw subs.

    Picks any session frame's parent dir. Sessions are expected to be a flat
    folder of .fits files (Dwarf 3 layout); for mosaic sessions each panel is
    its own session row, also a flat folder. If frames span more than one
    parent dir we raise so the caller doesn't silently get a partial stack.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT path FROM frames
        WHERE id IN (
          SELECT frame_id FROM session_frames WHERE session_id = ?
        ) AND image_type = 'LIGHT'
        """,
        (session_id,),
    ).fetchall()
    if not rows:
        raise SessionNotFound(f"session {session_id} has no light frames")
    parents = {Path(r["path"]).parent for r in rows}
    if len(parents) != 1:
        raise JobBuildError(
            f"session {session_id} spans {len(parents)} folders; "
            "manual staging required"
        )
    return parents.pop()


def matched_master_path(
    conn: sqlite3.Connection, session_id: int, kind: str
) -> Path | None:
    """Return the path of the matched master for `kind`, or None if no match."""
    row = conn.execute(
        """
        SELECT m.path
        FROM calibration_matches cm
        JOIN masters m ON m.id = cm.master_id
        WHERE cm.session_id = ? AND cm.kind = ? AND cm.master_id IS NOT NULL
        """,
        (session_id, kind),
    ).fetchone()
    return Path(row["path"]) if row is not None else None


def build_from_session(
    conn: sqlite3.Connection,
    session_id: int,
    template: Template,
    calibration: CalibrationSpec | None = None,
) -> Job:
    """Materialize the external Refs the template needs for `session_id`.

    `calibration` is honored when mode='explicit' (master_ids[<kind>] -> id)
    or 'none' (skip calibration entirely; convert+register+stack still run).
    Default 'auto' uses the catalog's calibration_matches.
    """
    cal = calibration if calibration is not None else CalibrationSpec()

    lights_dir = session_lights_folder(conn, session_id)
    inputs: dict[str, Ref] = {}

    for node in template.nodes:
        from .registry import lookup as registry_lookup

        node_cls = registry_lookup(node.kind, node.variant)
        for port_name, port_type in node_cls.inputs.items():
            if port_name in node.inputs:
                continue  # internal edge; not external
            external_key = f"{node.id}.{port_name}"

            # Convention 1: a SEQUENCE_FITS port named 'lights' is the session.
            if port_type is PortType.SEQUENCE_FITS and port_name == "lights":
                inputs[external_key] = Ref(
                    node_hash="ext",
                    port=port_name,
                    path=lights_dir,
                    type=PortType.SEQUENCE_FITS,
                )
                continue

            # Convention 2: a MASTER_FITS port named after a calibration kind
            # (dark, flat, bias) gets the catalog's match (or an explicit one).
            if port_type is PortType.MASTER_FITS and port_name in (
                "dark",
                "flat",
                "bias",
            ):
                if cal.mode == "none":
                    # Skip optional masters; for required ones, fail loud.
                    if port_name in node_cls.optional_inputs:
                        continue
                    raise CalibrationMissing(
                        f"template requires master {port_name} but "
                        f"calibration mode is 'none'"
                    )
                if cal.mode == "explicit" and port_name in cal.master_ids:
                    master_id = cal.master_ids[port_name]
                    row = conn.execute(
                        "SELECT path FROM masters WHERE id = ?", (master_id,)
                    ).fetchone()
                    if row is None:
                        raise CalibrationMissing(
                            f"explicit master {port_name}={master_id} not found"
                        )
                    path = Path(row["path"])
                else:
                    matched = matched_master_path(conn, session_id, port_name)
                    if matched is None:
                        if port_name in node_cls.optional_inputs:
                            continue
                        raise CalibrationMissing(
                            f"no matched master {port_name} for session "
                            f"{session_id}; explicit override or scoped scan needed"
                        )
                    path = matched
                inputs[external_key] = Ref(
                    node_hash="ext",
                    port=port_name,
                    path=path,
                    type=PortType.MASTER_FITS,
                )
                continue

            # Unrecognized external port; let the runtime surface a clear
            # error rather than guessing.

    return Job(
        template_id=template.id,
        template_version=template.version,
        target_id=None,
        session_ids=[str(session_id)],
        calibration=cal,
        inputs=inputs,
    )
