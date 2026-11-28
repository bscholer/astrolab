"""The local Gaia star catalogue Siril plate solves against.

Without it Siril asks an online service for the stars around every frame, which can
take minutes a frame (a duo-band night of 900 frames then takes a day). With it a solve
takes under a second. The catalogue is Siril's healpix8 Gaia DR3 astrometry file from
Zenodo record 14692304: 1.1 GB to download, about 3 GB on disk.

It lives in a folder on the cache volume (ASTROLAB_SIRIL_CATALOGS, by default
siril-catalogs/ under the cache root) and is linked to the path Siril reads,
~/.local/share/siril/gaia_astrometric.dat. The container entrypoint does the same link
at start; ensure_link() does it again at API start and after an install, so no restart
is needed: each Siril run reads the catalogue as it starts.
"""

from __future__ import annotations

import bz2
import hashlib
import logging
import os
import shutil
import threading
import urllib.request
from pathlib import Path
from typing import Any

from server.paths import cache_root

log = logging.getLogger(__name__)

RECORD = "14692304"
FILE = "siril_cat_healpix8_astro.dat"
BASE_URL = f"https://zenodo.org/api/records/{RECORD}/files"
DOWNLOAD_BYTES = 1_142_276_749
"""Size of the .bz2 on Zenodo, for the "before you install" figure."""
DISK_BYTES = 3_100_000_000
"""About what the unpacked file takes; the real size shows once it is installed."""

CHUNK = 1 << 20


def catalog_dir() -> Path:
    override = os.environ.get("ASTROLAB_SIRIL_CATALOGS", "").strip()
    return Path(override) if override else cache_root() / "siril-catalogs"


def catalog_file() -> Path:
    return catalog_dir() / FILE


def siril_link() -> Path:
    return Path.home() / ".local" / "share" / "siril" / "gaia_astrometric.dat"


def ensure_link() -> bool:
    """Point Siril's catalogue path at the installed file. False when none is installed."""
    src, link = catalog_file(), siril_link()
    if not src.is_file():
        return False
    if link.is_symlink() and link.resolve() == src.resolve():
        return True
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(src)
    except OSError:
        log.exception("could not link the Gaia catalogue at %s", link)
        return False
    return True


class Cancelled(Exception):
    pass


class GaiaCatalog:
    """Status, install and removal of the catalogue. One install runs at a time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._phase: str | None = None  # downloading | unpacking | None
        self._done = 0
        self._total = 0
        self._error: str | None = None

    def status(self) -> dict[str, Any]:
        f = catalog_file()
        installed = f.is_file()
        d = catalog_dir()
        probe = d if d.exists() else d.parent
        try:
            free = shutil.disk_usage(probe if probe.exists() else Path("/")).free
        except OSError:
            free = None
        link = siril_link()
        with self._lock:
            install = (
                {"phase": self._phase, "done_bytes": self._done, "total_bytes": self._total}
                if self._phase
                else None
            )
            error = self._error
        return {
            "installed": installed,
            "path": str(f),
            "size_bytes": f.stat().st_size if installed else 0,
            "linked": link.is_symlink() and link.exists(),
            "download_bytes": DOWNLOAD_BYTES,
            "disk_bytes": DISK_BYTES,
            "free_bytes": free,
            "source": f"https://zenodo.org/records/{RECORD}",
            "install": install,
            "error": error,
        }

    @property
    def installing(self) -> bool:
        with self._lock:
            return self._phase is not None

    def start_install(self) -> bool:
        """Begin the download in a background thread. False if one is already running."""
        with self._lock:
            if self._phase is not None:
                return False
            self._phase = "downloading"
            self._done = 0
            self._total = DOWNLOAD_BYTES
            self._error = None
            self._cancel.clear()
        self._thread = threading.Thread(target=self._run, name="gaia-install", daemon=True)
        self._thread.start()
        return True

    def cancel(self) -> bool:
        if not self.installing:
            return False
        self._cancel.set()
        return True

    def remove(self) -> bool:
        if self.installing:
            return False
        link = siril_link()
        if link.is_symlink():
            link.unlink()
        f = catalog_file()
        had = f.exists()
        f.unlink(missing_ok=True)
        return had

    # -- install ----------------------------------------------------------------

    def _set(self, phase: str, done: int, total: int) -> None:
        with self._lock:
            self._phase, self._done, self._total = phase, done, total

    def _run(self) -> None:
        d = catalog_dir()
        part_bz2 = d / f"{FILE}.bz2.part"
        part_dat = d / f"{FILE}.part"
        try:
            d.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(d).free
            if free < DOWNLOAD_BYTES + DISK_BYTES + (1 << 30):
                raise RuntimeError(
                    f"not enough room: needs about {(DOWNLOAD_BYTES + DISK_BYTES) // 10**9} GB "
                    f"free, {free // 10**9} GB is"
                )
            want = self._fetch_sha()
            self._download(part_bz2, want)
            self._unpack(part_bz2, part_dat)
            part_dat.replace(catalog_file())
            ensure_link()
            log.info("local Gaia catalogue installed at %s", catalog_file())
        except Cancelled:
            log.info("local Gaia install cancelled")
        except Exception as e:
            log.exception("local Gaia install failed")
            with self._lock:
                self._error = str(e) or type(e).__name__
        finally:
            part_bz2.unlink(missing_ok=True)
            part_dat.unlink(missing_ok=True)
            with self._lock:
                self._phase = None

    def _fetch_sha(self) -> str:
        url = f"{BASE_URL}/{FILE}.bz2.sha256sum/content"
        with urllib.request.urlopen(url, timeout=60) as r:
            return r.read().decode().split()[0].strip().lower()

    def _download(self, dest: Path, want_sha: str) -> None:
        url = f"{BASE_URL}/{FILE}.bz2/content"
        sha = hashlib.sha256()
        done = 0
        with urllib.request.urlopen(url, timeout=60) as r, open(dest, "wb") as out:
            total = int(r.headers.get("Content-Length") or DOWNLOAD_BYTES)
            self._set("downloading", 0, total)
            while chunk := r.read(CHUNK):
                if self._cancel.is_set():
                    raise Cancelled
                out.write(chunk)
                sha.update(chunk)
                done += len(chunk)
                self._set("downloading", done, total)
        if sha.hexdigest() != want_sha:
            raise RuntimeError("the download is damaged (checksum does not match); try again")

    def _unpack(self, src: Path, dest: Path) -> None:
        total = src.stat().st_size
        self._set("unpacking", 0, total)
        dec = bz2.BZ2Decompressor()
        read = 0
        with open(src, "rb") as f, open(dest, "wb") as out:
            while chunk := f.read(CHUNK):
                if self._cancel.is_set():
                    raise Cancelled
                out.write(dec.decompress(chunk))
                read += len(chunk)
                self._set("unpacking", read, total)


gaia = GaiaCatalog()
