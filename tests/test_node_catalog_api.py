"""Tests for GET /api/nodes endpoint."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import nodes.basic  # noqa: F401  ensure all nodes are registered
from server.api import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# (a) Endpoint returns 200 and includes all registered nodes
# ---------------------------------------------------------------------------

def test_nodes_endpoint_returns_200(client: TestClient) -> None:
    r = client.get("/api/nodes")
    assert r.status_code == 200


def test_nodes_endpoint_includes_all_registered_kinds(client: TestClient) -> None:
    from server.registry import all_kinds

    r = client.get("/api/nodes")
    assert r.status_code == 200
    catalog = r.json()

    for kind, variant in all_kinds():
        expected_key = kind if variant is None else f"{kind}/{variant}"
        assert expected_key in catalog, f"node {expected_key!r} missing from catalog"


def test_nodes_endpoint_shape(client: TestClient) -> None:
    """Each entry has the expected top-level keys."""
    r = client.get("/api/nodes")
    catalog = r.json()
    required_keys = {"version", "cost", "uses_siril", "inputs", "outputs", "params"}
    for key, entry in catalog.items():
        missing = required_keys - entry.keys()
        assert not missing, f"{key}: missing keys {missing}"


# ---------------------------------------------------------------------------
# (b) At least one node has agent_hint populated
# ---------------------------------------------------------------------------

def test_at_least_one_agent_hint_populated(client: TestClient) -> None:
    r = client.get("/api/nodes")
    catalog = r.json()
    hints_found = [
        (node_key, param_name)
        for node_key, entry in catalog.items()
        for param_name, param in entry["params"].items()
        if param.get("agent_hint")
    ]
    assert hints_found, "Expected at least one param with a non-empty agent_hint"


def test_stretch_node_has_agent_hints(client: TestClient) -> None:
    """Spot-check that the stretch node has hints on its tuneable params."""
    r = client.get("/api/nodes")
    catalog = r.json()
    assert "stretch" in catalog
    stretch_params = catalog["stretch"]["params"]
    # method, shadows_clip, target_bg, mtf_midtones should all have hints.
    for param_name in ("method", "shadows_clip", "target_bg", "mtf_midtones"):
        assert param_name in stretch_params, f"stretch missing param {param_name!r}"
        assert stretch_params[param_name]["agent_hint"], (
            f"stretch.{param_name} has an empty agent_hint"
        )


# ---------------------------------------------------------------------------
# (c) Constraint extraction handles ge/le/gt/lt/Literal correctly
# ---------------------------------------------------------------------------

def test_constraint_ge_le_extracted(client: TestClient) -> None:
    """target_bg has ge=0.0, le=1.0 — verify they appear in constraints."""
    r = client.get("/api/nodes")
    catalog = r.json()
    target_bg = catalog["stretch"]["params"]["target_bg"]
    assert "ge" in target_bg["constraints"], "expected 'ge' in target_bg constraints"
    assert "le" in target_bg["constraints"], "expected 'le' in target_bg constraints"
    assert target_bg["constraints"]["ge"] == 0.0
    assert target_bg["constraints"]["le"] == 1.0


def test_constraint_gt_extracted(client: TestClient) -> None:
    """sigma_low has gt=0.0 — verify 'gt' appears (not 'ge')."""
    r = client.get("/api/nodes")
    catalog = r.json()
    sigma_low = catalog["seq_stack"]["params"]["sigma_low"]
    assert "gt" in sigma_low["constraints"], "expected 'gt' in sigma_low constraints"
    assert sigma_low["constraints"]["gt"] == 0.0


def test_literal_type_extracted(client: TestClient) -> None:
    """stretch.method is a Literal; its type should reflect the enum values."""
    r = client.get("/api/nodes")
    catalog = r.json()
    method_param = catalog["stretch"]["params"]["method"]
    # The type string should include Literal or enum-style notation.
    assert "autostretch" in method_param["type"] or method_param["type"].startswith("Literal"), (
        f"Expected Literal type for stretch.method, got {method_param['type']!r}"
    )


def test_optional_param_constraint_extracted(client: TestClient) -> None:
    """filter_fwhm is Optional[float] with ge=0.0, le=1.0."""
    r = client.get("/api/nodes")
    catalog = r.json()
    filter_fwhm = catalog["seq_register"]["params"]["filter_fwhm"]
    # Should have le or ge extracted even through the anyOf branch.
    assert filter_fwhm["constraints"].get("ge") == 0.0 or filter_fwhm["constraints"].get("le") == 1.0, (
        f"filter_fwhm constraints: {filter_fwhm['constraints']}"
    )


def test_ui_hidden_params_still_appear_with_flag(client: TestClient) -> None:
    """ui_hidden params should appear in the catalog but with ui_hidden=True."""
    r = client.get("/api/nodes")
    catalog = r.json()
    # seq_stack.input_basename is ui_hidden
    input_basename = catalog["seq_stack"]["params"]["input_basename"]
    assert input_basename["ui_hidden"] is True


def test_ui_section_extracted(client: TestClient) -> None:
    """Params with ui_section='advanced' should have it populated."""
    r = client.get("/api/nodes")
    catalog = r.json()
    rejection_type = catalog["seq_stack"]["params"]["rejection_type"]
    assert rejection_type["ui_section"] == "advanced"
