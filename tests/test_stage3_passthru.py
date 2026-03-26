"""Tests for Stage 3 PASSTHRU tier and add_passthru_variants merge."""
import json
import pytest


@pytest.fixture
def passthru_resolved():
    """Sample passthru-resolved.json data."""
    return {
        "stage": "passthru_builder",
        "passthru_endpoints": {
            "ListGraphRequest": {
                "coverage": "PASSTHRU",
                "discriminator_param": "Endpoint",
                "variants": [
                    {
                        "endpoint_value": "/users",
                        "request_schema": {"properties": {"Endpoint": {"const": "/users"}}},
                        "response_schema": {"type": "array", "items": {"properties": {"id": {"type": "string"}}}},
                        "select_projected": True,
                    }
                ],
                "unresolved": [],
            }
        }
    }


@pytest.fixture
def sidecar_with_passthru_variants():
    """Sample sidecar with add_passthru_variants key."""
    return {
        "add_passthru_variants": [
            {
                "endpoint_value": "customSuffix/tables",
                "description": "Azure table query",
                "request_schema": {
                    "properties": {
                        "Table": {"type": "string"},
                        "Filter": {"type": "string"},
                    }
                },
                "response_schema": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                },
                "trust_level": "reversed",
            }
        ]
    }


def test_passthru_tier_assigned(passthru_resolved):
    from stage3_merger import _assign_passthru_coverage
    ep_data = {"coverage_tier": "BLIND"}
    passthru_ep = passthru_resolved["passthru_endpoints"]["ListGraphRequest"]
    _assign_passthru_coverage(ep_data, passthru_ep)
    assert ep_data["coverage_tier"] == "PASSTHRU"


def test_passthru_tier_not_assigned_when_no_variants(passthru_resolved):
    from stage3_merger import _assign_passthru_coverage
    ep_data = {"coverage_tier": "BLIND"}
    empty_passthru = {"coverage": "PASSTHRU", "variants": [], "unresolved": []}
    _assign_passthru_coverage(ep_data, empty_passthru)
    # No variants resolved — keep original tier
    assert ep_data["coverage_tier"] == "BLIND"


def test_sidecar_passthru_variants_merged(passthru_resolved, sidecar_with_passthru_variants):
    from stage3_merger import _merge_sidecar_passthru_variants
    passthru_ep = passthru_resolved["passthru_endpoints"]["ListGraphRequest"]
    _merge_sidecar_passthru_variants(passthru_ep, sidecar_with_passthru_variants)
    # Should now have 2 variants: /users from builder + customSuffix/tables from sidecar
    assert len(passthru_ep["variants"]) == 2
    values = {v["endpoint_value"] for v in passthru_ep["variants"]}
    assert "/users" in values
    assert "customSuffix/tables" in values


def test_sidecar_passthru_variants_not_duplicated(passthru_resolved):
    """Sidecar variant with same endpoint_value as existing should replace, not duplicate."""
    from stage3_merger import _merge_sidecar_passthru_variants
    passthru_ep = passthru_resolved["passthru_endpoints"]["ListGraphRequest"]
    sidecar = {
        "add_passthru_variants": [
            {
                "endpoint_value": "/users",
                "request_schema": {"properties": {"Endpoint": {"const": "/users"}, "custom": {"type": "string"}}},
                "response_schema": {"type": "object"},
            }
        ]
    }
    _merge_sidecar_passthru_variants(passthru_ep, sidecar)
    # Sidecar should replace the existing /users variant
    assert len(passthru_ep["variants"]) == 1
    assert passthru_ep["variants"][0]["request_schema"]["properties"].get("custom") is not None


def test_passthru_data_attached_to_merged_endpoint(passthru_resolved):
    """Merged endpoint dict should carry passthru_resolved data for Stage 4."""
    from stage3_merger import _attach_passthru_data
    ep_data = {"endpoint": "ListGraphRequest"}
    passthru_ep = passthru_resolved["passthru_endpoints"]["ListGraphRequest"]
    _attach_passthru_data(ep_data, passthru_ep)
    assert "passthru_resolved" in ep_data
    assert ep_data["passthru_resolved"]["discriminator_param"] == "Endpoint"
