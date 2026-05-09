"""Resolve runtime data paths.

Per README, runtime state lives outside the repo. By default we put it under
~/Library/Application Support/astrolab on macOS and $XDG_DATA_HOME/astrolab on
Linux, overridable by the ASTROLAB_HOME env var.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def astrolab_home() -> Path:
    """Return the active astrolab runtime directory, creating it if needed."""
    override = os.environ.get("ASTROLAB_HOME")
    if override:
        path = Path(override).expanduser().resolve()
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "astrolab"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
        path = base / "astrolab"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_root() -> Path:
    """Return the active cache directory, creating it if needed.

    Resolution order: a DB-stored override (set via the Settings UI) wins,
    so the user can move the cache off /scratch onto a different partition
    without touching env vars or systemd units; otherwise we fall back to
    `astrolab_home() / "cache"`. The override is read once per call and is
    cheap (a single sqlite read), so callers that already cache the
    ContentCache root won't see a hot path change.
    """
    override = _read_cache_root_override()
    if override is not None:
        override.mkdir(parents=True, exist_ok=True)
        return override
    path = astrolab_home() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_cache_root_override() -> Path | None:
    """Look up the cache_root setting in the system DB, if it exists.

    The DB itself lives under astrolab_home() and is independent of where
    the cache sits, so this is safe to call even before the cache has been
    initialized. Returns None on any failure (DB missing, unreadable, the
    setting unset) — callers fall back to the default location.
    """
    db_path = astrolab_home() / "astrolab.sqlite"
    if not db_path.exists():
        return None
    try:
        # Local import keeps paths.py free of the heavier storage import
        # chain at module load time (storage.py pulls cache, models, etc.).
        from .storage import SETTING_CACHE_ROOT_OVERRIDE, get_setting

        raw = get_setting(SETTING_CACHE_ROOT_OVERRIDE, None, db_path=db_path)
    except Exception:
        return None
    if not raw:
        return None
    return Path(str(raw)).expanduser().resolve()
