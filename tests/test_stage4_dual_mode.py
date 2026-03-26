"""Tests for Stage 4 dual-mode oneOf response emitter."""
import pytest


@pytest.fixture
def dual_mode_endpoint():
    return {
        "endpoint": "ListUsers",
        "coverage_tier": "FULL",
        "http_methods": ["GET"],
        "query_params": [{"name": "UserID", "in": "query"}],
        "body_params": [],
        "dual_mode_resolved": {
            "dual_mode": True,
            "discriminator_param": "UserID",
            "variants": [
                {
                    "mode": "collection",
                    "title": "Collection (no UserID)",
                    "description": "Returns all users with projected fields",
                    "response_schema": {
                        "type": "array",
                        "items": {"type": "object", "properties": {"id": {"type": "string"}, "displayName": {"type": "string"}}},
                    },
                },
                {
                    "mode": "single_item",
                    "title": "Single item (UserID provided)",
                    "description": "Returns one user with full details",
                    "response_schema": {
                        "type": "array",
                        "items": {"type": "object", "properties": {"id": {"type": "string"}, "manager": {"type": "object"}}},
                    },
                },
            ],
        },
    }


def test_emit_dual_mode_response_has_oneof(dual_mode_endpoint):
    from stage4_emitter import _emit_dual_mode_response
    resp = _emit_dual_mode_response(dual_mode_endpoint["dual_mode_resolved"])
    schema = resp["content"]["application/json"]["schema"]
    assert "oneOf" in schema
    assert len(schema["oneOf"]) == 2


def test_emit_dual_mode_response_titles(dual_mode_endpoint):
    from stage4_emitter import _emit_dual_mode_response
    resp = _emit_dual_mode_response(dual_mode_endpoint["dual_mode_resolved"])
    titles = [v["title"] for v in resp["content"]["application/json"]["schema"]["oneOf"]]
    assert any("no UserID" in t for t in titles)
    assert any("UserID provided" in t for t in titles)


def test_emit_dual_mode_response_descriptions(dual_mode_endpoint):
    from stage4_emitter import _emit_dual_mode_response
    resp = _emit_dual_mode_response(dual_mode_endpoint["dual_mode_resolved"])
    for v in resp["content"]["application/json"]["schema"]["oneOf"]:
        assert "description" in v


def test_emit_dual_mode_skips_null_schemas():
    from stage4_emitter import _emit_dual_mode_response
    dm = {
        "discriminator_param": "WidgetID",
        "variants": [
            {"mode": "collection", "title": "Collection", "description": "All", "response_schema": None},
            {"mode": "single_item", "title": "Single", "description": "One",
             "response_schema": {"type": "object", "properties": {"id": {"type": "string"}}}},
        ],
    }
    resp = _emit_dual_mode_response(dm)
    schema = resp["content"]["application/json"]["schema"]
    assert len(schema["oneOf"]) == 1
