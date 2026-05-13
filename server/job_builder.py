"""Build a runtime Job from one-or-more catalog sessions + a canned template.

The Library UI lets users click 'Run' on a session (or check several
compatible sessions and run them as one project). To turn that into a Job
the runtime can execute, we need to:

1. Look up the session(s) in the catalog and figure out the on-disk folder
   that holds the raw FITS subs (we use the parent dir of any session
   frame).
2. For multi-session, materialize a deterministic staging directory of
   symlinks so `convert_lights` sees one flat folder of all frames; the
   path is keyed off the sorted session id list so re-runs hit the same
   cache lineage.
3. Find a usable master dark for the session via calibration_matches; bail
   out clearly when none is available rather than silently shipping a stack
   with no calibration. For multi-session we use the first session's match —
   compat checks ensure all sessions share gain/exptime/instrument, so the
   match would be identical for each in practice.
4. Wire the session folder + master path into the template's external inputs
   (convert.lights and calibrate.dark, by convention).

The wiring is convention-based for now: any unwired SEQUENCE_FITS port named
'lights' gets the session folder, any unwired MASTER_FITS port named 'dark'
gets the matched master. Generalize when a second template needs different
shape (e.g. mosaics).
"""

from __future__ import annotations

import contextlib
import hashlib
import sqlite3
from pathlib import Path

from .catalog.matching import match_bundle_darks
from .models import CalibrationSpec, Job, Ref, Template
from .paths import astrolab_home
from .ports import PortType


class JobBuildError(RuntimeError):
    pass


class SessionNotFound(JobBuildError):
    pass


class CalibrationMissing(JobBuildError):
    """Raised when the template needs a master and none is available."""


class TooFewFrames(JobBuildError):
    """Raised when the session doesn't have enough frames to stack.

    Siril's `calibrate <basename>` rejects single-frame inputs as 'No
    sequence found', and stacking <3 frames is rarely useful anyway. We
    surface this pre-flight rather than letting the pipeline blow up
    halfway through."""


class IncompatibleSessions(JobBuildError):
    """Raised when a multi-session request bundles sessions that don't share
    the metadata the stacker treats as identical (target, gain, exptime,
    filter, instrument, camera, binning).

    We keep this strict for now: dedicated DAGs for differing exposures /
    filters are out of scope. The error names every conflicting field with
    the offending values so the UI can render a useful diagnostic.
    """


MIN_FRAMES_FOR_STACK = 3

# Fields a multi-session bundle must agree on before we will stack them.
# Order matters only for stable error formatting.
#
# `exptime` is intentionally NOT in this list: the calibrate node now
# accepts a list of master darks and selects the right one per-frame from
# the bundle's per-bin matches, so a bundle that mixes 30s and 60s subs is
# fine as long as a dark exists for each. Keep this list in sync with the
# inline filter in api._suggestions_for_project (the "add frames to an
# existing project" banner uses the same criteria).
COMPAT_FIELDS: tuple[str, ...] = (
    "target_id",
    "instrument",
    "camera",
    "filter",
    "gain",
    "binning",
)


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

    Thin wrapper around `build_from_sessions([session_id], ...)` so the
    single-session call site stays clean.
    """
    return build_from_sessions(conn, [session_id], template, calibration)


def build_from_sessions(
    conn: sqlite3.Connection,
    session_ids: list[int],
    template: Template,
    calibration: CalibrationSpec | None = None,
) -> Job:
    """Build a Job whose external 'lights' port spans every session in
    `session_ids`.

    For N=1 this is identical to build_from_session: the lights Ref points
    at the actual session folder. For N>1 we materialize a deterministic
    symlink farm under `<astrolab_home>/lights_stage/<hash>/` so
    convert_lights sees one flat directory of frames; the staging path is
    keyed off the sorted session id tuple so the same bundle hits the same
    cache lineage on re-run.

    The matching session calibration is taken from `session_ids[0]` —
    compat checks above guarantee all sessions in the bundle share the
    fields the matcher uses (instrument/exptime/gain/binning), so any of
    them would resolve to the same master.
    """
    if not session_ids:
        raise JobBuildError("at least one session is required")
    # Treat the input as a set: dedupe and sort so behavior is deterministic
    # regardless of the order the API or test fixtures hand us. Cache lineage
    # (the staging path) and 'first session' calibration both rely on this
    # canonical order; without it [1,2] and [2,1] would race for the same dir.
    session_ids = sorted(set(session_ids))
    cal = calibration if calibration is not None else CalibrationSpec()

    # Compatibility gate (and frame-count gate): only meaningful for N>=2,
    # but we always run the row lookup so a bogus id surfaces as
    # SessionNotFound here instead of much later in the pipeline.
    sessions = _load_sessions(conn, session_ids)

    if len(sessions) > 1:
        _assert_compatible(sessions)

    # Reject bundles that don't have enough total frames to stack.
    total_lights = 0
    for sid in session_ids:
        total_lights += conn.execute(
            """
            SELECT COUNT(*) FROM frames
            WHERE id IN (SELECT frame_id FROM session_frames WHERE session_id = ?)
              AND image_type = 'LIGHT'
            """,
            (sid,),
        ).fetchone()[0]
    if total_lights < MIN_FRAMES_FOR_STACK:
        if len(session_ids) == 1:
            raise TooFewFrames(
                f"session {session_ids[0]} has only {total_lights} light frame(s); "
                f"the pipeline needs at least {MIN_FRAMES_FOR_STACK}"
            )
        raise TooFewFrames(
            f"sessions {session_ids} have only {total_lights} light frame(s) "
            f"between them; the pipeline needs at least {MIN_FRAMES_FOR_STACK}"
        )

    # Resolve the lights directory: a single session's folder for N=1, or a
    # per-bundle staging dir of symlinks for N>=2.
    if len(session_ids) == 1:
        lights_dir = session_lights_folder(conn, session_ids[0])
    else:
        lights_dir = _stage_multi_session_lights(conn, session_ids)

    # Calibration: take the first session's match. Multi-session bundles are
    # already guaranteed to share gain/exptime/instrument by _assert_compatible.
    cal_session = session_ids[0]

    inputs: dict[str, Ref | list[Ref]] = {}

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

            # Convention 2: a MASTER_FITS_LIST port named 'dark' gets a per-
            # bin match. Bundles that mix exposures or span a wide temp range
            # produce more than one master in the list; homogeneous bundles
            # produce one. The calibrate node consumes the list and routes
            # each frame to its matching dark.
            if port_type is PortType.MASTER_FITS_LIST and port_name == "dark":
                if cal.mode == "none":
                    if port_name in node_cls.optional_inputs:
                        continue
                    raise CalibrationMissing(
                        f"template requires master {port_name} but "
                        f"calibration mode is 'none'."
                    )
                if cal.mode == "explicit" and port_name in cal.master_ids:
                    # Explicit override forces a single master across every
                    # frame, regardless of bin. Useful for "stack this with
                    # my-favorite-dark-no-matter-what" overrides.
                    master_id = cal.master_ids[port_name]
                    row = conn.execute(
                        "SELECT path FROM masters WHERE id = ?", (master_id,)
                    ).fetchone()
                    if row is None:
                        raise CalibrationMissing(
                            f"explicit master {port_name}={master_id} not found"
                        )
                    dark_refs = [
                        Ref(
                            node_hash="ext",
                            port=port_name,
                            path=Path(row["path"]),
                            type=PortType.MASTER_FITS,
                        )
                    ]
                else:
                    bins = match_bundle_darks(conn, session_ids)
                    unique_paths: dict[Path, None] = {}
                    for b in bins:
                        if b.master_path is not None:
                            unique_paths.setdefault(b.master_path, None)
                    if not unique_paths:
                        if port_name in node_cls.optional_inputs:
                            continue
                        raise CalibrationMissing(
                            f"no matched master {port_name} for any bin in "
                            f"bundle {session_ids}; explicit override or "
                            "scoped scan needed"
                        )
                    dark_refs = [
                        Ref(
                            node_hash="ext",
                            port=port_name,
                            path=p,
                            type=PortType.MASTER_FITS,
                        )
                        for p in unique_paths
                    ]
                inputs[external_key] = dark_refs
                continue

            # Convention 3: a MASTER_FITS port named after a calibration kind
            # (flat, bias, or a legacy `dark` port pre-list-port migration)
            # gets the catalog's per-session match.
            if port_type is PortType.MASTER_FITS and port_name in (
                "dark",
                "flat",
                "bias",
            ):
                if cal.mode == "none":
                    if port_name in node_cls.optional_inputs:
                        continue
                    raise CalibrationMissing(
                        f"template requires master {port_name} but "
                        f"calibration mode is 'none'. Mark the port "
                        f"optional on the node to allow uncalibrated runs."
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
                    matched = matched_master_path(conn, cal_session, port_name)
                    if matched is None:
                        if port_name in node_cls.optional_inputs:
                            continue
                        raise CalibrationMissing(
                            f"no matched master {port_name} for session "
                            f"{cal_session}; explicit override or scoped scan needed"
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
        session_ids=[str(sid) for sid in session_ids],
        calibration=cal,
        inputs=inputs,
    )


# ---------------------------------------------------------------------------
# Multi-session helpers
# ---------------------------------------------------------------------------


def _load_sessions(
    conn: sqlite3.Connection, session_ids: list[int]
) -> list[sqlite3.Row]:
    """Fetch session rows in the order requested. Raises SessionNotFound if
    any id is missing — that's a programmer error from the API layer, since
    the UI already filtered to known sessions."""
    placeholders = ",".join("?" for _ in session_ids)
    rows = conn.execute(
        f"SELECT * FROM sessions WHERE id IN ({placeholders})",  # noqa: S608
        session_ids,
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    out: list[sqlite3.Row] = []
    for sid in session_ids:
        row = by_id.get(sid)
        if row is None:
            raise SessionNotFound(f"session {sid} not found")
        out.append(row)
    return out


def _assert_compatible(sessions: list[sqlite3.Row]) -> None:
    """Raise IncompatibleSessions if the bundle disagrees on any
    COMPAT_FIELDS value."""
    head = sessions[0]
    conflicts: list[str] = []
    for field in COMPAT_FIELDS:
        head_val = head[field]
        for s in sessions[1:]:
            if s[field] != head_val:
                values = sorted(
                    {repr(s[field]) for s in sessions}
                    | {repr(head_val)}
                )
                conflicts.append(f"{field}: {', '.join(values)}")
                break
    if conflicts:
        raise IncompatibleSessions(
            "sessions don't share required metadata: "
            + "; ".join(conflicts)
        )


def _stage_multi_session_lights(
    conn: sqlite3.Connection, session_ids: list[int]
) -> Path:
    """Materialize a flat directory of symlinks pointing at every light
    frame across the bundle.

    The directory path is deterministic per (sorted) bundle so re-runs hit
    the same convert_lights cache entry. Filenames are prefixed with
    `s<session_id>__` to avoid collisions when two sessions share frame
    numbers (Dwarf 3 starts every session at frame 0001).

    We rebuild links idempotently each call so newly-scanned frames in any
    session are picked up without an explicit reset.
    """
    sorted_ids = sorted(session_ids)
    bundle_key = ",".join(str(sid) for sid in sorted_ids)
    h = hashlib.sha1(bundle_key.encode("utf-8")).hexdigest()[:16]
    stage_root = astrolab_home() / "lights_stage" / h
    stage_root.mkdir(parents=True, exist_ok=True)

    placeholders = ",".join("?" for _ in sorted_ids)
    rows = conn.execute(
        f"""
        SELECT sf.session_id, f.path
        FROM session_frames sf
        JOIN frames f ON f.id = sf.frame_id
        WHERE sf.session_id IN ({placeholders})
          AND f.image_type = 'LIGHT'
        ORDER BY sf.session_id, f.path
        """,  # noqa: S608  (placeholders are ints)
        sorted_ids,
    ).fetchall()
    if not rows:
        raise SessionNotFound(
            f"no light frames found across sessions {sorted_ids}"
        )

    desired: dict[str, Path] = {}
    for r in rows:
        src = Path(r["path"]).resolve()
        # Prefix avoids clashes; underscore separator keeps it path-safe.
        link_name = f"s{r['session_id']}__{src.name}"
        desired[link_name] = src

    # Drop any stale links that are no longer expected (e.g. a session
    # was rescanned and frames were removed).
    for child in stage_root.iterdir():
        if child.name not in desired:
            with contextlib.suppress(OSError):
                child.unlink()

    for link_name, src in desired.items():
        link = stage_root / link_name
        if link.is_symlink() or link.exists():
            try:
                # If the link already points where we want, leave it alone.
                if link.is_symlink() and Path(link.readlink()) == src:
                    continue
                link.unlink()
            except OSError:
                pass
        try:
            link.symlink_to(src)
        except OSError as exc:
            raise JobBuildError(
                f"could not stage multi-session lights at {link}: {exc}"
            ) from exc

    return stage_root
