"""Shared pytest fixtures."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def astrolab_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ASTROLAB_HOME at a fresh tmp dir for the duration of a test."""
    home = tmp_path / "astrolab-home"
    home.mkdir()
    monkeypatch.setenv("ASTROLAB_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _isolate_astrolab_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default-isolate ASTROLAB_HOME for every test, even ones that don't ask for it.

    Tests that need to inspect the home dir should still take the explicit
    fixture; this autouse fixture just guarantees no test ever writes into
    the user's real ~/Library/Application Support/astrolab.
    """
    if "ASTROLAB_HOME" not in os.environ or not os.environ["ASTROLAB_HOME"].startswith(
        str(tmp_path)
    ):
        default_home = tmp_path / "_default-astrolab-home"
        default_home.mkdir(exist_ok=True)
        monkeypatch.setenv("ASTROLAB_HOME", str(default_home))
