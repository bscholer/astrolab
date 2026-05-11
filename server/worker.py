"""Worker process entrypoint.

`python -m server.worker` runs the job worker as its own systemd unit so a
deploy-restart of astrolab-api can't SIGTERM in-flight Siril subprocesses.

Lifecycle:
 - SIGUSR1   drain then exit (systemctl reload). The in-flight job is left
             alone; the loop just stops claiming new ones.
 - SIGTERM   drain AND cancel the running job. systemctl stop sends this;
             the unit's TimeoutStopSec=86400 gives Siril time to actually
             stop.
 - SIGINT    same as SIGTERM (developer ergonomics under ctrl-c).
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

# Populate the node registry. nodes.basic registers every concrete node via
# decorator side-effect at import time; without this import the runtime
# can't find anything when it tries to dispatch a template.
import nodes.basic  # noqa: F401

from .cache import ContentCache
from .jobs import JobManager, JobWorker

log = logging.getLogger("astrolab.worker")

IDLE_POLL_SECONDS = 0.25


class _Shutdown:
    """Two-event coordinator. `stop` ends the loop on the next idle tick;
    `cancel_current` also asks the worker to abort the active job."""

    def __init__(self) -> None:
        self.stop = threading.Event()
        self.cancel_current = threading.Event()

    def install_handlers(self) -> None:
        def on_usr1(signum: int, frame: object) -> None:
            _ = signum, frame
            log.info("SIGUSR1: drain requested, will exit when idle")
            self.stop.set()

        def on_term(signum: int, frame: object) -> None:
            _ = frame
            log.info("signal %d: drain + cancel in-flight job", signum)
            self.stop.set()
            self.cancel_current.set()

        signal.signal(signal.SIGUSR1, on_usr1)
        signal.signal(signal.SIGTERM, on_term)
        signal.signal(signal.SIGINT, on_term)


def _run_loop(worker: JobWorker, shutdown: _Shutdown) -> None:
    reclaimed = worker.reclaim_stale()
    if reclaimed:
        log.info("reclaimed %d stale running job(s)", reclaimed)
    while not shutdown.stop.is_set():
        record = worker.claim_next_queued()
        if record is None:
            shutdown.stop.wait(IDLE_POLL_SECONDS)
            continue
        if shutdown.cancel_current.is_set():
            # Caller wants this job killed too. Set cancel_requested on
            # the row; the worker's own monitor thread will see it and
            # forward to the runtime token. Use a one-shot JobManager
            # rather than reaching into JobWorker internals.
            JobManager(db_path=worker.db_path).cancel(record.id)
            shutdown.cancel_current.clear()
        worker._run(record)  # noqa: SLF001 (worker drives its own _run)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="server.worker")
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Path to the catalog SQLite DB (default: platform default).",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("ASTROLAB_LOG_LEVEL", "INFO"),
        help="Logging level (default: INFO or $ASTROLAB_LOG_LEVEL).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    worker = JobWorker(cache=ContentCache(), db_path=args.db)
    shutdown = _Shutdown()
    shutdown.install_handlers()

    log.info("astrolab worker starting (pid=%d, db=%s)", os.getpid(), args.db)
    try:
        _run_loop(worker, shutdown)
    except Exception:  # pragma: no cover  (defensive: log + exit non-zero)
        log.exception("worker loop crashed")
        return 1
    log.info("astrolab worker exited cleanly")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
