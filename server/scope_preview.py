"""Preview of the newest stack on a scope service, resized for phones."""

from __future__ import annotations

import base64
import io
import threading

from PIL import Image

from server.scope_mirror import SESSION_PREFIX
from server.scope_service import ScopeServiceClient

STACK_NAME = "stacked.jpg"
MAX_WIDTH = 1024
_lock = threading.Lock()
_cache: dict[str, tuple[int, bytes]] = {}


def _stamp(session: str) -> str:
    return session[-23:]


def newest_session(client: ScopeServiceClient) -> str | None:
    entries = client.call("listFiles", {"path": ""})["entries"]
    names = [e["name"] for e in entries if e["dir"] and e["name"].startswith(SESSION_PREFIX)]
    return max(names, key=_stamp) if names else None


def _download(client: ScopeServiceClient, path: str, size: int) -> bytes:
    out = bytearray()
    while len(out) < size:
        chunk = client.call("readFileChunk", {"path": path, "offset": len(out), "length": 1 << 20})
        data = base64.b64decode(chunk["data"])
        if not data:
            break
        out += data
    return bytes(out)


def stack_preview(client: ScopeServiceClient) -> tuple[str, bytes] | None:
    """Return (session, jpeg) for the newest session with a stack, or None."""
    session = newest_session(client)
    if session is None:
        return None
    entries = client.call("listFiles", {"path": session})["entries"]
    stack = next((e for e in entries if e["name"] == STACK_NAME and not e["dir"]), None)
    if stack is None:
        return None
    key = f"{session}/{STACK_NAME}"
    with _lock:
        hit = _cache.get(key)
    if hit and hit[0] == stack["size"]:
        return session, hit[1]
    raw = _download(client, key, stack["size"])
    img = Image.open(io.BytesIO(raw))
    if img.width > MAX_WIDTH:
        img = img.resize((MAX_WIDTH, round(img.height * MAX_WIDTH / img.width)))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=85)
    with _lock:
        _cache.clear()
        _cache[key] = (stack["size"], buf.getvalue())
    return session, buf.getvalue()
