"""OpenNGC-backed deep-sky catalog.

Reads `catalogs/openngc/{NGC,addendum}.csv` once on first call and keeps
the parsed rows + alias index in process memory. Single source of truth
— there is no derived JSON / cache file. Restart the server to refresh.

Public surface is small on purpose:

  enrich("M 33") -> CatalogEntry | None

`CatalogEntry` carries the fields the UI cares about (common name, sky
position in decimal degrees, magnitude, constellation, expanded object
type). Rows that don't match anything return None and the caller falls
through to the curated `common_names.json` table.

The catalog is licensed CC-BY-SA-4.0; see `catalogs/openngc/SOURCE.md`.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from functools import cache
from pathlib import Path

# Repo root is two levels up from this file (server/catalog/openngc.py).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_NGC_PATH = _REPO_ROOT / "catalogs" / "openngc" / "NGC.csv"
_ADDENDUM_PATH = _REPO_ROOT / "catalogs" / "openngc" / "addendum.csv"


@dataclass(frozen=True)
class CatalogEntry:
    """One parsed catalog row, surfaced to the API layer."""

    canonical: str          # 'NGC 7380', 'IC 1', 'M 33', 'B 33', 'C 9'
    common_name: str | None # 'Wizard Nebula' / 'Andromeda Galaxy' / None
    object_type: str | None # 'Galaxy', 'Emission Nebula', …
    ra_deg: float | None    # decimal degrees, J2000
    dec_deg: float | None
    magnitude: float | None # V-Mag preferred; falls back to B-Mag
    constellation: str | None  # IAU 3-letter abbreviation


# OpenNGC type code -> human-readable label. Keep this list aligned with
# catalogs/openngc/SOURCE.md so the documentation stays accurate.
_TYPE_LABELS: dict[str, str] = {
    "G": "Galaxy",
    "GPair": "Galaxy Pair",
    "GTrpl": "Galaxy Triplet",
    "GGroup": "Galaxy Group",
    "OC": "Open Cluster",
    "GCl": "Globular Cluster",
    "Cl+N": "Cluster with Nebula",
    "HII": "H II Region",
    "EmN": "Emission Nebula",
    "RfN": "Reflection Nebula",
    "DrkN": "Dark Nebula",
    "PN": "Planetary Nebula",
    "SNR": "Supernova Remnant",
    "Neb": "Nebula",
    "*Ass": "Stellar Association",
    "AsterismCl": "Asterism",
    "Star": "Star",
    "*": "Star",
    "**": "Double Star",
    "Dup": "Duplicate Entry",
    "NonEx": "Non-existent",
}


def _parse_ra(s: str) -> float | None:
    """Hours:minutes:seconds (sexagesimal) -> decimal degrees.

    Returns None if the field is empty or malformed; callers treat that
    as 'unknown sky position'.
    """
    s = s.strip()
    if not s:
        return None
    try:
        h, m, sec = s.split(":")
        return (float(h) + float(m) / 60 + float(sec) / 3600) * 15.0
    except (ValueError, IndexError):
        return None


def _parse_dec(s: str) -> float | None:
    """Signed degrees:minutes:seconds (sexagesimal) -> decimal degrees."""
    s = s.strip()
    if not s:
        return None
    try:
        sign = -1.0 if s.startswith("-") else 1.0
        body = s.lstrip("+-")
        d, m, sec = body.split(":")
        return sign * (float(d) + float(m) / 60 + float(sec) / 3600)
    except (ValueError, IndexError):
        return None


def _strip_zeros(prefix: str, raw: str) -> str:
    """'NGC0007' -> 'NGC 7'. The OpenNGC `Name` column zero-pads to 4
    digits; we want the human form for our canonical id and our index
    keys. Suffix letters (e.g. 'NGC4438A') are preserved.
    """
    digits = raw[len(prefix):]
    # Capture trailing letters after the digit run for objects like NGC4438A.
    i = 0
    while i < len(digits) and digits[i].isdigit():
        i += 1
    num = digits[:i].lstrip("0") or "0"
    suffix = digits[i:]
    return f"{prefix} {num}{suffix}"


def _canonical_from_name(raw: str) -> str:
    """Convert the OpenNGC `Name` column value to its canonical id form.
    Returns '' for unknown formats so the caller can skip the row."""
    raw = raw.strip()
    for prefix in ("NGC", "IC"):
        if raw.startswith(prefix):
            return _strip_zeros(prefix, raw)
    # Addendum entries: B033 (Barnard), C009 (Caldwell), Sh2-155 already-spaced…
    for prefix in ("B", "C"):
        if raw.startswith(prefix) and len(raw) > 1 and raw[1].isdigit():
            return _strip_zeros(prefix, raw)
    return raw


def _alias_keys_for(entry_name: str, m: str, ngc: str, ic: str, identifiers: str) -> list[str]:
    """Every key under which we want this row to be findable.

    The user (and FITS headers) might write 'M 33', 'M33', 'NGC 0598',
    'NGC 598', 'IC 0001', 'Sh2-155'… all should hit the same row.
    """
    keys: set[str] = {entry_name}
    # Drop any space variants of the canonical id.
    if " " in entry_name:
        keys.add(entry_name.replace(" ", ""))
    # Messier cross-reference.
    if m:
        m = m.strip()
        keys.add(f"M {m}")
        keys.add(f"M{m}")
    # NGC / IC cross-references (only meaningful when this row's name
    # isn't already that catalog).
    if ngc and not entry_name.startswith("NGC"):
        keys.add(f"NGC {ngc.strip()}")
        keys.add(f"NGC{ngc.strip()}")
    if ic and not entry_name.startswith("IC"):
        keys.add(f"IC {ic.strip()}")
        keys.add(f"IC{ic.strip()}")
    # Foreign identifiers (Sh2-155, LBN 529, …). We only index the ones
    # that look like 'PREFIX number' so we don't pollute the index with
    # SDSS J… names that nobody types by hand.
    for raw in (identifiers or "").split(","):
        token = raw.strip()
        if not token:
            continue
        if any(token.upper().startswith(p) for p in ("SH ", "SH2-", "LBN ", "LDN ", "C ", "B ", "MEL ", "VDB ", "RCW ", "ABELL ")):
            keys.add(token)
            keys.add(token.replace(" ", ""))
    return list(keys)


def _pick_magnitude(v: str, b: str) -> float | None:
    """Prefer V-Mag (closer to perceived brightness), fall back to B-Mag.
    OpenNGC stores empty strings for unknown values."""
    for raw in (v, b):
        raw = raw.strip()
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
    return None


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh, delimiter=";")
        return list(reader)


@cache
def _load() -> tuple[list[CatalogEntry], dict[str, CatalogEntry]]:
    """Parse both CSVs and build the alias index.

    Cached for the life of the process. The time cost is a few hundred
    ms on cold start; subsequent calls are O(1).
    """
    rows = _read_csv(_NGC_PATH) + _read_csv(_ADDENDUM_PATH)
    entries: list[CatalogEntry] = []
    index: dict[str, CatalogEntry] = {}

    for r in rows:
        canonical = _canonical_from_name(r.get("Name", ""))
        if not canonical:
            continue
        # First common name wins for display; the rest become aliases.
        common_raw = (r.get("Common names") or "").strip()
        common_list = [c.strip() for c in common_raw.split(",") if c.strip()]
        common_name = common_list[0] if common_list else None

        type_code = (r.get("Type") or "").strip()
        entry = CatalogEntry(
            canonical=canonical,
            common_name=common_name,
            object_type=_TYPE_LABELS.get(type_code, type_code or None),
            ra_deg=_parse_ra(r.get("RA", "")),
            dec_deg=_parse_dec(r.get("Dec", "")),
            magnitude=_pick_magnitude(r.get("V-Mag", ""), r.get("B-Mag", "")),
            constellation=(r.get("Const") or "").strip() or None,
        )
        entries.append(entry)

        # Build alias keys: canonical id, no-space variant, Messier xref,
        # NGC/IC xrefs, common names, and selected foreign identifiers.
        for key in _alias_keys_for(
            canonical,
            (r.get("M") or "").strip(),
            (r.get("NGC") or "").strip(),
            (r.get("IC") or "").strip(),
            r.get("Identifiers") or "",
        ):
            # Earlier entries win — Messier numbers tend to show up first
            # in NGC.csv and are the canonical resolution.
            index.setdefault(_normalize(key), entry)
        for c in common_list:
            index.setdefault(_normalize(c), entry)
    return entries, index


def _normalize(s: str) -> str:
    """Lowercase, collapse whitespace runs, trim. Used for both index
    keys and lookup probes so 'M  33' and 'm 33' both resolve."""
    return " ".join(s.lower().split())


def enrich(name: str | None) -> CatalogEntry | None:
    """Return catalog data for `name`, or None if no match.

    Tries the literal canonicalized form first, then a no-space variant
    (so 'NGC7380' resolves to the 'NGC 7380' row).
    """
    if not name:
        return None
    _, index = _load()
    key = _normalize(name)
    hit = index.get(key)
    if hit is not None:
        return hit
    # Compact form fallback: strip all spaces and try again.
    return index.get(key.replace(" ", ""))


def stats() -> dict[str, int]:
    """Tiny diagnostic — handy on a /api/health probe to confirm the
    catalog is loaded and how many rows are indexed."""
    entries, index = _load()
    named = sum(1 for e in entries if e.common_name)
    return {
        "entries": len(entries),
        "alias_keys": len(index),
        "named": named,
    }
