"""Catalog row models.

These mirror catalog table columns. They are not for the wire (no FastAPI yet)
and not for the cache hashing path; they're just convenient typed views over
sqlite3.Row dicts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

ImageType = Literal["LIGHT", "DARK", "FLAT", "BIAS"]
"""Frame types we catalog as raw subs. Pre-built calibration masters live in
the masters table, not here."""

Quality = Literal["ok", "failed"]
"""Sub-quality flag. 'failed' means the scope's own software rejected the sub
(e.g. Dwarf 3 prefixes failed_). Failed subs are still catalogued so a user
can override the rejection later."""

MasterKind = Literal["dark", "flat", "bias"]

MasterSource = Literal["factory", "user", "astrolab"]
"""'factory' = bundled with the scope; 'user' = stacked on the scope by the
user; 'astrolab' = built by our own master-build job (deferred)."""

MatchQuality = Literal["exact", "approx", "none"]


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    name: str
    aliases: list[str] | None = None
    ra: float | None = None
    dec: float | None = None


class Frame(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    file_hash: str | None = None
    path: str
    inode: int | None = None
    mtime: float | None = None
    size: int | None = None

    image_type: ImageType
    quality: Quality = "ok"
    object: str | None = None
    instrument: str | None = None
    camera: str | None = None
    filter: str | None = None
    exptime: float | None = None
    gain: int | None = None
    binning: int | None = None
    ccd_temp: float | None = None
    date_obs: str | None = None
    ra: float | None = None
    dec: float | None = None

    scope_id: str
    fits_headers: dict | None = None
    scanned_at: float | None = None


class Session(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    scope_id: str
    target_id: int | None = None
    instrument: str | None = None
    camera: str | None = None
    filter: str | None = None
    exptime: float | None = None
    gain: int | None = None
    binning: int | None = None
    started_at: str | None = None
    ended_at: str | None = None
    frame_count: int = 0
    failed_count: int = 0
    notes: str | None = None


class Master(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    kind: MasterKind
    scope_id: str | None = None
    source: MasterSource = "factory"
    instrument: str | None = None
    camera: str | None = None
    filter: str | None = None
    exptime: float | None = None
    gain: int | None = None
    binning: int | None = None
    ccd_temp: float | None = None
    stack_count: int | None = None
    file_hash: str | None = None
    path: str
    inode: int | None = None
    mtime: float | None = None
    size: int | None = None
    date_built: str | None = None
    cache_ref: str | None = None
    source_frame_ids: list[int] | None = None
    scanned_at: float | None = None


class DiscoveredMaster(BaseModel):
    """A scope-specific helper's classification of a pre-built calibration master.

    Used for masters the scope ships with (currently only Dwarf 3 factory
    masters under ``CALI_FRAME/``). DAG-built masters bypass this path;
    they're inserted directly by the master-build job.
    """

    model_config = ConfigDict(extra="forbid")

    path: Path
    kind: MasterKind
    source: MasterSource = "factory"
    camera: str | None = None
    instrument: str | None = None
    filter: str | None = None
    exptime: float | None = None
    gain: int | None = None
    binning: int | None = None
    ccd_temp: float | None = None
    stack_count: int | None = None


class CalibrationMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: int
    kind: MasterKind
    master_id: int | None = None
    match_quality: MatchQuality
    details: dict | None = None
    overridden: bool = False
    updated_at: float | None = None
