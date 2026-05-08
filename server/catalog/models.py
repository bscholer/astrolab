"""Catalog row models.

These mirror catalog table columns. They are not for the wire (no FastAPI yet)
and not for the cache hashing path; they're just convenient typed views over
sqlite3.Row dicts.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

ImageType = Literal["LIGHT", "DARK", "FLAT", "BIAS"]
"""Phase 1.a frame types. MASTER_DARK / MASTER_FLAT / MASTER_BIAS land later."""

Quality = Literal["ok", "failed"]
"""Sub-quality flag. 'failed' means the scope's own software rejected the sub
(e.g. Dwarf 3 prefixes failed_)."""


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
    session_key: str | None = None
    fits_headers: dict | None = None
    scanned_at: float | None = None


class Session(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    scope_id: str
    session_key: str
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
