"""Tests for dual-mode resolution in the passthru builder."""
import pytest


def test_resolve_dual_mode_both_in_registry(sample_registry):
    from stage_passthru_builder import resolve_dual_mode_endpoints
    endpoint_index = {
        "ListUsers": {
            "dual_mode": {
                "discriminator_param": "UserID",
                "collection_graph_path": "/users",
                "single_item_graph_path": "/users/{userid}",
            },
            "downstream_calls": [
                {"service": "graph", "method": "GET", "url_pattern": "/users",
                 "select": "id,displayName", "is_passthru": False},
            ],
        }
    }
    result = resolve_dual_mode_endpoints(endpoint_index, sample_registry)
    assert "ListUsers" in result
    entry = result["ListUsers"]
    assert entry["dual_mode"] is True
    assert entry["discriminator_param"] == "UserID"
    assert len(entry["variants"]) == 2
    modes = {v["mode"] for v in entry["variants"]}
    assert "collection" in modes
    assert "single_item" in modes


def test_resolve_dual_mode_collection_projected(sample_registry):
    from stage_passthru_builder import resolve_dual_mode_endpoints
    endpoint_index = {
        "ListUsers": {
            "dual_mode": {
                "discriminator_param": "UserID",
                "collection_graph_path": "/users",
                "single_item_graph_path": "/users/{userid}",
            },
            "downstream_calls": [
                {"service": "graph", "method": "GET", "url_pattern": "/users",
                 "select": "id,displayName", "is_passthru": False},
            ],
        }
    }
    result = resolve_dual_mode_endpoints(endpoint_index, sample_registry)
    coll = next(v for v in result["ListUsers"]["variants"] if v["mode"] == "collection")
    assert coll["response_schema"] is not None
    items_props = coll["response_schema"]["items"]["properties"]
    assert "id" in items_props
    assert "displayName" in items_props
    assert "userPrincipalName" not in items_props


def test_resolve_dual_mode_single_item_full(sample_registry):
    from stage_passthru_builder import resolve_dual_mode_endpoints
    endpoint_index = {
        "ListUsers": {
            "dual_mode": {
                "discriminator_param": "UserID",
                "collection_graph_path": "/users",
                "single_item_graph_path": "/users/{userid}",
            },
            "downstream_calls": [],
        }
    }
    result = resolve_dual_mode_endpoints(endpoint_index, sample_registry)
    single = next(v for v in result["ListUsers"]["variants"] if v["mode"] == "single_item")
    assert single["response_schema"] is not None


def test_resolve_dual_mode_not_in_registry(sample_registry):
    from stage_passthru_builder import resolve_dual_mode_endpoints
    endpoint_index = {
        "ListWidgets": {
            "dual_mode": {
                "discriminator_param": "WidgetID",
                "collection_graph_path": "/widgets",
                "single_item_graph_path": "/widgets/{widgetid}",
            },
            "downstream_calls": [],
        }
    }
    result = resolve_dual_mode_endpoints(endpoint_index, sample_registry)
    assert "ListWidgets" in result
    coll = next(v for v in result["ListWidgets"]["variants"] if v["mode"] == "collection")
    assert coll["response_schema"] is None


def test_resolve_dual_mode_skips_non_dual():
    from stage_passthru_builder import resolve_dual_mode_endpoints
    endpoint_index = {
        "ListUsers": {
            "dual_mode": None,
            "downstream_calls": [],
        }
    }
    result = resolve_dual_mode_endpoints(endpoint_index, {})
    assert result == {}


def test_resolve_dual_mode_variant_titles(sample_registry):
    from stage_passthru_builder import resolve_dual_mode_endpoints
    endpoint_index = {
        "ListUsers": {
            "dual_mode": {
                "discriminator_param": "UserID",
                "collection_graph_path": "/users",
                "single_item_graph_path": "/users/{userid}",
            },
            "downstream_calls": [],
        }
    }
    result = resolve_dual_mode_endpoints(endpoint_index, sample_registry)
    coll = next(v for v in result["ListUsers"]["variants"] if v["mode"] == "collection")
    single = next(v for v in result["ListUsers"]["variants"] if v["mode"] == "single_item")
    assert "no UserID" in coll["title"]
    assert "UserID" in single["title"]
