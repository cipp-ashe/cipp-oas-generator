"""Tests for Stage 3 dual-mode data attachment and sidecar merge."""
import pytest


@pytest.fixture
def dual_mode_resolved():
    return {
        "ListUsers": {
            "dual_mode": True,
            "discriminator_param": "UserID",
            "variants": [
                {
                    "mode": "collection",
                    "title": "Collection (no UserID)",
                    "description": "Returns all users",
                    "response_schema": {"type": "array", "items": {"properties": {"id": {"type": "string"}}}},
                },
                {
                    "mode": "single_item",
                    "title": "Single item (UserID provided)",
                    "description": "Returns one user",
                    "response_schema": {"type": "array", "items": {"properties": {"id": {"type": "string"}, "manager": {"type": "object"}}}},
                },
            ],
        }
    }


def test_attach_dual_mode_data(dual_mode_resolved):
    from stage3_merger import _attach_dual_mode_data
    ep_data = {"endpoint": "ListUsers"}
    dm_ep = dual_mode_resolved["ListUsers"]
    _attach_dual_mode_data(ep_data, dm_ep)
    assert "dual_mode_resolved" in ep_data
    assert ep_data["dual_mode_resolved"]["discriminator_param"] == "UserID"
    assert len(ep_data["dual_mode_resolved"]["variants"]) == 2


def test_sidecar_dual_mode_response_overrides_single_item(dual_mode_resolved):
    from stage3_merger import _merge_sidecar_dual_mode
    dm_ep = dual_mode_resolved["ListUsers"]
    sidecar = {
        "dual_mode_response": {
            "single_item": {
                "type": "object",
                "properties": {
                    "groupInfo": {"type": "object"},
                    "members": {"type": "array"},
                }
            }
        }
    }
    _merge_sidecar_dual_mode(dm_ep, sidecar)
    single = next(v for v in dm_ep["variants"] if v["mode"] == "single_item")
    assert single["response_schema"]["type"] == "object"
    assert "groupInfo" in single["response_schema"]["properties"]


def test_sidecar_dual_mode_response_overrides_collection(dual_mode_resolved):
    from stage3_merger import _merge_sidecar_dual_mode
    dm_ep = dual_mode_resolved["ListUsers"]
    sidecar = {
        "dual_mode_response": {
            "collection": {
                "type": "array",
                "items": {"properties": {"custom": {"type": "string"}}}
            }
        }
    }
    _merge_sidecar_dual_mode(dm_ep, sidecar)
    coll = next(v for v in dm_ep["variants"] if v["mode"] == "collection")
    assert "custom" in coll["response_schema"]["items"]["properties"]


def test_no_sidecar_dual_mode_leaves_variants_unchanged(dual_mode_resolved):
    from stage3_merger import _merge_sidecar_dual_mode
    dm_ep = dual_mode_resolved["ListUsers"]
    original_coll = next(v for v in dm_ep["variants"] if v["mode"] == "collection")["response_schema"]
    _merge_sidecar_dual_mode(dm_ep, {})
    coll_after = next(v for v in dm_ep["variants"] if v["mode"] == "collection")["response_schema"]
    assert coll_after == original_coll
