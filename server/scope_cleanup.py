"""Free space on the scope SD card by deleting capture sessions that the mirror holds in full."""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from server.scope_mirror import list_sessions
from server.scope_service import ScopeServiceClient, ScopeServiceError
from server.telemetry import traced

log = logging.getLogger(__name__)

KEEP_DAYS = 7
LOW_FREE_GB = 20.0
TARGET_FREE_GB = 40.0
MIN_AGE_S = 3600
EVERY_S = 3600
STAMP = re.compile(r"(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-\d+$")


def session_start(name: str) -> datetime | None:
    """Return the start time in the name. The name holds scope local time, so ages are rough."""
    m = STAMP.search(name)
    if not m:
        return None
    y, mo, d, h, mi, s = (int(g) for g in m.groups())
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


def mirrored_size(client: ScopeServiceClient, session: str, root: Path) -> int | None:
    """Return the session size if every scope file has a same-size copy under root, else None."""
    entries = [e for e in client.call("listFiles", {"path": session})["entries"] if not e["dir"]]
    if not entries:
        return None
    for e in entries:
        local = root / session / e["name"]
        if not local.is_file() or local.stat().st_size != e["size"]:
            return None
    return sum(e["size"] for e in entries)


def pick(
    sessions: list[tuple[str, int]],
    now: datetime,
    free_gb: float | None,
    keep_days: float = KEEP_DAYS,
) -> list[str]:
    """Choose sessions to delete from (name, size) pairs that the mirror holds in full.

    Everything older than keep_days goes. Under LOW_FREE_GB the oldest of the rest go too,
    until the estimate reaches TARGET_FREE_GB. Sessions under MIN_AGE_S old never go."""
    aged = []
    for name, size in sessions:
        start = session_start(name)
        if start is not None and (now - start).total_seconds() >= MIN_AGE_S:
            aged.append((start, name, size))
    aged.sort()
    chosen, freed = [], 0.0
    for start, name, size in aged:
        if (now - start).days >= keep_days:
            chosen.append(name)
            freed += size / 1e9
    if free_gb is not None and free_gb < LOW_FREE_GB:
        for _, name, size in aged:
            if free_gb + freed >= TARGET_FREE_GB:
                break
            if name not in chosen:
                chosen.append(name)
                freed += size / 1e9
    return chosen


class Cleaner:
    def __init__(
        self,
        get_client: Callable[[], ScopeServiceClient | None],
        get_root: Callable[[], Path | None],
        notify: Callable[[str, str], None] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        get_purged: Callable[[], set[str]] | None = None,
    ) -> None:
        self._get_client = get_client
        self._get_root = get_root
        self._notify = notify or (lambda *_: None)
        self._clock = clock
        self._get_purged = get_purged or set
        self.last: dict = {"at": None, "deleted": [], "error": None}

    @traced("scope.cleanup")
    def run_once(self) -> list[str]:
        client, root = self._get_client(), self._get_root()
        if client is None or root is None:
            return []
        deleted: list[str] = []
        try:
            if not client.has("delete") or not client.has("files"):
                return []
            status = client.call("getStatus") or {}
            if (status.get("job") or {}).get("state") == "running" or status.get("busy_elsewhere"):
                return []
            held = []
            purged = self._get_purged()
            for session in list_sessions(client):
                if session in purged:  # copied, judged scrap, deleted here: done with it
                    entries = client.call("listFiles", {"path": session})["entries"]
                    held.append((session, sum(e["size"] for e in entries if not e["dir"])))
                    continue
                size = mirrored_size(client, session, root)
                if size is not None:
                    held.append((session, size))
            for session in pick(held, self._clock(), status.get("sd_free_gb")):
                client.call("deleteSession", {"session": session})
                log.warning("deleted %s from the scope, the mirror holds a full copy", session)
                deleted.append(session)
            if deleted:
                self._notify(
                    "Scope", f"Freed scope SD card space: deleted {len(deleted)} mirrored sessions"
                )
            self.last = {"at": time.time(), "deleted": deleted, "error": None}
        except ScopeServiceError as exc:
            log.warning("scope cleanup failed: %s", exc)
            self.last = {"at": time.time(), "deleted": deleted, "error": str(exc)}
        return deleted

    def start(self) -> None:
        def loop() -> None:
            time.sleep(300)
            while True:
                try:
                    self.run_once()
                except Exception:
                    log.exception("scope cleanup tick failed")
                time.sleep(EVERY_S)

        threading.Thread(target=loop, name="astrolab-scope-cleanup", daemon=True).start()
