"""SQLite catalog database.

The catalog persists what we know about every frame on disk: target,
session, calibration coverage, header summary. Phase 1.a only writes the
frames / sessions / targets / session_frames tables; masters and
calibration_matches land in a later slice.

Schema migration is forward-only and idempotent. `schema_version` holds the
last applied migration; new migrations append to MIGRATIONS and bump the
version. We avoid an ORM intentionally per README ("no ORM unless we feel
the need"); a thin row-dict layer is enough.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from server.paths import astrolab_home

CURRENT_SCHEMA_VERSION = 5


# Each entry runs once when the DB is at version N-1, advancing it to N.
MIGRATIONS: dict[int, list[str]] = {
    1: [
        """
        CREATE TABLE targets (
            id              INTEGER PRIMARY KEY,
            name            TEXT NOT NULL UNIQUE,
            aliases         TEXT,
            ra              REAL,
            dec             REAL
        )
        """,
        """
        CREATE TABLE frames (
            id              INTEGER PRIMARY KEY,
            file_hash       TEXT,
            path            TEXT NOT NULL UNIQUE,
            inode           INTEGER,
            mtime           REAL,
            size            INTEGER,
            image_type      TEXT,
            quality         TEXT,
            object          TEXT,
            instrument      TEXT,
            camera          TEXT,
            filter          TEXT,
            exptime         REAL,
            gain            INTEGER,
            binning         INTEGER,
            ccd_temp        REAL,
            date_obs        TEXT,
            ra              REAL,
            dec             REAL,
            scope_id        TEXT,
            session_key     TEXT,
            fits_headers    BLOB,
            scanned_at      REAL
        )
        """,
        "CREATE INDEX idx_frames_object ON frames(object)",
        "CREATE INDEX idx_frames_image_type ON frames(image_type)",
        "CREATE INDEX idx_frames_session_key ON frames(session_key)",
        "CREATE INDEX idx_frames_inode_mtime ON frames(inode, mtime)",
        """
        CREATE TABLE sessions (
            id              INTEGER PRIMARY KEY,
            scope_id        TEXT,
            session_key     TEXT NOT NULL UNIQUE,
            target_id       INTEGER REFERENCES targets(id),
            instrument      TEXT,
            camera          TEXT,
            filter          TEXT,
            exptime         REAL,
            gain            INTEGER,
            binning         INTEGER,
            started_at      TEXT,
            ended_at        TEXT,
            frame_count     INTEGER,
            failed_count    INTEGER,
            notes           TEXT
        )
        """,
        """
        CREATE TABLE session_frames (
            session_id      INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
            frame_id        INTEGER REFERENCES frames(id) ON DELETE CASCADE,
            PRIMARY KEY (session_id, frame_id)
        )
        """,
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY)",
        "INSERT INTO schema_version (version) VALUES (1)",
    ],
    2: [
        # Pre-built or DAG-built calibration masters. Dwarf 3 ships factory
        # masters in CALI_FRAME; later, a master-build job will produce ones
        # whose source_frame_ids reference rows in `frames`. cache_ref is the
        # filesystem location of the master image.
        """
        CREATE TABLE masters (
            id              INTEGER PRIMARY KEY,
            kind            TEXT NOT NULL,        -- 'dark' | 'flat' | 'bias'
            scope_id        TEXT,
            source          TEXT,                  -- 'factory' | 'user' | 'astrolab'
            instrument      TEXT,
            camera          TEXT,                  -- TELE / WIDE for Dwarf
            filter          TEXT,                  -- relevant for flats
            exptime         REAL,                  -- relevant for darks
            gain            INTEGER,
            binning         INTEGER,               -- Dwarf res-mode for Dwarf 3 (1=4k, 2=2k)
            ccd_temp        REAL,
            stack_count     INTEGER,               -- source frame count for the stack
            file_hash       TEXT,
            path            TEXT NOT NULL UNIQUE,
            inode           INTEGER,
            mtime           REAL,
            size            INTEGER,
            date_built      TEXT,
            cache_ref       TEXT,                  -- content-cache pointer or external path
            source_frame_ids TEXT,                 -- JSON array of frames.id; null for factory
            scanned_at      REAL
        )
        """,
        "CREATE INDEX idx_masters_kind ON masters(kind)",
        """
        CREATE INDEX idx_masters_match ON masters(
            kind, instrument, camera, gain, exptime, binning, ccd_temp
        )
        """,
        # Best-known calibration choice for each session, recomputed on demand
        # by the matcher. `master_id` is null when no acceptable match exists;
        # the UI should make that loud (per README).
        """
        CREATE TABLE calibration_matches (
            session_id      INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            kind            TEXT NOT NULL,         -- 'dark' | 'flat' | 'bias'
            master_id       INTEGER REFERENCES masters(id) ON DELETE SET NULL,
            match_quality   TEXT NOT NULL,         -- 'exact' | 'approx' | 'none'
            details         TEXT,                   -- JSON: deltas, candidate count, etc.
            overridden      INTEGER NOT NULL DEFAULT 0,
            updated_at      REAL,
            PRIMARY KEY (session_id, kind)
        )
        """,
        "INSERT INTO schema_version (version) VALUES (2)",
    ],
    3: [
        # Jobs and their event streams. Phase 2 keeps in-memory state for
        # speed; this gives durability across restarts. template_json and
        # job_json hold the full Pydantic dumps so we can reconstruct on
        # load without joining anything else. outputs_json is null until the
        # job completes; error is null unless something blew up.
        """
        CREATE TABLE jobs (
            id                TEXT PRIMARY KEY,
            status            TEXT NOT NULL,
            template_id       TEXT NOT NULL,
            template_version  INTEGER NOT NULL,
            template_json     TEXT NOT NULL,
            job_json          TEXT NOT NULL,
            outputs_json      TEXT,
            error             TEXT,
            submitted_at      TEXT NOT NULL,
            started_at        TEXT,
            finished_at       TEXT
        )
        """,
        "CREATE INDEX idx_jobs_status ON jobs(status)",
        "CREATE INDEX idx_jobs_submitted ON jobs(submitted_at DESC)",
        # Append-only event log per job. Event ordering = insertion order.
        # node_id / fraction / message / error are pulled out of the payload
        # for cheap querying; extra_json holds anything else (e.g. node hash).
        """
        CREATE TABLE job_events (
            id              INTEGER PRIMARY KEY,
            job_id          TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            seq             INTEGER NOT NULL,
            type            TEXT NOT NULL,
            timestamp       TEXT NOT NULL,
            node_id         TEXT,
            fraction        REAL,
            message         TEXT,
            error           TEXT,
            extra_json      TEXT
        )
        """,
        "CREATE INDEX idx_job_events_job ON job_events(job_id, seq)",
        "INSERT INTO schema_version (version) VALUES (3)",
    ],
    4: [
        # Renderings: a Rendering is a "live edit" of a stack from a source
        # session(s) under a chosen template. It holds the seed Job (external
        # inputs + calibration choice) and a sequence of param-override
        # snapshots. Each snapshot is materialized as a Job; tweaking a slider
        # in the UI appends a new history entry whose Job runs with the
        # changed param_overrides. The cache makes upstream nodes free across
        # tweaks. current_seq is the pointer the UI is "looking at": undo
        # rewinds it, redo advances, edits create-and-advance.
        """
        CREATE TABLE renderings (
            id                    TEXT PRIMARY KEY,
            name                  TEXT NOT NULL,
            template_id           TEXT NOT NULL,
            template_version      INTEGER NOT NULL,
            template_json         TEXT NOT NULL,
            base_job_json         TEXT NOT NULL,
            current_seq           INTEGER NOT NULL,
            draft_mode            INTEGER NOT NULL DEFAULT 0,
            source_session_ids    TEXT NOT NULL,
            created_at            TEXT NOT NULL,
            updated_at            TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_renderings_updated ON renderings(updated_at DESC)",
        # Append-only edit log. seq is monotonic per rendering; revert moves
        # the current_seq pointer rather than truncating, so prior entries
        # remain reachable for "compare" affordances later. job_id is not a
        # FK so deleting an old job (eg cache cleanup) leaves the entry as a
        # tombstone instead of cascading the rendering away.
        """
        CREATE TABLE rendering_history (
            id              INTEGER PRIMARY KEY,
            rendering_id    TEXT NOT NULL REFERENCES renderings(id) ON DELETE CASCADE,
            seq             INTEGER NOT NULL,
            job_id          TEXT NOT NULL,
            overrides_json  TEXT NOT NULL,
            label           TEXT,
            created_at      TEXT NOT NULL,
            UNIQUE (rendering_id, seq)
        )
        """,
        "CREATE INDEX idx_rh_rendering ON rendering_history(rendering_id, seq)",
        "INSERT INTO schema_version (version) VALUES (4)",
    ],
    5: [
        # Two storage-management additions:
        #   1. node_hashes on jobs lets us reverse-map cache entries back to
        #      the renderings that touched them, without re-walking the
        #      event log every time we render the storage UI.
        #   2. A simple key/value settings table holds system-wide knobs
        #      (cache budget cap, etc.). We use JSON-encoded values so the
        #      schema doesn't need to change per setting type.
        "ALTER TABLE jobs ADD COLUMN node_hashes_json TEXT",
        """
        CREATE TABLE settings (
            key             TEXT PRIMARY KEY,
            value_json      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        )
        """,
        "INSERT INTO schema_version (version) VALUES (5)",
    ],
}


def default_db_path() -> Path:
    return astrolab_home() / "astrolab.sqlite"


def _current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if row is None:
        return 0
    cur = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return cur[0] or 0


def migrate(conn: sqlite3.Connection) -> None:
    """Apply outstanding migrations. Idempotent."""
    have = _current_version(conn)
    for version in sorted(MIGRATIONS.keys()):
        if version <= have:
            continue
        with conn:
            for stmt in MIGRATIONS[version]:
                conn.execute(stmt)
            # Each migration is responsible for inserting its own
            # schema_version row.


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the catalog DB and ensure the schema is current.

    Foreign keys are enabled; row factory yields sqlite3.Row so callers get
    name-based access without needing to remember column ordering.
    """
    target = path if path is not None else default_db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


@contextmanager
def open_db(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()
