"""Tests for Stage 4 passthru oneOf emitter + projected response emitter."""
import pytest


@pytest.fixture
def passthru_endpoint_merged():
    """Merged endpoint with passthru_resolved data from Stage 3."""
    return {
        "endpoint": "ListGraphRequest",
        "coverage_tier": "PASSTHRU",
        "http_methods": ["GET"],
        "query_params": [],
        "body_params": [],
        "passthru_resolved": {
            "coverage": "PASSTHRU",
            "discriminator_param": "Endpoint",
            "variants": [
                {
                    "endpoint_value": "/users",
                    "request_schema": {
                        "properties": {
                            "Endpoint": {"const": "/users"},
                            "tenantFilter": {"type": "string"},
                        }
                    },
                    "response_schema": {
                        "type": "array",
                        "items": {"properties": {"id": {"type": "string"}, "displayName": {"type": "string"}}},
                    },
                    "select_projected": True,
                },
                {
                    "endpoint_value": "/groups",
                    "request_schema": {
                        "properties": {
                            "Endpoint": {"const": "/groups"},
                            "tenantFilter": {"type": "string"},
                        }
                    },
                    "response_schema": {
                        "type": "array",
                        "items": {"properties": {"id": {"type": "string"}, "displayName": {"type": "string"}}},
                    },
                    "select_projected": False,
                },
            ],
            "unresolved": [],
        },
    }


def test_emit_passthru_request_body_has_oneof(passthru_endpoint_merged):
    from stage4_emitter import _emit_passthru_request_body
    body = _emit_passthru_request_body(passthru_endpoint_merged["passthru_resolved"])
    schema = body["content"]["application/json"]["schema"]
    assert "oneOf" in schema
    assert len(schema["oneOf"]) == 2
    assert schema["discriminator"]["propertyName"] == "Endpoint"


def test_emit_passthru_request_body_variant_titles(passthru_endpoint_merged):
    from stage4_emitter import _emit_passthru_request_body
    body = _emit_passthru_request_body(passthru_endpoint_merged["passthru_resolved"])
    titles = [v["title"] for v in body["content"]["application/json"]["schema"]["oneOf"]]
    assert "GET /users" in titles
    assert "GET /groups" in titles


def test_emit_passthru_response_has_oneof(passthru_endpoint_merged):
    from stage4_emitter import _emit_passthru_response
    resp = _emit_passthru_response(passthru_endpoint_merged["passthru_resolved"])
    schema = resp["content"]["application/json"]["schema"]
    assert "oneOf" in schema
    assert len(schema["oneOf"]) == 2


def test_emit_passthru_response_variant_has_title(passthru_endpoint_merged):
    from stage4_emitter import _emit_passthru_response
    resp = _emit_passthru_response(passthru_endpoint_merged["passthru_resolved"])
    variant = resp["content"]["application/json"]["schema"]["oneOf"][0]
    assert "title" in variant


def test_emit_projected_response_schema():
    """Non-passthru endpoint with inferred_response_schema should emit it."""
    from stage4_emitter import _emit_response_schema_from_inference
    inferred = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "displayName": {"type": "string"},
            }
        }
    }
    resp = _emit_response_schema_from_inference(inferred)
    schema = resp["content"]["application/json"]["schema"]
    assert schema["type"] == "array"
    assert "id" in schema["items"]["properties"]


def test_emit_fallback_response_with_extension():
    """When no response schema is available, emit additionalProperties + extension."""
    from stage4_emitter import _emit_fallback_response
    resp = _emit_fallback_response("GraphUser")
    schema = resp["content"]["application/json"]["schema"]
    assert schema.get("additionalProperties") is True
    assert schema.get("x-cipp-response-source") == "GraphUser"
