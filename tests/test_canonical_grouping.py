"""Unit tests for the `_canonical_group_for_row` helper.

The Library page buckets target rows by canonical so that an override
pinning "C 20" to "NGC 7000" merges into the same group as a target that
auto-resolved to "NGC 7000". This is the helper that does the
coalesce, in one tested place so the same rule gets reused wherever else
we'd want to merge (CSV exports, Tonight overlay, future scripts).
"""

from __future__ import annotations

from server.api import _canonical_group_for_row


def _row(**overrides: object) -> dict[str, object]:
    """Build a minimal dict that quacks like `sqlite3.Row` for the helper:
    only the four keys the helper actually probes need to be present.
    Lets us cover each branch without spinning up a sqlite connection."""
    base = {
        "name": "MY_TARGET",
        "resolved_canonical": None,
        "resolved_canonical_override": None,
        "resolved_source": None,
    }
    base.update(overrides)
    return base


def test_override_wins() -> None:
    """The user's pin trumps every other source, including a non-null
    auto-resolved canonical with a name that ALSO resolves."""
    row = _row(
        name="M 31",
        resolved_canonical="NGC 224",
        resolved_canonical_override="NGC 7000",
        resolved_source="position",
    )
    assert _canonical_group_for_row(row) == "NGC 7000"


def test_position_resolve() -> None:
    """No override: the position-derived canonical bucket wins."""
    row = _row(
        resolved_canonical="NGC 7000",
        resolved_source="position",
    )
    assert _canonical_group_for_row(row) == "NGC 7000"


def test_name_resolve_uses_persisted_canonical() -> None:
    """A name-resolved target with a persisted canonical groups under
    that canonical even though `resolved_as` itself surfaces as None for
    source='name'."""
    row = _row(
        name="M 31",
        resolved_canonical="NGC 224",
        resolved_source="name",
    )
    assert _canonical_group_for_row(row) == "NGC 224"


def test_name_fallback_when_no_persisted_canonical() -> None:
    """No persisted canonical, but the stored name resolves in OpenNGC:
    re-enrich on the fly so a freshly inserted target still buckets."""
    row = _row(name="M 31")
    assert _canonical_group_for_row(row) == "NGC 224"


def test_returns_none_for_unresolvable_name() -> None:
    """Garbage name + no persisted canonical = unresolved bucket (None)."""
    row = _row(name="GARBAGE_NOPE")
    assert _canonical_group_for_row(row) is None


def test_returns_none_for_blank_state() -> None:
    """Empty everything is also unresolved; an `enrich("")` defensive
    null-check protects the helper from blowing up on edge rows."""
    row = _row(name="")
    assert _canonical_group_for_row(row) is None


def test_two_rows_resolving_to_same_canonical() -> None:
    """The whole point: pinning row A to "NGC 7000" while row B
    auto-resolves to "NGC 7000" buckets both rows under the same key."""
    row_a = _row(
        name="C 20",
        resolved_canonical="NGC 7000",
        resolved_source="position",
    )
    row_b = _row(
        name="MY_GARBAGE",
        resolved_canonical_override="NGC 7000",
    )
    assert _canonical_group_for_row(row_a) == _canonical_group_for_row(row_b)
    assert _canonical_group_for_row(row_a) == "NGC 7000"
