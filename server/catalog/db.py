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

CURRENT_SCHEMA_VERSION = 16


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
    6: [
        # Rename: renderings -> projects, rendering_history -> project_history.
        # SQLite 3.26+ updates FK references during ALTER TABLE RENAME, so
        # the ON DELETE CASCADE link survives the rename. Indexes need to
        # be dropped + recreated; ALTER doesn't touch them.
        "DROP INDEX IF EXISTS idx_renderings_updated",
        "DROP INDEX IF EXISTS idx_rh_rendering",
        "ALTER TABLE renderings RENAME TO projects",
        "ALTER TABLE rendering_history RENAME TO project_history",
        "ALTER TABLE project_history RENAME COLUMN rendering_id TO project_id",
        "CREATE INDEX idx_projects_updated ON projects(updated_at DESC)",
        "CREATE INDEX idx_ph_project ON project_history(project_id, seq)",
        "INSERT INTO schema_version (version) VALUES (6)",
    ],
    7: [
        # Per-project cover image: lets the user pin which history seq
        # represents the project on the Projects list and Gallery. NULL
        # means "auto-pick the latest entry with outputs" (the v6
        # behavior, preserved as fallback in _attach_preview).
        "ALTER TABLE projects ADD COLUMN cover_seq INTEGER",
        "INSERT INTO schema_version (version) VALUES (7)",
    ],
    8: [
        # Gallery opt-in: history entries default to unpublished. The
        # gallery surfaces only published rows, so the user's iterate-
        # and-compare crumbs don't drown the curated keepers.
        "ALTER TABLE project_history ADD COLUMN published INTEGER NOT NULL DEFAULT 0",
        "INSERT INTO schema_version (version) VALUES (8)",
    ],
    9: [
        # Position-based target resolution. The scanner falls back to a
        # great-circle match against OpenNGC when the target's stored
        # name doesn't enrich on its own. resolved_canonical / *_arcmin
        # / resolved_at are scanner-managed. resolved_canonical_override
        # was the user pin (later dropped in migration 10).
        # resolved_source tags which path produced the current resolution
        # so the UI knows whether to render a "matched" caption.
        "ALTER TABLE targets ADD COLUMN resolved_canonical TEXT",
        "ALTER TABLE targets ADD COLUMN resolved_separation_arcmin REAL",
        "ALTER TABLE targets ADD COLUMN resolved_at TEXT",
        "ALTER TABLE targets ADD COLUMN resolved_canonical_override TEXT",
        "ALTER TABLE targets ADD COLUMN resolved_source TEXT",
        "INSERT INTO schema_version (version) VALUES (9)",
    ],
    10: [
        # Drop the unused override column. The grouping/override UI was
        # ripped out in favor of a per-session reassign action; subsequent
        # slices won't bring it back. SQLite 3.35+ supports DROP COLUMN
        # natively and both the dev box (3.45) and the Linux service box
        # (3.45) are well past that threshold. This migration is
        # irreversible: any pinned values are gone after it runs.
        "ALTER TABLE targets DROP COLUMN resolved_canonical_override",
        "INSERT INTO schema_version (version) VALUES (10)",
    ],
    11: [
        # Free-text notes/description on both projects and sessions. The
        # user attaches per-render context (capture conditions, gear
        # tweaks, processing rationale) that nothing else in the schema
        # captures. Nullable; empty/null means "no note" so the UI can
        # suppress empty labels cleanly.
        "ALTER TABLE projects ADD COLUMN description TEXT",
        "ALTER TABLE sessions ADD COLUMN description TEXT",
        "INSERT INTO schema_version (version) VALUES (11)",
    ],
    12: [
        # Tag history entries with a discriminator so future code paths
        # (revert, gallery filter, audit) can tell an override-edit apart
        # from a session-swap. `kind` defaults to 'edit' for back-compat;
        # session-swap entries land as 'swap_sessions' and carry the new
        # session id list + frame/integration totals in `snapshot_json`
        # so a later revert can navigate back through the swap.
        "ALTER TABLE project_history ADD COLUMN kind TEXT NOT NULL DEFAULT 'edit'",
        "ALTER TABLE project_history ADD COLUMN snapshot_json TEXT",
        "INSERT INTO schema_version (version) VALUES (12)",
    ],
    13: [
        # Worker/API process split. The worker now runs as its own
        # systemd unit so a PR-merge restart of astrolab-api doesn't
        # SIGTERM in-flight Siril subprocesses; that meant several
        # in-memory JobRecord fields have to cross a process boundary.
        #
        # cancel_requested: the API flips this to 1 on cancel; the
        #   worker's monitor thread polls and forwards to the
        #   threading.Event the runtime reads. (Was JobRecord.cancel_event.)
        # worker_pid: the worker stamps its PID at claim time so a
        #   subsequent worker boot can tell whether a 'running' row
        #   belongs to a still-alive process or a corpse that needs
        #   reclaiming.
        # heartbeat_at: the worker bumps this every few seconds while a
        #   job is live. Stale-claim reclaim treats anything older than
        #   STALE_HEARTBEAT_SECONDS (or a dead PID) as crashed.
        # force: was an in-memory-only JobRecord field; with submit
        #   running in the API process and execution in the worker, the
        #   force flag has to round-trip through the DB.
        "ALTER TABLE jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN worker_pid INTEGER",
        "ALTER TABLE jobs ADD COLUMN heartbeat_at TEXT",
        "ALTER TABLE jobs ADD COLUMN force INTEGER NOT NULL DEFAULT 0",
        "INSERT INTO schema_version (version) VALUES (13)",
    ],
    14: [
        # Drop session_key. It was a denormalized join column between
        # frames and sessions; the proper join goes through
        # session_frames (the junction table) and we use sessions.id as
        # the stable cross-scan identifier now.
        #
        # The clusterer (sessions.cluster_sessions) preserves sessions.id
        # across rescans by matching new clusters to existing sessions on
        # frame-id overlap, so projects pinned to a session.id survive a
        # rescan even if the cluster's first frame shifts (e.g. when a
        # back-dated frame arrives in a later scan).
        #
        # frames.session_key drops with a plain ALTER (no constraints on
        # it past the index). sessions.session_key had a UNIQUE
        # constraint that SQLite refuses to drop with ALTER, so we
        # round-trip through a rebuild table (CREATE new, copy rows,
        # DROP old, RENAME).
        "DROP INDEX IF EXISTS idx_frames_session_key",
        "ALTER TABLE frames DROP COLUMN session_key",
        """
        CREATE TABLE sessions_new (
            id              INTEGER PRIMARY KEY,
            scope_id        TEXT,
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
            notes           TEXT,
            description     TEXT
        )
        """,
        """
        INSERT INTO sessions_new (
            id, scope_id, target_id, instrument, camera, filter,
            exptime, gain, binning, started_at, ended_at,
            frame_count, failed_count, notes, description
        )
        SELECT
            id, scope_id, target_id, instrument, camera, filter,
            exptime, gain, binning, started_at, ended_at,
            frame_count, failed_count, notes, description
        FROM sessions
        """,
        "DROP TABLE sessions",
        "ALTER TABLE sessions_new RENAME TO sessions",
        "INSERT INTO schema_version (version) VALUES (14)",
    ],
    15: [
        # Force re-ingest of every existing frame and master on the next
        # scan. Earlier versions of filter_aliases canonicalized Dwarf 3's
        # 'Astro' IR-cut to the literal string 'None', which surfaces in
        # the UI as if the filter were missing. The new alias table
        # preserves source filter names except for the one real synonym
        # merge (Duo / Duo-Band / HaOIII), but the scanner's incremental
        # skip-if-unchanged path won't refresh existing rows on its own.
        # Nulling file_hash trips the "needs re-read" branch in scanner._
        # ingest_frame and _ingest_master so the next scan re-parses each
        # FITS header and writes the corrected filter value.
        "UPDATE frames SET file_hash = NULL",
        "UPDATE masters SET file_hash = NULL",
        "INSERT INTO schema_version (version) VALUES (15)",
    ],
    16: [
        # Persisted quality metrics on the jobs row. The /api/jobs/{id}/quality
        # endpoint used to recompute from disk on every call; that's fine for
        # one-off inspection but breaks the History strip, which wants per-
        # version Noise / Sharpness numbers ranked against the project's own
        # median. The worker now computes the same blob at completion and
        # stashes it here, so the project response can fan out N versions of
        # quality data without N disk reads.
        "ALTER TABLE jobs ADD COLUMN quality_json TEXT",
        "INSERT INTO schema_version (version) VALUES (16)",
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
    """Apply outstanding migrations. Idempotent and concurrency-safe.

    Two processes opening a fresh DB at the same time would otherwise both
    see version=0, both run migration 1's CREATE TABLE statements, and the
    loser hits "table targets already exists". BEGIN IMMEDIATE serializes
    one migration step at a time across processes; the re-read of
    _current_version inside the txn covers the case where the other
    process raced ahead of us between SELECT and BEGIN.
    """
    while True:
        have = _current_version(conn)
        pending = [v for v in sorted(MIGRATIONS.keys()) if v > have]
        if not pending:
            return
        version = pending[0]
        conn.execute("BEGIN IMMEDIATE")
        try:
            have_now = _current_version(conn)
            if version <= have_now:
                conn.execute("ROLLBACK")
                continue
            for stmt in MIGRATIONS[version]:
                conn.execute(stmt)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the catalog DB and ensure the schema is current.

    Foreign keys are enabled; row factory yields sqlite3.Row so callers get
    name-based access without needing to remember column ordering.
    """
    target = path if path is not None else default_db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False because FastAPI's
    # contextmanager_in_threadpool can land __enter__ and __exit__ on
    # different worker threads. We never share a single connection
    # across threads concurrently — open_db() yields a fresh one per
    # call and closes it on exit — so SQLite's check is just a noisy
    # tripwire here that throws 500s under load.
    conn = sqlite3.connect(target, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # busy_timeout has to come BEFORE other pragmas: the API and worker
    # processes both open the catalog on startup and their connect()
    # calls can race on the schema_version write inside migrate(). 10s
    # gives the loser plenty of room to wait for the winner to finish.
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    # PRAGMA journal_mode is special: it can fail with SQLITE_BUSY even
    # when busy_timeout is set, because changing journal mode requires
    # an exclusive lock the timeout sometimes doesn't wait for. Skip it
    # if the DB is already in WAL (the common case after first boot),
    # and retry a handful of times for the genuinely-fresh-DB race.
    current_mode = conn.execute("PRAGMA journal_mode").fetchone()
    if current_mode and str(current_mode[0]).lower() != "wal":
        import time as _time
        for attempt in range(20):
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                break
            except sqlite3.OperationalError:
                if attempt == 19:
                    raise
                _time.sleep(0.05)
    migrate(conn)
    return conn


@contextmanager
def open_db(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


def retry_on_locked(fn):
    """Retry a DB-touching callable on `OperationalError: database is locked`.

    `busy_timeout = 10000` inside connect() absorbs nearly all writer-vs-
    writer contention, but the rare WAL-checkpoint and journal-mode windows
    can still surface SQLITE_BUSY. When that happens inside _emit_event mid-
    pipeline (calibrate fires dozens of progress events; one unlucky one
    crashed the run), the exception bubbles out of ctx.progress and fails
    the whole job. This wrapper catches the locked/busy variants, sleeps
    with jittered exponential backoff, and re-invokes the callable. Any
    other OperationalError (schema mismatch, malformed disk image, ...)
    passes straight through.

    Wraps the entire callable, so each retry reopens its own connection;
    callers don't need to share state across attempts.
    """
    import functools
    import random
    import time as _time

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        attempts = 8
        delay = 0.03
        for i in range(attempts):
            try:
                return fn(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if "locked" not in msg and "busy" not in msg:
                    raise
                if i == attempts - 1:
                    raise
                _time.sleep(delay + random.random() * delay)
                delay = min(delay * 2, 1.0)
        # Unreachable; the loop either returns or re-raises.
        return None  # pragma: no cover

    return wrapper
