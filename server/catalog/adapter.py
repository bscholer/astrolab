"""Ingest adapter contract.

An adapter knows how to walk a single scope's capture tree and classify what
it finds. Each adapter is responsible only for the path/filename layer of
metadata; FITS header reading lives in fits_reader and is orchestrated by the
scanner. This separation keeps adapters tiny and FITS-agnostic, which matters
because some scopes write non-FITS sidecars (PNG/TIFF previews) that should
be ignored.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from .models import ImageType, Quality


class DiscoveredFrame(BaseModel):
    """An adapter's path-level classification of a candidate frame.

    The scanner combines this with FITS header data to produce a Frame row.
    Fields here are the ones the adapter is *better placed* to know than the
    header is: e.g. image_type for scopes that don't set IMAGETYP, and
    sub-quality for scopes that flag rejects in the filename.
    """

    model_config = ConfigDict(extra="forbid")

    path: Path
    image_type: ImageType
    quality: Quality = "ok"
    session_key: str | None = None
    """Adapter-defined session grouping. Frames with the same key go in the
    same session row. None means "not part of a session" (e.g. lone darks)."""

    session_hints: dict | None = None
    """Free-form hints the adapter extracted from the path that the scanner
    can use when creating the session row (e.g. exposure, target name).
    Header data wins where both are present, but path hints fill gaps."""


class IngestAdapter(Protocol):
    """A scope-specific walker."""

    scope_id: str

    def discover(self, root: Path) -> Iterator[DiscoveredFrame]:
        """Yield candidate frames found beneath `root`.

        Implementations should be lazy (use os.walk / Path.iterdir) and skip
        directories that don't belong to the scope. They should never read
        FITS data; that's the scanner's job.
        """
        ...


_REGISTRY: dict[str, IngestAdapter] = {}


def register(adapter: IngestAdapter) -> IngestAdapter:
    """Register an adapter instance under its scope_id."""
    if adapter.scope_id in _REGISTRY:
        existing = _REGISTRY[adapter.scope_id]
        if existing is not adapter:
            raise RuntimeError(
                f"adapter registry collision for scope {adapter.scope_id!r}: "
                f"{type(existing).__name__} vs {type(adapter).__name__}"
            )
    _REGISTRY[adapter.scope_id] = adapter
    return adapter


def lookup(scope_id: str) -> IngestAdapter:
    try:
        return _REGISTRY[scope_id]
    except KeyError as exc:
        known = sorted(_REGISTRY.keys())
        raise KeyError(f"no adapter registered for scope {scope_id!r}; known: {known}") from exc


def all_scopes() -> list[str]:
    return sorted(_REGISTRY.keys())
