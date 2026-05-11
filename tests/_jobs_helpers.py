"""Test helpers for the API/worker job split.

The production worker runs as its own process (`python -m server.worker`).
For tests, we drive a JobWorker in a background thread so the API tests can
submit a job and observe it complete without spawning a subprocess."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from server.cache import ContentCache
from server.jobs import JobWorker


@contextmanager
def background_worker(
    cache: ContentCache, db_path: Path
) -> Iterator[JobWorker]:
    """Run a JobWorker in a thread until the context exits."""
    worker = JobWorker(cache=cache, db_path=db_path)
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            if worker.run_one_pending() is None:
                stop.wait(0.05)

    thread = threading.Thread(target=loop, name="test-job-worker", daemon=True)
    thread.start()
    try:
        yield worker
    finally:
        stop.set()
        thread.join(timeout=5)
