"""Scope profile loader.

A scope profile captures the pipeline-side knowledge specific to one
smart-telescope model: whether its lights are pre-calibrated, default
parameter overrides for individual nodes, and similar quirks. Profiles
live as YAML under ``profiles/<scope_id>.yaml`` and are loaded once at
import.

The profile system is intentionally small. Most cross-scope variation
that exists in tools like Naztronomy's Smart-Telescope-PP script falls
into one of two buckets:

- **Calibration policy**: Seestar subtracts darks and flats on-device, so
  the matcher must not waste time looking for masters and the UI must not
  flag the absence as a problem. This is the only behavior that ships
  wrong without a profile.

- **Per-node overrides**: scope-specific Siril flags that affect a single
  node's output (e.g. forcing ``-32b`` on stack to avoid Dwarf 3's "milky"
  output). Most of these don't apply because astrolab's defaults already
  cover them; the rest live as `node_defaults` entries.

Other things that look scope-specific but live elsewhere:

- **OSC filter color-calibration flags** (``-oscfilter=UV/IR Block`` and
  the ``-narrowband -rwl=...`` wavelength list) are Siril SPCC inputs.
  astrolab doesn't run SPCC yet; the profile entries would be unused.
  When SPCC lands, the wavelength lists will move into the profile.

- **Sensor / focal-length / pixel-size fallback** matters for capture
  programs that omit those keys (per-Unistellar-firmware tweaks in Naz's
  script). All four scopes astrolab supports today
  (Dwarf 3 / Seestar / NINA / ASIAIR) write those headers reliably, so
  no profile entries are needed.

If a profile file is missing for a detected scope_id, the system uses an
empty default profile: matcher runs normally, no node defaults applied.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger("astrolab.profiles")


def _profiles_dir() -> Path:
    """Where on disk we read scope profiles from."""
    return Path(__file__).resolve().parent.parent / "profiles"


class CalibrationPolicy(BaseModel):
    """How the matcher should treat sessions from this scope."""

    model_config = ConfigDict(extra="forbid")

    skip_match: bool = Field(
        default=False,
        description=(
            "If true, the matcher does not look for master darks / flats / "
            "biases. The session's calibration entries are tagged "
            "match_quality='not_needed' with the supplied reason. Used for "
            "scopes that subtract calibration frames on-device before "
            "exporting lights (e.g. Seestar)."
        ),
    )
    skip_reason: str | None = Field(
        default=None,
        description=(
            "Free-text explanation surfaced to the UI when skip_match is "
            "true. Should answer 'why is there no calibration?' in one "
            "sentence."
        ),
    )


class ScopeProfile(BaseModel):
    """Static metadata + behavior knobs for one scope."""

    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    calibration: CalibrationPolicy = Field(default_factory=CalibrationPolicy)
    node_defaults: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description=(
            "Per-node parameter overrides, applied as defaults when a Job "
            "is built from a session under this scope. Keyed by template "
            "node id; values are partial param dicts merged onto the "
            "template defaults but below user overrides."
        ),
    )
    notes: str | None = Field(
        default=None,
        description=(
            "Free-text human notes about quirks, validation status, or "
            "known-limitations. Not consumed by code; surfaced in the UI "
            "next to the scope display name."
        ),
    )


_DEFAULT_PROFILE = ScopeProfile(id="_default", display_name="(unknown scope)")


@lru_cache(maxsize=1)
def _load_all() -> dict[str, ScopeProfile]:
    """Read every ``profiles/*.yaml`` and return ``{scope_id: profile}``.

    Cached for the lifetime of the process. Profiles are part of the
    source tree, not user-mutable state, so a hot reload isn't worth the
    invalidation complexity. A test or dev tool that needs to bust the
    cache can call ``reload()``.
    """
    profiles: dict[str, ScopeProfile] = {}
    d = _profiles_dir()
    if not d.exists():
        return profiles
    for path in sorted(d.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            log.warning("scope profile %s is not valid YAML: %s", path, exc)
            continue
        if not isinstance(data, dict):
            log.warning("scope profile %s did not parse to a dict", path)
            continue
        try:
            profile = ScopeProfile.model_validate(data)
        except Exception as exc:  # noqa: BLE001
            log.warning("scope profile %s failed validation: %s", path, exc)
            continue
        if profile.id in profiles:
            log.warning(
                "scope profile id collision: %s (from %s) overwrites earlier",
                profile.id, path,
            )
        profiles[profile.id] = profile
    return profiles


def get(scope_id: str | None) -> ScopeProfile:
    """Return the profile for ``scope_id``, or an empty default if missing.

    Callers that want to know "did we find one?" should compare to
    ``ScopeProfile`` defaults explicitly; most code just wants to read
    ``profile.calibration.skip_match`` without a None check.
    """
    if scope_id is None:
        return _DEFAULT_PROFILE
    return _load_all().get(scope_id, _DEFAULT_PROFILE)


def all_profiles() -> dict[str, ScopeProfile]:
    """Return a copy of every loaded profile, keyed by scope_id."""
    return dict(_load_all())


def reload() -> None:
    """Discard the cached profile set; next ``get()`` re-reads from disk.

    Useful for tests that write profile files into a tmp_path and want
    the loader to pick them up.
    """
    _load_all.cache_clear()
