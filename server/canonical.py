"""Canonical encoding and hashing for cache keys.

The cache invariant: equivalent (node_id, version, inputs, params) must hash to
the same value, and any meaningful difference must hash differently. This
module is the implementation of that invariant. Property tests in
tests/test_canonical.py pin the behavior; they are part of the contract.

Float canonicalization uses per-field precision declared on the Pydantic
params model via Field(json_schema_extra={"hash_precision": N}). Without an
explicit precision, floats round to DEFAULT_FLOAT_PRECISION places. This
matters because two GHS configs that look the same can differ by float noise
in the JSON representation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

DEFAULT_FLOAT_PRECISION: int = 6
"""Decimal places retained when a param has no explicit hash_precision."""

HASH_PRECISION_KEY: str = "hash_precision"
"""Key used inside Pydantic Field(json_schema_extra=...) to override precision."""


def _round_float(value: float, precision: int) -> float:
    """Round to `precision` decimals, normalizing -0.0 to 0.0 and NaN handling."""
    if value != value:  # NaN
        return float("nan")
    rounded = round(value, precision)
    if rounded == 0.0:
        return 0.0
    return rounded


def _field_precisions(params_model: type[BaseModel] | None) -> dict[str, int]:
    """Extract per-field hash_precision from a params model's Pydantic fields."""
    if params_model is None:
        return {}
    out: dict[str, int] = {}
    for name, field in params_model.model_fields.items():
        extra = field.json_schema_extra
        if isinstance(extra, dict):
            precision = extra.get(HASH_PRECISION_KEY)
            if isinstance(precision, int):
                out[name] = precision
    return out


def canonical_value(
    value: Any,
    *,
    field_name: str | None = None,
    precisions: Mapping[str, int] | None = None,
) -> Any:
    """Recursively normalize a JSON-serializable value into canonical form.

    Rules:
      - dicts: keys sorted; values canonicalized (precisions only apply at the
        top level, since that is where param fields live).
      - lists: canonicalized element-wise (order preserved; lists are ordered).
      - bools: kept as bools (must precede the int branch).
      - ints: kept as ints. We do not coerce 1 -> 1.0; param schemas decide.
      - floats: rounded per precision (top-level field precision wins).
      - everything else: passed through; the json encoder will reject if not
        serializable.
    """
    precisions = precisions or {}

    if isinstance(value, BaseModel):
        return canonical_value(
            value.model_dump(mode="json"),
            precisions=precisions,
        )

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k in sorted(value.keys()):
            out[k] = canonical_value(
                value[k],
                field_name=k,
                precisions=precisions,
            )
        return out

    if isinstance(value, list | tuple):
        return [canonical_value(v, precisions=precisions) for v in value]

    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        precision = (
            precisions.get(field_name, DEFAULT_FLOAT_PRECISION)
            if field_name is not None
            else DEFAULT_FLOAT_PRECISION
        )
        return _round_float(value, precision)

    return value


def canonical_json(
    params: BaseModel | Mapping[str, Any],
    params_model: type[BaseModel] | None = None,
) -> bytes:
    """Encode params to canonical JSON bytes suitable for hashing.

    If `params` is a Pydantic model instance, its type is used to discover
    per-field precisions automatically. If it's a raw mapping, pass
    `params_model` to enable precision overrides; otherwise the default
    precision applies to all floats.
    """
    if isinstance(params, BaseModel):
        precisions = _field_precisions(type(params))
        payload = params.model_dump(mode="json")
    else:
        precisions = _field_precisions(params_model)
        payload = dict(params)

    canonical = canonical_value(payload, precisions=precisions)
    return json.dumps(
        canonical,
        sort_keys=False,  # already sorted by canonical_value
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def node_hash(
    *,
    node_id: str,
    node_version: int,
    inputs: Mapping[str, RefLike],
    params: BaseModel | Mapping[str, Any],
    params_model: type[BaseModel] | None = None,
    extra_keys: Mapping[str, str] | None = None,
) -> str:
    """Compute the cache key for a node execution.

    Parts hashed:
      - node id and version (a version bump invalidates cache cleanly)
      - inputs sorted by port name; each input contributes its own node_hash
      - canonical params bytes
      - any extra keys (e.g. {"siril_version": "1.4.0"} for nodes that shell
        out to Siril; bumping the underlying tool invalidates cleanly)

    Returns a hex sha256 digest.
    """
    h = hashlib.sha256()
    h.update(b"id=")
    h.update(node_id.encode("utf-8"))
    h.update(b"\x00v=")
    h.update(str(node_version).encode("utf-8"))

    h.update(b"\x00inputs=")
    for port in sorted(inputs.keys()):
        ref = inputs[port]
        h.update(port.encode("utf-8"))
        h.update(b"=")
        h.update(ref.node_hash.encode("utf-8"))
        h.update(b"/")
        h.update(ref.port.encode("utf-8"))
        h.update(b";")

    h.update(b"\x00params=")
    h.update(canonical_json(params, params_model=params_model))

    if extra_keys:
        h.update(b"\x00extra=")
        for k in sorted(extra_keys.keys()):
            h.update(k.encode("utf-8"))
            h.update(b"=")
            h.update(extra_keys[k].encode("utf-8"))
            h.update(b";")

    return h.hexdigest()


class RefLike:
    """Structural type matching server.models.Ref for the parts node_hash needs.

    Defined here as a minimal protocol so canonical.py has no import cycle with
    models.py. Anything with `.node_hash: str` and `.port: str` works.
    """

    node_hash: str
    port: str
