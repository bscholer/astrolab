"""The local Gaia catalogue: install (download, verify, unpack, link), cancel, remove."""

from __future__ import annotations

import bz2
import hashlib
import io
import time
from pathlib import Path

import pytest

from server import gaia as gaia_mod

PAYLOAD = b"gaia" * 50_000


class _Resp(io.BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def cat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASTROLAB_SIRIL_CATALOGS", str(tmp_path / "cats"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(gaia_mod, "DOWNLOAD_BYTES", 10)
    monkeypatch.setattr(gaia_mod, "DISK_BYTES", 10)
    return gaia_mod.GaiaCatalog()


def _serve(monkeypatch: pytest.MonkeyPatch, sha: str | None = None) -> None:
    blob = bz2.compress(PAYLOAD)
    want = sha or hashlib.sha256(blob).hexdigest()

    def fake(url, timeout=0):
        return _Resp(f"{want}  x\n".encode() if "sha256sum" in url else blob)

    monkeypatch.setattr(gaia_mod.urllib.request, "urlopen", fake)


def _wait(cat) -> None:
    for _ in range(200):
        if not cat.installing:
            return
        time.sleep(0.02)
    raise AssertionError("install did not finish")


def test_install_unpacks_links_and_reports_size(cat, monkeypatch) -> None:
    _serve(monkeypatch)
    assert cat.status()["installed"] is False
    assert cat.start_install() is True
    _wait(cat)
    st = cat.status()
    assert st["error"] is None
    assert st["installed"] and st["linked"]
    assert st["size_bytes"] == len(PAYLOAD)
    assert gaia_mod.siril_link().read_bytes() == PAYLOAD
    assert not list(gaia_mod.catalog_dir().glob("*.part"))


def test_bad_checksum_installs_nothing(cat, monkeypatch) -> None:
    _serve(monkeypatch, sha="0" * 64)
    cat.start_install()
    _wait(cat)
    st = cat.status()
    assert "checksum" in st["error"]
    assert not st["installed"]
    assert not list(gaia_mod.catalog_dir().glob("*.part*"))


def test_remove_deletes_file_and_link(cat, monkeypatch) -> None:
    _serve(monkeypatch)
    cat.start_install()
    _wait(cat)
    assert cat.remove() is True
    st = cat.status()
    assert not st["installed"] and not st["linked"]


def test_ensure_link_repairs_a_missing_link(cat, monkeypatch) -> None:
    _serve(monkeypatch)
    cat.start_install()
    _wait(cat)
    gaia_mod.siril_link().unlink()
    assert gaia_mod.ensure_link() is True
    assert gaia_mod.siril_link().is_symlink()


def test_ensure_link_without_catalogue_is_false(cat) -> None:
    assert gaia_mod.ensure_link() is False
