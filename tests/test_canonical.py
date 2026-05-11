"""Property tests for cache key canonicalization.

These tests are the contract for the cache. If you find yourself reaching for
@settings(max_examples=1) to make a flaky test pass, stop: the canonicalization
itself is wrong. Float-noise drift in node hashes will cause real cache misses
in production.
"""

from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, Field

from server.canonical import (
    DEFAULT_FLOAT_PRECISION,
    canonical_json,
    canonical_value,
    node_hash,
)


class FakeRef:
    def __init__(self, node_hash: str, port: str = "out", path: str = "/x") -> None:
        self.node_hash = node_hash
        self.port = port
        self.path = path


def test_external_refs_with_different_paths_hash_differently() -> None:
    """Regression: external Refs (node_hash='ext') used to collide on path,
    so submitting jobs against different sessions served the wrong cached
    output. node_hash now mixes the path in to keep them distinct."""

    class P(BaseModel):
        x: int = 1

    h_a = node_hash(
        node_id="convert_lights",
        node_version=1,
        inputs={"lights": FakeRef("ext", "lights", "/data/sessionA")},
        params=P(),
    )
    h_b = node_hash(
        node_id="convert_lights",
        node_version=1,
        inputs={"lights": FakeRef("ext", "lights", "/data/sessionB")},
        params=P(),
    )
    assert h_a != h_b


class GHSParams(BaseModel):
    """A params model with explicit hash precision per field."""

    D: float = Field(default=1.0, json_schema_extra={"hash_precision": 3})
    b: float = Field(default=0.25, json_schema_extra={"hash_precision": 3})
    SP: float = Field(default=0.0, json_schema_extra={"hash_precision": 3})


class LooseParams(BaseModel):
    """A params model with no precision overrides; uses DEFAULT_FLOAT_PRECISION."""

    threshold: float = 0.5
    enabled: bool = True
    label: str = ""


# ---------------------------------------------------------------------------
# Determinism: same input -> same bytes / hash.
# ---------------------------------------------------------------------------


def test_canonical_json_is_deterministic_for_equal_input() -> None:
    a = canonical_json({"x": 1, "y": 2.0})
    b = canonical_json({"x": 1, "y": 2.0})
    assert a == b


@given(
    st.dictionaries(
        st.text(min_size=1, max_size=8),
        st.one_of(st.integers(-(10**6), 10**6), st.text(max_size=8)),
        max_size=10,
    )
)
def test_dict_key_order_does_not_affect_hash(d: dict) -> None:
    """Reordering dict keys must not change the canonical encoding."""
    if not d:
        return
    keys = list(d.keys())
    reversed_d = {k: d[k] for k in reversed(keys)}
    assert canonical_json(d) == canonical_json(reversed_d)


# ---------------------------------------------------------------------------
# Float tolerance: floats within rounding precision hash same; outside differ.
# ---------------------------------------------------------------------------


def test_floats_within_default_precision_hash_same() -> None:
    eps = 10 ** -(DEFAULT_FLOAT_PRECISION + 2)
    a = canonical_json({"k": 0.5})
    b = canonical_json({"k": 0.5 + eps})
    assert a == b


def test_floats_outside_default_precision_hash_different() -> None:
    bump = 10 ** -(DEFAULT_FLOAT_PRECISION - 1)
    a = canonical_json({"k": 0.5})
    b = canonical_json({"k": 0.5 + bump})
    assert a != b


def test_per_field_precision_is_honored() -> None:
    # GHSParams.D has hash_precision=3; values within 1e-4 collapse, outside don't.
    p1 = GHSParams(D=1.5, b=0.25, SP=0.0)
    p2 = GHSParams(D=1.5 + 1e-5, b=0.25, SP=0.0)
    p3 = GHSParams(D=1.5 + 1e-2, b=0.25, SP=0.0)
    assert canonical_json(p1) == canonical_json(p2)
    assert canonical_json(p1) != canonical_json(p3)


def test_default_precision_applies_when_no_field_metadata() -> None:
    eps = 10 ** -(DEFAULT_FLOAT_PRECISION + 2)
    a = LooseParams(threshold=0.123)
    b = LooseParams(threshold=0.123 + eps)
    assert canonical_json(a) == canonical_json(b)


def test_negative_zero_normalized() -> None:
    a = canonical_value(-0.0)
    b = canonical_value(0.0)
    assert a == b
    assert canonical_json({"k": -0.0}) == canonical_json({"k": 0.0})


# ---------------------------------------------------------------------------
# Sensitivity: meaningful differences must hash differently.
# ---------------------------------------------------------------------------


def test_node_hash_changes_with_node_id() -> None:
    inputs: dict = {}
    p = LooseParams(threshold=0.5)
    h1 = node_hash(node_id="a", node_version=1, inputs=inputs, params=p)
    h2 = node_hash(node_id="b", node_version=1, inputs=inputs, params=p)
    assert h1 != h2


def test_node_hash_changes_with_version() -> None:
    p = LooseParams(threshold=0.5)
    h1 = node_hash(node_id="a", node_version=1, inputs={}, params=p)
    h2 = node_hash(node_id="a", node_version=2, inputs={}, params=p)
    assert h1 != h2


def test_node_hash_changes_with_params() -> None:
    h1 = node_hash(node_id="a", node_version=1, inputs={}, params=LooseParams(threshold=0.5))
    h2 = node_hash(node_id="a", node_version=1, inputs={}, params=LooseParams(threshold=0.6))
    assert h1 != h2


def test_node_hash_changes_with_inputs() -> None:
    p = LooseParams()
    inputs1 = {"image": FakeRef("hash_a", "out")}
    inputs2 = {"image": FakeRef("hash_b", "out")}
    h1 = node_hash(node_id="a", node_version=1, inputs=inputs1, params=p)  # type: ignore[arg-type]
    h2 = node_hash(node_id="a", node_version=1, inputs=inputs2, params=p)  # type: ignore[arg-type]
    assert h1 != h2


def test_node_hash_stable_across_input_port_iteration_order() -> None:
    p = LooseParams()
    inputs1 = {"a": FakeRef("ha"), "b": FakeRef("hb")}
    inputs2 = {"b": FakeRef("hb"), "a": FakeRef("ha")}
    h1 = node_hash(node_id="x", node_version=1, inputs=inputs1, params=p)  # type: ignore[arg-type]
    h2 = node_hash(node_id="x", node_version=1, inputs=inputs2, params=p)  # type: ignore[arg-type]
    assert h1 == h2


def test_node_hash_extra_keys_invalidate() -> None:
    p = LooseParams()
    h1 = node_hash(node_id="x", node_version=1, inputs={}, params=p)
    h2 = node_hash(
        node_id="x",
        node_version=1,
        inputs={},
        params=p,
        extra_keys={"siril_version": "1.4.0"},
    )
    h3 = node_hash(
        node_id="x",
        node_version=1,
        inputs={},
        params=p,
        extra_keys={"siril_version": "1.4.1"},
    )
    assert h1 != h2
    assert h2 != h3


# ---------------------------------------------------------------------------
# JSON shape: output is parseable, sort_keys-compatible, and compact.
# ---------------------------------------------------------------------------


@given(
    st.dictionaries(
        st.text(min_size=1, max_size=8),
        st.one_of(
            st.integers(-(10**6), 10**6),
            st.booleans(),
            st.text(max_size=8),
            st.floats(allow_nan=False, allow_infinity=False, width=32),
        ),
        max_size=8,
    )
)
@settings(max_examples=200)
def test_canonical_json_roundtrips_through_json(d: dict) -> None:
    encoded = canonical_json(d)
    decoded = json.loads(encoded)
    assert canonical_json(decoded) == encoded


def test_canonical_json_uses_sorted_keys_in_output() -> None:
    encoded = canonical_json({"z": 1, "a": 2, "m": 3})
    assert encoded == b'{"a":2,"m":3,"z":1}'


# ---------------------------------------------------------------------------
# Cache-root independence: internal-ref paths must not bleed into the hash.
# ---------------------------------------------------------------------------


def test_internal_ref_path_does_not_affect_node_hash() -> None:
    """Internal refs (real producer hash) are identified by hash+port. Mixing
    their on-disk path would couple downstream hashes to the cache root, so
    moving ASTROLAB_HOME would silently invalidate the entire chain."""

    class P(BaseModel):
        x: int = 1

    # Same logical pipeline, two cache roots: producer hash is identical
    # (the upstream node would have hashed deterministically), but the on-disk
    # path lives under different roots.
    upstream_hash = "a" * 64
    h_root_a = node_hash(
        node_id="downstream",
        node_version=1,
        inputs={"image": FakeRef(upstream_hash, "out", "/cache/rootA/" + upstream_hash + "/image.fit")},
        params=P(),
    )
    h_root_b = node_hash(
        node_id="downstream",
        node_version=1,
        inputs={"image": FakeRef(upstream_hash, "out", "/cache/rootB/" + upstream_hash + "/image.fit")},
        params=P(),
    )
    assert h_root_a == h_root_b


def test_external_ref_path_still_affects_node_hash() -> None:
    """Counterpart to the internal-ref test: external refs must still mix
    their path, since that path IS the user-facing identity (eg the session
    folder selected in the UI)."""

    class P(BaseModel):
        x: int = 1

    h_a = node_hash(
        node_id="convert_lights",
        node_version=1,
        inputs={"lights": FakeRef("ext", "lights", "/data/sessionA")},
        params=P(),
    )
    h_b = node_hash(
        node_id="convert_lights",
        node_version=1,
        inputs={"lights": FakeRef("ext", "lights", "/data/sessionB")},
        params=P(),
    )
    assert h_a != h_b
