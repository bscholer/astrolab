"""NAS usage: every file on the share lands in exactly one kind."""

from __future__ import annotations

from pathlib import Path

from server.nas_usage import NasUsage, measure


def _put(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_measure_sorts_files_by_kind(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ASTROLAB_NAS_CAPTURES_DIR", "Astronomy")
    monkeypatch.setenv("ASTROLAB_NAS_APP_DIR", "astrolab")
    session = tmp_path / "Astronomy" / "DWARF_RAW_TELE_M 31_EXP_15_GAIN_60_2026-11-25-21-00-00-000"
    _put(session / "M 31_0001.fits", 100)
    _put(session / "M 31_0002.fits", 100)
    _put(session / "failed_M 31_0003.fits", 40)
    _put(session / "stacked.png", 7)
    _put(tmp_path / "Astronomy" / "DWARF_DARK" / "tele" / "raw_0000.fits", 30)
    _put(tmp_path / "Astronomy" / "CALI_FRAME" / "dark" / "cam_0" / "d.fits", 20)
    _put(tmp_path / "astrolab" / "images" / "M 31" / "M 31 v1.png", 9)
    _put(tmp_path / "astrolab" / "db" / "astrolab-2026-11-26.sqlite", 5)
    _put(tmp_path / "Panoramas" / "p.jpg", 3)
    _put(tmp_path / ".DS_Store", 1)

    got = {k["id"]: (k["bytes"], k["files"]) for k in measure(tmp_path)["kinds"]}
    assert got == {
        "lights": (200, 2),
        "failed": (40, 1),
        "scope_outputs": (7, 1),
        "calibration": (50, 2),
        "images": (9, 1),
        "db": (5, 1),
        "other": (4, 2),
    }


def test_captures_at_the_share_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ASTROLAB_NAS_CAPTURES_DIR", "")
    monkeypatch.setenv("ASTROLAB_NAS_APP_DIR", "astrolab")
    _put(tmp_path / "DWARF_RAW_x" / "a.fits", 10)
    _put(tmp_path / "astrolab" / "db" / "s.sqlite", 2)
    got = {k["id"]: k["bytes"] for k in measure(tmp_path)["kinds"]}
    assert got["lights"] == 10 and got["db"] == 2 and got["other"] == 0


def test_run_keeps_the_result_and_reports_it(tmp_path: Path) -> None:
    _put(tmp_path / "f.bin", 4)
    saved: list[dict] = []
    usage = NasUsage(lambda: tmp_path, lambda: saved[-1] if saved else None, saved.append)
    assert usage.snapshot()["last"] is None
    usage.run()
    snap = usage.snapshot()
    assert snap["configured"] and not snap["measuring"]
    assert snap["last"]["disk"]["total_bytes"] > 0


def test_no_nas_configured() -> None:
    usage = NasUsage(lambda: None, lambda: None, lambda _v: None)
    assert usage.snapshot() == {"configured": False, "measuring": False, "error": None, "last": None}
    assert usage.refresh() is False
