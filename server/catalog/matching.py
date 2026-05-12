"""Calibration matching.

Given a session row, choose the best master_dark, master_flat, master_bias.
Rules per the README, tunable per camera; the defaults here are a starting
point and will tighten once we have more nights of data to validate against.

| kind | match by                                   | tolerance              |
|------|--------------------------------------------|------------------------|
| dark | instrument, camera, gain, exptime, bin     | temp +/- 5C; exp exact |
| flat | instrument, camera, filter, bin            | date proximity         |
| bias | instrument, camera, bin                    | exact                  |

Note on Dwarf 3 specifics: factory bias and flats encode a `gain_N` index
that is *not* the photographic gain on lights (which is `GAIN_60` etc.).
Their photographic-gain field is None and the matcher does not filter on
it, otherwise no flat or bias would ever match a real session. Flats are
matched on filter (Astro / VIS / Duo-Band, derived from the file's
`ir_0/1/2` suffix); bias is matched on bin only.

For darks, all candidates within DARK_TEMP_TOLERANCE_C are ranked by
(temp_delta_bin, -stack_count): candidates in the same 1-C bin are treated
as thermally equivalent and the deeper stack wins.  A 0-delta master with
only 3 frames will therefore lose to a 0.5-C-off master with 10 frames.
'exact' quality is reported when the winner's delta is zero; 'approx'
otherwise.  'none' means no candidate satisfies the hard equality
constraints on exp/gain/bin or falls within the temperature window.

User overrides land in calibration_matches.overridden=1 and are not
clobbered by automatic matching.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any

from .models import MatchQuality

DARK_TEMP_TOLERANCE_C: float = 5.0
"""Allowed temperature delta for an 'approx' dark match.

5 C covers the typical spread between Dwarf 3 factory darks (captured at
fixed sensor temps) and real-world session temps without pulling in darks
that are thermally meaningless.  The original 3 C limit was too tight: it
rejected factory masters that differed by 4-5 C and left sessions with no
dark at all.
"""

DARK_STACK_BIN_C: float = 2.0
"""Temperature resolution used when comparing candidates by stack depth.

Candidates whose temp delta falls in the same 2-C bin are considered
thermally equivalent; the one with the higher stack_count wins.  This
prevents a shallow exact-temp master from beating a much deeper master
that is only 1-2 C off -- the typical spread between Dwarf 3 factory
dark temps and real session sensor temps.
"""

log = logging.getLogger("astrolab.catalog.matching")


def _match_dark(
    conn: sqlite3.Connection, session: sqlite3.Row
) -> tuple[int | None, MatchQuality, dict[str, Any]]:
    if session["instrument"] is None or session["exptime"] is None:
        return None, "none", {"reason": "session missing instrument or exptime"}

    candidates = conn.execute(
        """
        SELECT id, ccd_temp, gain, binning, stack_count, exptime
        FROM masters
        WHERE kind = 'dark'
          AND instrument = ?
          AND ABS(IFNULL(exptime, 0) - ?) < 0.001
          AND IFNULL(gain, -1) = IFNULL(?, -1)
          AND IFNULL(binning, -1) = IFNULL(?, -1)
          AND (camera = ? OR camera IS NULL OR ? IS NULL)
        """,
        (
            session["instrument"],
            session["exptime"],
            session["gain"],
            session["binning"],
            session["camera"],
            session["camera"],
        ),
    ).fetchall()

    if not candidates:
        return None, "none", {"reason": "no dark candidates with matching exp/gain/bin"}

    session_temp = session.get("avg_ccd_temp") if isinstance(session, dict) else None
    if session_temp is None:
        # sqlite3.Row doesn't behave like a dict for unknown keys; fall through.
        try:
            session_temp = session["avg_ccd_temp"]
        except (IndexError, KeyError):
            session_temp = None

    if session_temp is None:
        # Without a session temp, accept the candidate with the largest stack
        # (deepest signal-to-noise) and call it approx.
        chosen = max(candidates, key=lambda r: (r["stack_count"] or 0))
        return (
            chosen["id"],
            "approx",
            {"reason": "no session ccd_temp; chose deepest stack"},
        )

    in_tolerance = [
        c for c in candidates if abs((c["ccd_temp"] or 0) - session_temp) <= DARK_TEMP_TOLERANCE_C
    ]
    if not in_tolerance:
        return (
            None,
            "none",
            {
                "reason": (
                    f"no dark within +/-{DARK_TEMP_TOLERANCE_C:g}C of "
                    f"session temp {session_temp:.1f}C"
                )
            },
        )

    # Rank by (temp-delta bin, -stack_count).  Candidates in the same 1-C
    # bin are thermally equivalent; prefer the deeper stack so a shallow
    # exact-temp master does not beat a much better-sampled nearby master.
    def _score(r: sqlite3.Row) -> tuple[float, int]:
        delta = abs((r["ccd_temp"] or 0) - session_temp)
        bin_ = int(delta / DARK_STACK_BIN_C)
        return (bin_, -(r["stack_count"] or 0))

    chosen = min(in_tolerance, key=_score)
    delta_c = float((chosen["ccd_temp"] or 0) - session_temp)
    quality: MatchQuality = "exact" if delta_c == 0.0 else "approx"
    return chosen["id"], quality, {"delta_C": delta_c}


def _match_flat(
    conn: sqlite3.Connection, session: sqlite3.Row
) -> tuple[int | None, MatchQuality, dict[str, Any]]:
    if session["instrument"] is None:
        return None, "none", {"reason": "session missing instrument"}
    # Match on filter + binning + camera; deliberately NOT on photographic
    # gain (factory flats use a different gain index) or exptime (flats are
    # exposure-independent).
    candidates = conn.execute(
        """
        SELECT id, date_built, stack_count
        FROM masters
        WHERE kind = 'flat'
          AND instrument = ?
          AND IFNULL(filter, '') = IFNULL(?, '')
          AND IFNULL(binning, -1) = IFNULL(?, -1)
          AND (camera = ? OR camera IS NULL OR ? IS NULL)
        """,
        (
            session["instrument"],
            session["filter"],
            session["binning"],
            session["camera"],
            session["camera"],
        ),
    ).fetchall()
    if not candidates:
        return None, "none", {
            "reason": (
                f"no flat for filter={session['filter']!r} bin={session['binning']!r} "
                f"on {session['camera'] or 'any camera'}"
            )
        }
    chosen = max(candidates, key=lambda r: (r["date_built"] or "", r["stack_count"] or 0))
    return chosen["id"], "exact", {}


def _match_bias(
    conn: sqlite3.Connection, session: sqlite3.Row
) -> tuple[int | None, MatchQuality, dict[str, Any]]:
    if session["instrument"] is None:
        return None, "none", {"reason": "session missing instrument"}
    # Bias is binning-only; factory bias has no exposure, no filter, and
    # the `gain_N` in its filename is not photographic gain (so don't match
    # on session gain).
    candidates = conn.execute(
        """
        SELECT id, stack_count
        FROM masters
        WHERE kind = 'bias'
          AND instrument = ?
          AND IFNULL(binning, -1) = IFNULL(?, -1)
          AND (camera = ? OR camera IS NULL OR ? IS NULL)
        """,
        (
            session["instrument"],
            session["binning"],
            session["camera"],
            session["camera"],
        ),
    ).fetchall()
    if not candidates:
        return None, "none", {
            "reason": (
                f"no bias for bin={session['binning']!r} "
                f"on {session['camera'] or 'any camera'}"
            )
        }
    chosen = max(candidates, key=lambda r: (r["stack_count"] or 0))
    return chosen["id"], "exact", {}


_MATCHERS = {
    "dark": _match_dark,
    "flat": _match_flat,
    "bias": _match_bias,
}


def _session_with_temp(conn: sqlite3.Connection, session_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT s.*,
               (SELECT AVG(f.ccd_temp) FROM frames f
                JOIN session_frames sf ON sf.frame_id = f.id
                WHERE sf.session_id = s.id AND f.ccd_temp IS NOT NULL) AS avg_ccd_temp
        FROM sessions s
        WHERE s.id = ?
        """,
        (session_id,),
    ).fetchone()


def match_session(conn: sqlite3.Connection, session_id: int) -> dict[str, dict[str, Any]]:
    """Compute and persist calibration matches for one session.

    Returns a dict mapping kind -> {master_id, match_quality, details}. User
    overrides (overridden=1) are preserved.
    """
    session = _session_with_temp(conn, session_id)
    if session is None:
        raise ValueError(f"session {session_id} not found")

    out: dict[str, dict[str, Any]] = {}
    now = time.time()
    for kind, matcher in _MATCHERS.items():
        existing = conn.execute(
            "SELECT overridden FROM calibration_matches WHERE session_id = ? AND kind = ?",
            (session_id, kind),
        ).fetchone()
        if existing is not None and existing["overridden"]:
            existing_full = conn.execute(
                "SELECT master_id, match_quality, details FROM calibration_matches "
                "WHERE session_id = ? AND kind = ?",
                (session_id, kind),
            ).fetchone()
            details = json.loads(existing_full["details"]) if existing_full["details"] else None
            out[kind] = {
                "master_id": existing_full["master_id"],
                "match_quality": existing_full["match_quality"],
                "details": details,
                "overridden": True,
            }
            continue

        master_id, quality, details = matcher(conn, session)
        out[kind] = {
            "master_id": master_id,
            "match_quality": quality,
            "details": details,
            "overridden": False,
        }
        conn.execute(
            """
            INSERT INTO calibration_matches
                (session_id, kind, master_id, match_quality, details, overridden, updated_at)
            VALUES (?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT(session_id, kind) DO UPDATE SET
                master_id     = excluded.master_id,
                match_quality = excluded.match_quality,
                details       = excluded.details,
                updated_at    = excluded.updated_at
            WHERE calibration_matches.overridden = 0
            """,
            (session_id, kind, master_id, quality, json.dumps(details), now),
        )
    return out


def match_all_sessions(conn: sqlite3.Connection) -> int:
    """Recompute matches for every session. Returns the count processed."""
    rows = conn.execute("SELECT id FROM sessions").fetchall()
    for r in rows:
        match_session(conn, r["id"])
    return len(rows)
