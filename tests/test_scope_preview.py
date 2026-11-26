"""Preview: newest session by timestamp, downscaled, cached until the stack grows."""

from __future__ import annotations

import base64
import io

from PIL import Image

from server import scope_preview
from server.scope_preview import stack_preview


def _jpeg(width: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, width // 2), (40, 40, 40)).save(buf, "JPEG")
    return buf.getvalue()


class Fake:
    def __init__(self, files):
        self.files = files
        self.reads = 0

    def call(self, op, payload):
        if op == "listFiles":
            if not payload["path"]:
                return {"entries": [{"name": n, "size": 0, "dir": True} for n in self.files]}
            return {
                "entries": [
                    {"name": n, "size": len(b), "dir": False}
                    for n, b in self.files[payload["path"]].items()
                ]
            }
        self.reads += 1
        session, name = payload["path"].split("/")
        data = self.files[session][name][payload["offset"] :][: payload["length"]]
        return {"data": base64.b64encode(data).decode()}


NEW = "DWARF_RAW_TELE_IC 1396_EXP_15_GAIN_60_2026-11-06-18-47-12-250"
OLD = "DWARF_RAW_TELE_zzz_EXP_15_GAIN_60_2026-11-05-22-57-32-526"


def test_newest_by_timestamp_not_name_and_downscaled():
    scope_preview._cache.clear()
    svc = Fake({OLD: {"stacked.jpg": _jpeg(200)}, NEW: {"stacked.jpg": _jpeg(3000)}})
    session, jpeg = stack_preview(svc)
    assert session == NEW
    assert Image.open(io.BytesIO(jpeg)).width == 1024


def test_cached_until_size_changes():
    scope_preview._cache.clear()
    svc = Fake({NEW: {"stacked.jpg": _jpeg(2000)}})
    stack_preview(svc)
    reads = svc.reads
    stack_preview(svc)
    assert svc.reads == reads
    svc.files[NEW]["stacked.jpg"] = _jpeg(2400)
    stack_preview(svc)
    assert svc.reads > reads


def test_no_stack_yet():
    scope_preview._cache.clear()
    assert stack_preview(Fake({NEW: {"1.fits": b"x"}})) is None
