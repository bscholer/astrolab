"""Copy new capture sessions from a scope service into the capture root."""

from __future__ import annotations

import base64
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from server.scope_service import ScopeServiceClient
from server.telemetry import traced

log = logging.getLogger(__name__)

SESSION_PREFIX = "DWARF_RAW_"
CHUNK_BYTES = 1_048_576


def list_sessions(client: ScopeServiceClient) -> list[str]:
    entries = client.call("listFiles", {"path": ""})["entries"]
    return sorted(e["name"] for e in entries if e["dir"] and e["name"].startswith(SESSION_PREFIX))


def copy_file(client: ScopeServiceClient, remote: str, size: int, local: Path) -> bool:
    """Copy one file. Resume a partial copy. Return False when the local copy is complete."""
    if local.exists() and local.stat().st_size == size:
        return False
    part = local.with_name(local.name + ".part")
    offset = part.stat().st_size if part.exists() else 0
    if offset > size:
        offset = 0
    local.parent.mkdir(parents=True, exist_ok=True)
    with part.open("r+b" if offset else "wb") as out:
        out.seek(offset)
        while offset < size:
            chunk = client.call(
                "readFileChunk", {"path": remote, "offset": offset, "length": CHUNK_BYTES}
            )
            data = base64.b64decode(chunk["data"])
            if not data:
                raise OSError(f"empty read at {offset} of {remote}")
            out.write(data)
            offset += len(data)
    part.replace(local)
    return True


def mirror_session(client: ScopeServiceClient, session: str, root: Path) -> int:
    copied = 0
    for entry in client.call("listFiles", {"path": session})["entries"]:
        if entry["dir"]:
            continue
        name = entry["name"]
        if copy_file(client, f"{session}/{name}", entry["size"], root / session / name):
            copied += 1
    return copied


class Mirror:
    """Background copier. It skips sessions that existed when it first ran."""

    def __init__(
        self,
        get_client: Callable[[], ScopeServiceClient | None],
        get_root: Callable[[], Path | None],
        load_baseline: Callable[[], list[str] | None],
        save_baseline: Callable[[list[str]], None],
        on_copied: Callable[[], None],
        interval_s: float = 60,
        on_session_copied: Callable[[str], None] | None = None,
    ) -> None:
        self._get_client = get_client
        self._get_root = get_root
        self._load_baseline = load_baseline
        self._save_baseline = save_baseline
        self._on_copied = on_copied
        self._on_session_copied = on_session_copied or (lambda _name: None)
        self.interval_s = interval_s
        self._lock = threading.Lock()
        self.state: dict = {"state": "idle", "files_copied": 0, "last_ok": None, "error": None}

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.state)

    def _set(self, **kw) -> None:
        with self._lock:
            self.state.update(kw)

    @traced("scope.mirror")
    def run_once(self) -> int:
        client, root = self._get_client(), self._get_root()
        if client is None or root is None:
            self._set(state="idle", error=None)
            return 0
        try:
            if not client.has("files"):
                self._set(state="idle", error=None)
                return 0
            sessions = list_sessions(client)
            baseline = self._load_baseline()
            if baseline is None:
                self._save_baseline(sessions)
                baseline = sessions
            copied = 0
            for session in sessions:
                if session in baseline:
                    continue
                self._set(state="copying", current=session)
                n = mirror_session(client, session, root)
                copied += n
                if n:
                    self._on_session_copied(session)
            self._set(
                state="idle",
                current=None,
                error=None,
                last_ok=time.time(),
                files_copied=self.snapshot()["files_copied"] + copied,
            )
            if copied:
                self._on_copied()
            return copied
        except Exception as exc:
            log.warning("scope mirror failed: %s", exc)
            self._set(state="error", error=str(exc))
            return 0

    def start(self) -> None:
        def loop() -> None:
            while True:
                self.run_once()
                time.sleep(self.interval_s)

        threading.Thread(target=loop, name="astrolab-scope-mirror", daemon=True).start()
