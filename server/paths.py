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
    path = astrolab_home() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path
