"""Tests for stage_passthru_builder — Graph resolution + oneOf assembly."""
import json
import pytest


def test_resolve_variant_found_in_registry(sample_registry):
    from stage_passthru_builder import _resolve_variant
    variant = _resolve_variant("GET", "/users", None, sample_registry)
    assert variant is not None
    assert variant["request_schema"] is None  # GET has no request body
    assert variant["response_schema"] is not None
    assert variant.get("sidecar_candidate") is not True


def test_resolve_variant_with_select_projection(sample_registry):
    from stage_passthru_builder import _resolve_variant
    variant = _resolve_variant("GET", "/users", "id,displayName", sample_registry)
    assert variant is not None
    assert variant["select_projected"] is True
    resp = variant["response_schema"]
    # Projected response should only contain selected fields
    items_props = resp["items"]["properties"]
    assert "id" in items_props
    assert "displayName" in items_props
    assert "userPrincipalName" not in items_props


def test_resolve_variant_not_in_registry(sample_registry):
    from stage_passthru_builder import _resolve_variant
    variant = _resolve_variant("GET", "/deviceManagement/managedDevices", None, sample_registry)
    assert variant is not None
    assert variant["sidecar_candidate"] is True


def test_resolve_variant_post_with_request_schema(sample_registry):
    from stage_passthru_builder import _resolve_variant
    variant = _resolve_variant("POST", "/groups", None, sample_registry)
    assert variant is not None
    assert variant["request_schema"] is not None
    assert "required" in variant["request_schema"]


def test_build_passthru_resolved_single_endpoint(sample_registry):
    from stage_passthru_builder import build_passthru_resolved
    endpoint_index = {
        "ListGraphRequest": {
            "downstream_calls": [
                {"service": "graph", "method": "GET", "url_pattern": "/users",
                 "select": "id,displayName", "is_passthru": True},
            ]
        }
    }
    frontend_calls = {
        "ListGraphRequest": {
            "passthru_calls": [
                {"graph_url": "/users", "select": "id,displayName",
                 "source_file": "src/pages/Users.jsx"},
                {"graph_url": "/groups", "select": None,
                 "source_file": "src/pages/Groups.jsx"},
            ]
        }
    }
    result = build_passthru_resolved(endpoint_index, frontend_calls, sample_registry)
    assert "ListGraphRequest" in result
    entry = result["ListGraphRequest"]
    assert entry["coverage"] == "PASSTHRU"
    assert entry["discriminator_param"] == "Endpoint"
    # Should have 2 variants: /users (deduplicated) and /groups
    assert len(entry["variants"]) == 2
    urls = {v["endpoint_value"] for v in entry["variants"]}
    assert "/users" in urls
    assert "/groups" in urls


def test_build_passthru_resolved_deduplicates(sample_registry):
    """Same graph_url from Stage 1 and Stage 2 should not duplicate."""
    from stage_passthru_builder import build_passthru_resolved
    endpoint_index = {
        "ListGraphRequest": {
            "downstream_calls": [
                {"service": "graph", "method": "GET", "url_pattern": "/users",
                 "select": "id,displayName", "is_passthru": True},
            ]
        }
    }
    frontend_calls = {
        "ListGraphRequest": {
            "passthru_calls": [
                {"graph_url": "/users", "select": "id,displayName",
                 "source_file": "src/pages/Users.jsx"},
            ]
        }
    }
    result = build_passthru_resolved(endpoint_index, frontend_calls, sample_registry)
    assert len(result["ListGraphRequest"]["variants"]) == 1


def test_build_passthru_resolved_unresolved_tracked(sample_registry):
    """URLs not in registry should appear in the unresolved list."""
    from stage_passthru_builder import build_passthru_resolved
    endpoint_index = {"ListGraphRequest": {"downstream_calls": []}}
    frontend_calls = {
        "ListGraphRequest": {
            "passthru_calls": [
                {"graph_url": "/deviceManagement/managedDevices", "select": None,
                 "source_file": "src/pages/Devices.jsx"},
            ]
        }
    }
    result = build_passthru_resolved(endpoint_index, frontend_calls, sample_registry)
    entry = result["ListGraphRequest"]
    assert len(entry["unresolved"]) == 1
    assert entry["unresolved"][0]["graph_url"] == "/deviceManagement/managedDevices"
    assert entry["unresolved"][0]["sidecar_candidate"] is True


def test_unresolvable_frontend_calls_in_gaps(sample_registry):
    """Computed endpoint= values should land in unresolved."""
    from stage_passthru_builder import build_passthru_resolved
    endpoint_index = {"ListGraphRequest": {"downstream_calls": []}}
    frontend_calls = {
        "ListGraphRequest": {
            "passthru_calls": [
                {"graph_url": None, "select": None,
                 "source_file": "src/pages/Dynamic.jsx", "unresolvable": True},
            ]
        }
    }
    result = build_passthru_resolved(endpoint_index, frontend_calls, sample_registry)
    entry = result["ListGraphRequest"]
    assert len(entry["unresolved"]) == 1
