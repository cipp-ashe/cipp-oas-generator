"""End-to-end integration test for passthru pipeline flow."""
import json
import pytest
from pathlib import Path

from stage_passthru_builder import build_passthru_resolved


def test_full_passthru_flow(sample_registry):
    """
    Simulate the full pipeline flow:
    Stage 1 output -> Stage 2 output -> passthru builder -> verify variant structure.
    """
    # Simulated Stage 1 output for ListGraphRequest
    endpoint_index = {
        "ListGraphRequest": {
            "endpoint": "ListGraphRequest",
            "http_methods": ["GET"],
            "coverage": "BLIND",
            "is_passthrough": True,
            "downstream_calls": [
                {
                    "service": "graph",
                    "method": "GET",
                    "url_pattern": "/users",
                    "select": "id,displayName",
                    "is_passthru": True,
                },
            ],
        }
    }

    # Simulated Stage 2 output for ListGraphRequest
    frontend_calls = {
        "ListGraphRequest": {
            "endpoint": "ListGraphRequest",
            "passthru_calls": [
                {
                    "graph_url": "/users",
                    "select": "id,displayName",
                    "source_file": "src/pages/Users.jsx",
                },
                {
                    "graph_url": "/groups",
                    "select": None,
                    "source_file": "src/pages/Groups.jsx",
                },
            ],
        }
    }

    # Run passthru builder
    result = build_passthru_resolved(endpoint_index, frontend_calls, sample_registry)

    # Validate structure
    assert "ListGraphRequest" in result
    entry = result["ListGraphRequest"]
    assert entry["coverage"] == "PASSTHRU"
    assert entry["discriminator_param"] == "Endpoint"

    # /users should be deduplicated (present in both Stage 1 and Stage 2)
    # /groups should be added from Stage 2
    assert len(entry["variants"]) == 2
    variant_urls = {v["endpoint_value"] for v in entry["variants"]}
    assert "/users" in variant_urls
    assert "/groups" in variant_urls

    # /users variant should have projected response (select was provided)
    users_variant = next(v for v in entry["variants"] if v["endpoint_value"] == "/users")
    assert users_variant["select_projected"] is True
    resp_props = users_variant["response_schema"]["items"]["properties"]
    assert "id" in resp_props
    assert "displayName" in resp_props
    # Fields NOT in $select should be absent
    assert "userPrincipalName" not in resp_props

    # /groups variant should have full response (no select)
    groups_variant = next(v for v in entry["variants"] if v["endpoint_value"] == "/groups")
    assert groups_variant["select_projected"] is False


def test_full_passthru_flow_with_sidecar_merge(sample_registry):
    """Test that sidecar add_passthru_variants integrates into the flow."""
    from stage3_merger import _merge_sidecar_passthru_variants, _assign_passthru_coverage

    endpoint_index = {
        "ListGraphRequest": {
            "downstream_calls": [],
        }
    }
    frontend_calls = {
        "ListGraphRequest": {
            "passthru_calls": [
                {"graph_url": "/users", "select": None, "source_file": "src/Users.jsx"},
            ],
        }
    }

    result = build_passthru_resolved(endpoint_index, frontend_calls, sample_registry)
    passthru_ep = result["ListGraphRequest"]

    # Sidecar adds a custom variant
    sidecar = {
        "add_passthru_variants": [
            {
                "endpoint_value": "customSuffix/tables",
                "request_schema": {"properties": {"Table": {"type": "string"}}},
                "response_schema": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            }
        ]
    }
    _merge_sidecar_passthru_variants(passthru_ep, sidecar)

    assert len(passthru_ep["variants"]) == 2
    values = {v["endpoint_value"] for v in passthru_ep["variants"]}
    assert "/users" in values
    assert "customSuffix/tables" in values

    # Coverage should be PASSTHRU
    ep_data = {"coverage_tier": "BLIND"}
    _assign_passthru_coverage(ep_data, passthru_ep)
    assert ep_data["coverage_tier"] == "PASSTHRU"


def test_emitter_produces_valid_oneof(sample_registry):
    """Stage 4 emitter functions produce valid oneOf structure."""
    from stage4_emitter import _emit_passthru_request_body, _emit_passthru_response

    passthru_resolved = {
        "discriminator_param": "Endpoint",
        "variants": [
            {
                "endpoint_value": "/users",
                "request_schema": {"properties": {"Endpoint": {"const": "/users"}}},
                "response_schema": {"type": "array", "items": {"properties": {"id": {"type": "string"}}}},
            },
            {
                "endpoint_value": "/groups",
                "request_schema": {"properties": {"Endpoint": {"const": "/groups"}}},
                "response_schema": {"type": "array", "items": {"properties": {"id": {"type": "string"}}}},
            },
        ],
    }

    req_body = _emit_passthru_request_body(passthru_resolved)
    assert "oneOf" in req_body["content"]["application/json"]["schema"]
    assert req_body["content"]["application/json"]["schema"]["discriminator"]["propertyName"] == "Endpoint"

    resp = _emit_passthru_response(passthru_resolved)
    assert "oneOf" in resp["content"]["application/json"]["schema"]
    assert len(resp["content"]["application/json"]["schema"]["oneOf"]) == 2
