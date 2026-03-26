"""End-to-end integration test for dual-mode detection and emission."""
import pytest

from stage1_api_scanner import _detect_dual_mode
from stage_passthru_builder import resolve_dual_mode_endpoints
from stage3_merger import _attach_dual_mode_data, _merge_sidecar_dual_mode
from stage4_emitter import _emit_dual_mode_response


def test_full_dual_mode_flow(sample_registry):
    """Full pipeline: detect -> resolve -> merge -> emit."""
    # Stage 1: detect dual-mode
    query_params = [{"name": "UserID", "in": "query"}, {"name": "tenantFilter", "in": "query"}]
    ps1_content = '''
    $userid = $Request.Query.UserID
    if ($userid) {
        $result = New-GraphGetRequest -uri "https://graph.microsoft.com/v1.0/users/$($userid)"
    } else {
        $result = New-GraphGetRequest -uri "https://graph.microsoft.com/v1.0/users" -select "id,displayName"
    }
    '''
    graph_calls = [{"path": "/users", "method": "GET"}]
    dual_mode = _detect_dual_mode(query_params, ps1_content, graph_calls)
    assert dual_mode is not None
    assert dual_mode["discriminator_param"] == "UserID"

    # Stage 2.5: resolve against registry
    endpoint_index = {
        "ListUsers": {
            "dual_mode": dual_mode,
            "downstream_calls": [
                {"service": "graph", "method": "GET", "url_pattern": "/users",
                 "select": "id,displayName", "is_passthru": False},
            ],
        }
    }
    resolved = resolve_dual_mode_endpoints(endpoint_index, sample_registry)
    assert "ListUsers" in resolved
    dm_ep = resolved["ListUsers"]
    assert len(dm_ep["variants"]) == 2

    # Collection should be projected
    coll = next(v for v in dm_ep["variants"] if v["mode"] == "collection")
    assert "userPrincipalName" not in coll["response_schema"]["items"]["properties"]

    # Single item should have more fields
    single = next(v for v in dm_ep["variants"] if v["mode"] == "single_item")
    assert single["response_schema"] is not None

    # Stage 3: attach to merged endpoint
    ep_data = {"endpoint": "ListUsers"}
    _attach_dual_mode_data(ep_data, dm_ep)
    assert "dual_mode_resolved" in ep_data

    # Stage 4: emit oneOf
    resp = _emit_dual_mode_response(ep_data["dual_mode_resolved"])
    schema = resp["content"]["application/json"]["schema"]
    assert "oneOf" in schema
    assert len(schema["oneOf"]) == 2
    titles = [v["title"] for v in schema["oneOf"]]
    assert any("Collection" in t for t in titles)
    assert any("Single" in t for t in titles)


def test_dual_mode_with_sidecar_override(sample_registry):
    """Sidecar overrides single-item response for restructured endpoints."""
    endpoint_index = {
        "ListGroups": {
            "dual_mode": {
                "discriminator_param": "GroupID",
                "collection_graph_path": "/groups",
                "single_item_graph_path": "/groups/{groupid}",
            },
            "downstream_calls": [
                {"service": "graph", "method": "GET", "url_pattern": "/groups",
                 "select": "id,displayName", "is_passthru": False},
            ],
        }
    }
    resolved = resolve_dual_mode_endpoints(endpoint_index, sample_registry)
    dm_ep = resolved["ListGroups"]

    # Sidecar overrides the single-item response
    sidecar = {
        "dual_mode_response": {
            "single_item": {
                "type": "object",
                "properties": {
                    "groupInfo": {"type": "object", "additionalProperties": True},
                    "members": {"type": "array", "items": {"type": "object"}},
                    "owners": {"type": "array", "items": {"type": "object"}},
                    "allowExternal": {"type": "boolean"},
                }
            }
        }
    }
    _merge_sidecar_dual_mode(dm_ep, sidecar)

    single = next(v for v in dm_ep["variants"] if v["mode"] == "single_item")
    assert single["response_schema"]["type"] == "object"
    assert "groupInfo" in single["response_schema"]["properties"]

    # Emit and verify
    ep_data = {"endpoint": "ListGroups"}
    _attach_dual_mode_data(ep_data, dm_ep)
    resp = _emit_dual_mode_response(ep_data["dual_mode_resolved"])
    one_of = resp["content"]["application/json"]["schema"]["oneOf"]
    assert len(one_of) == 2
