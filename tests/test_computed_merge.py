"""Tests for merging computed fields into response schemas."""
import pytest


def test_merge_adds_computed_to_registry_schema():
    from stage_passthru_builder import _merge_computed_fields
    schema = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "displayName": {"type": "string"},
            }
        }
    }
    computed = [
        {"name": "primDomain", "type": "string", "source": "ps1_select_computed"},
        {"name": "teamsEnabled", "type": "boolean", "source": "ps1_add_member"},
    ]
    result = _merge_computed_fields(schema, computed)
    props = result["items"]["properties"]
    assert "id" in props
    assert "displayName" in props
    assert "primDomain" in props
    assert props["primDomain"] == {"type": "string"}
    assert props["teamsEnabled"] == {"type": "boolean"}


def test_merge_registry_wins_on_collision():
    from stage_passthru_builder import _merge_computed_fields
    schema = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
            }
        }
    }
    computed = [
        {"name": "id", "type": "integer", "source": "ps1_add_member"},
    ]
    result = _merge_computed_fields(schema, computed)
    assert result["items"]["properties"]["id"] == {"type": "string"}


def test_merge_with_null_schema():
    from stage_passthru_builder import _merge_computed_fields
    computed = [
        {"name": "Tenant", "type": "string", "source": "ps1_add_member"},
        {"name": "CippStatus", "type": "string", "source": "ps1_add_member"},
    ]
    result = _merge_computed_fields(None, computed)
    assert result is not None
    assert result["items"]["properties"]["Tenant"] == {"type": "string"}
    assert result["items"]["properties"]["CippStatus"] == {"type": "string"}


def test_merge_empty_computed_returns_original():
    from stage_passthru_builder import _merge_computed_fields
    schema = {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}}}}
    result = _merge_computed_fields(schema, [])
    assert result == schema


def test_merge_with_object_schema():
    from stage_passthru_builder import _merge_computed_fields
    schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
        }
    }
    computed = [
        {"name": "SID", "type": "string", "source": "ps1_select_computed"},
    ]
    result = _merge_computed_fields(schema, computed)
    assert "SID" in result["properties"]


def test_build_endpoint_responses_includes_computed(sample_registry):
    from stage_passthru_builder import build_endpoint_responses
    endpoint_index = {
        "ListGroups": {
            "graph_calls": [{"path": "/groups", "method": "GET"}],
            "downstream_calls": [
                {"service": "graph", "method": "GET", "url_pattern": "/groups",
                 "select": "id,displayName", "is_passthru": False},
            ],
            "computed_fields": [
                {"name": "primDomain", "type": "string", "source": "ps1_select_computed"},
                {"name": "teamsEnabled", "type": "boolean", "source": "ps1_add_member"},
            ],
            "dual_mode": None,
        }
    }
    result = build_endpoint_responses(endpoint_index, sample_registry)
    assert "ListGroups" in result
    schema = result["ListGroups"]
    props = schema["items"]["properties"]
    assert "id" in props
    assert "primDomain" in props
    assert "teamsEnabled" in props
