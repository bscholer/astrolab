"""Finishing defaults picked from what the catalog knows about a target.

A recipe is a set of parameter overrides for the stack_per_night template's
finishing nodes. Target masters start from one, and every value stays
editable on the project page.

The values come from the projects tuned by hand so far (C 20, C 34): GraXpert
background extraction and denoise on, and a background around 0.1-0.15. The
template's own default background of 0.05 renders faint targets nearly black.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

NEBULA_TYPES = {
    "H II Region",
    "Emission Nebula",
    "Supernova Remnant",
    "Nebula",
    "Cluster with Nebula",
    "Planetary Nebula",
    "Reflection Nebula",
    "Dark Nebula",
}
GALAXY_TYPES = {"Galaxy", "Galaxy Pair", "Galaxy Triplet", "Galaxy Group"}
DUO_BAND = {"HAOIII", "DUO", "DUAL"}


@dataclass(frozen=True)
class Recipe:
    id: str
    label: str
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)


def _kind(object_type: str | None) -> str:
    if object_type in NEBULA_TYPES:
        return "nebula"
    if object_type in GALAXY_TYPES:
        return "galaxy"
    return "stars"


def recipe_for(object_type: str | None, filter_name: str | None) -> Recipe:
    kind = _kind(object_type)
    duo = (filter_name or "").replace("-", "").replace(" ", "").upper() in DUO_BAND
    base: dict[str, dict[str, Any]] = {"graxpert_bg": {"enabled": True}}
    if kind == "nebula":
        base["graxpert_denoise"] = {"enabled": True}
        base["stretch"] = {"target_bg": 0.14, "shadows_clip": -3.5}
        if duo:
            # SCNR at full strength eats the teal of OIII on duo-band data.
            base["color_balance"] = {"amount": 0.7}
        return Recipe(
            "nebula-duo" if duo else "nebula",
            "Emission nebula, duo-band" if duo else "Nebula",
            base,
        )
    if kind == "galaxy":
        base["graxpert_denoise"] = {"enabled": True}
        base["stretch"] = {"target_bg": 0.1}
        return Recipe("galaxy", "Galaxy", base)
    base["stretch"] = {"target_bg": 0.1}
    return Recipe("stars", "Star field or cluster", base)
