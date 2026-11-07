"""Mirror: new sessions are copied, interrupted copies resume, old sessions stay out."""

from __future__ import annotations

import base64
from pathlib import Path

from server.scope_mirror import Mirror, copy_file

FILES = {
    "DWARF_RAW_old": {"a.fits": b"old-frame"},
    "DWARF_RAW_new": {"1.fits": b"0123456789" * 30, "2.fits": b"abc"},
}


class FakeService:
    def __init__(self, files=FILES, chunk_fail_after=None):
        self.files = files
        self.reads: list[tuple[str, int]] = []
        self.chunk_fail_after = chunk_fail_after

    def has(self, capability: str) -> bool:
        return capability == "files"

    def call(self, op: str, payload: dict):
        if op == "listFiles":
            if not payload["path"]:
                return {"entries": [{"name": n, "size": 0, "dir": True} for n in self.files]}
            return {
                "entries": [
                    {"name": n, "size": len(b), "dir": False}
                    for n, b in self.files[payload["path"]].items()
                ]
            }
        session, name = payload["path"].split("/")
        self.reads.append((payload["path"], payload["offset"]))
        if self.chunk_fail_after is not None and len(self.reads) > self.chunk_fail_after:
            raise OSError("link dropped")
        data = self.files[session][name][payload["offset"] : payload["offset"] + payload["length"]]
        return {"data": base64.b64encode(data).decode()}


def make_mirror(tmp_path: Path, service, baseline=None, copied=None):
    tmp_path = tmp_path / "captures"
    tmp_path.mkdir(exist_ok=True)
    saved: list[list[str]] = [] if baseline is None else [baseline]
    return (
        Mirror(
            get_client=lambda: service,
            get_root=lambda: tmp_path,
            load_baseline=lambda: saved[-1] if saved else None,
            save_baseline=saved.append,
            on_copied=lambda: copied.append(1) if copied is not None else None,
        ),
        saved,
    )


def test_first_run_records_baseline_and_copies_nothing(tmp_path):
    scans: list[int] = []
    mirror, saved = make_mirror(tmp_path, FakeService(), copied=scans)
    assert mirror.run_once() == 0
    assert saved[-1] == ["DWARF_RAW_new", "DWARF_RAW_old"]
    assert not list((tmp_path / "captures").iterdir()) and not scans


def test_copies_only_sessions_added_after_the_baseline_then_scans_once(tmp_path):
    scans: list[int] = []
    mirror, _ = make_mirror(tmp_path, FakeService(), baseline=["DWARF_RAW_old"], copied=scans)
    assert mirror.run_once() == 2
    assert (tmp_path / "captures" / "DWARF_RAW_new" / "1.fits").read_bytes() == b"0123456789" * 30
    assert not (tmp_path / "captures" / "DWARF_RAW_old").exists() and scans == [1]
    assert mirror.run_once() == 0 and scans == [1]


def test_interrupted_copy_resumes_at_the_partial_size(tmp_path, monkeypatch):
    monkeypatch.setattr("server.scope_mirror.CHUNK_BYTES", 100)
    flaky = FakeService(chunk_fail_after=2)
    mirror, _ = make_mirror(tmp_path, flaky, baseline=["DWARF_RAW_old"])
    mirror.run_once()
    assert mirror.snapshot()["state"] == "error"
    part = tmp_path / "captures" / "DWARF_RAW_new" / "1.fits.part"
    assert part.stat().st_size == 200
    good = FakeService()
    final = tmp_path / "captures" / "DWARF_RAW_new" / "1.fits"
    copy_file(good, "DWARF_RAW_new/1.fits", 300, final)
    assert good.reads == [("DWARF_RAW_new/1.fits", 200)]
    assert final.read_bytes() == b"0123456789" * 30
