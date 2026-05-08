"""SirilRuntime: binary discovery and .ssf composition.

These tests run on any platform; they don't actually invoke Siril.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from server.siril import (
    MIN_VERSION,
    SirilBinary,
    SirilNotFound,
    SirilRuntime,
    _parse_version,
    find_siril,
)


def test_parse_version_basic() -> None:
    assert _parse_version("siril 1.4.3") == (1, 4, 3)
    assert _parse_version("1.4.0-beta4") == (1, 4, 0)
    assert _parse_version("1.4") == (1, 4, 0)
    assert _parse_version("not a version") is None


def test_min_version_is_at_least_1_4() -> None:
    assert MIN_VERSION >= (1, 4)


def test_compose_ssf_injects_requires() -> None:
    text = SirilRuntime._compose_ssf(
        ["cd /tmp", "convert light -out=../process"], require_version="1.4.0"
    )
    lines = text.strip().split("\n")
    assert lines[0] == "requires 1.4.0"
    assert "convert light -out=../process" in lines


def test_compose_ssf_respects_existing_requires() -> None:
    text = SirilRuntime._compose_ssf(
        ["requires 1.4.1", "stack r_light_ rej 3 3"], require_version="1.4.0"
    )
    # Should NOT add a second requires line.
    assert text.count("requires ") == 1
    assert "requires 1.4.1" in text


def test_find_siril_honors_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = tmp_path / "fake-siril"
    fake.touch()
    monkeypatch.setenv("SIRIL_BIN", str(fake))
    binary = find_siril()
    assert binary.path == fake
    assert binary.source == "env"


def test_find_siril_env_override_missing_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIRIL_BIN", "/definitely/not/a/real/siril")
    with pytest.raises(SirilNotFound):
        find_siril()


def test_find_siril_appimage_picked_up(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Place a fake AppImage in a fake $HOME/Downloads and verify discovery."""
    monkeypatch.delenv("SIRIL_BIN", raising=False)
    fake_home = tmp_path / "home"
    (fake_home / "Downloads").mkdir(parents=True)
    appimage = fake_home / "Downloads" / "Siril-1.4.3-x86_64.AppImage"
    appimage.touch()
    monkeypatch.setenv("HOME", str(fake_home))
    # Also clear PATH so a real system siril is not discovered.
    monkeypatch.setenv("PATH", "")
    binary = find_siril()
    assert binary.path == appimage
    assert binary.source == "appimage"
    assert binary.version is not None and binary.version[:2] == (1, 4)


def test_find_siril_prefers_extracted_appdir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An extracted AppDir wins over a packed AppImage of the same version,
    since FUSE-based AppImages frequently fail on locked-down hosts."""
    monkeypatch.delenv("SIRIL_BIN", raising=False)
    fake_home = tmp_path / "home"
    (fake_home / "Downloads").mkdir(parents=True)
    (fake_home / "Applications").mkdir(parents=True)
    (fake_home / "Downloads" / "Siril-1.4.3-x86_64.AppImage").touch()
    appdir = fake_home / "Applications" / "Siril-1.4.3.AppDir"
    appdir.mkdir()
    apprun = appdir / "AppRun"
    apprun.touch()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("PATH", "")

    binary = find_siril()
    assert binary.path == apprun
    assert binary.source == "appdir"
    assert binary.args_prefix == ("siril-cli",)


def test_run_invokes_appdir_with_siril_cli_prefix(tmp_path: Path) -> None:
    """When the binary carries an args_prefix, it is inserted before -s."""
    fake = tmp_path / "fake-apprun.sh"
    fake.write_text(
        "#!/bin/sh\n"
        'echo "[fake] argv=$*"\n'
        "exit 0\n"
    )
    fake.chmod(0o755)

    rt = SirilRuntime(
        binary=SirilBinary(path=fake, source="appdir", args_prefix=("siril-cli",))
    )
    res = rt.run(["save dummy"], working_dir=tmp_path)
    assert res.returncode == 0
    # Fake shell prints what argv it got; siril-cli should appear before -s.
    assert "siril-cli -s" in res.stdout


def test_find_siril_skips_old_appimages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("SIRIL_BIN", raising=False)
    fake_home = tmp_path / "home"
    (fake_home / "Downloads").mkdir(parents=True)
    (fake_home / "Downloads" / "Siril-1.2.1-x86_64.AppImage").touch()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("PATH", "")
    with pytest.raises(SirilNotFound):
        find_siril()


def test_runtime_init_uses_passed_binary(tmp_path: Path) -> None:
    fake = tmp_path / "fake-siril"
    fake.touch()
    rt = SirilRuntime(binary=SirilBinary(path=fake, source="env"))
    assert rt.binary.path == fake


def test_run_invokes_subprocess(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """End-to-end run() against a fake Siril that just echos the .ssf path."""
    # Build a fake siril script that records the args we got and writes a marker.
    fake = tmp_path / "fake-siril.sh"
    fake.write_text(
        "#!/bin/sh\n"
        'echo "[fake] invoked with $@" \n'
        'echo "[fake] reading ssf:"\n'
        'cat "$2"\n'
        "exit 0\n"
    )
    fake.chmod(0o755)

    rt = SirilRuntime(binary=SirilBinary(path=fake, source="env"))
    captured: list[str] = []
    result = rt.run(
        ["cd /tmp", "convert light -out=../process"],
        working_dir=tmp_path,
        on_log=captured.append,
    )
    assert result.returncode == 0
    assert "convert light -out=../process" in result.stdout
    assert "convert light -out=../process" in result.ssf
    assert any("invoked with -s" in line for line in captured)


def test_run_propagates_nonzero_exit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = tmp_path / "fake-bad-siril.sh"
    fake.write_text("#!/bin/sh\necho 'boom'\nexit 7\n")
    fake.chmod(0o755)

    rt = SirilRuntime(binary=SirilBinary(path=fake, source="env"))
    res = rt.run(["save dummy"], working_dir=tmp_path)
    assert res.returncode == 7


@pytest.mark.skipif("CI" in os.environ, reason="don't run actual siril in CI")
def test_run_real_siril_smoke() -> None:
    """If a real Siril binary is discoverable, do a one-line dry run.

    Skipped silently when no Siril is found; on a dev box (Linux with
    AppImage in ~/Downloads) this exercises the real subprocess path.
    """
    try:
        binary = find_siril()
    except SirilNotFound:
        pytest.skip("no siril found on this machine")
    rt = SirilRuntime(binary=binary)
    # 'requires' alone exits cleanly when version satisfied.
    res = rt.run([], require_version="1.4.0")
    assert res.returncode == 0, res.stderr
