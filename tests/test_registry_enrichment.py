"""Tests for schema registry auto-enrichment from scanner discoveries."""
import json
import pytest


def test_enrich_adds_missing_graph_call():
    from stage_passthru_builder import enrich_registry
    registry = {"version": "1.0", "graph": {}, "exo": {}}
    endpoint_index = {
        "ListWidgets": {
            "graph_calls": [{"path": "/widgets", "method": "GET"}],
            "downstream_calls": [
                {"service": "graph", "method": "GET", "url_pattern": "/widgets",
                 "select": "id,displayName", "is_passthru": False},
            ],
        }
    }
    stats = enrich_registry(registry, endpoint_index)
    assert "GET /widgets" in registry["graph"]
    entry = registry["graph"]["GET /widgets"]
    assert entry["source"] == "discovered"
    assert entry["select_fields"]["id"] == "string"
    assert entry["select_fields"]["displayName"] == "string"
    assert entry["response"] is not None
    assert stats["new_entries_added"] == 1


def test_enrich_does_not_overwrite_curated():
    from stage_passthru_builder import enrich_registry
    registry = {
        "version": "1.0",
        "graph": {
            "GET /users": {
                "source": "curated",
                "request": None,
                "response": {"type": "array", "items": {"type": "object"}},
                "select_fields": {"id": "string"},
            }
        },
        "exo": {},
    }
    endpoint_index = {
        "ListUsers": {
            "graph_calls": [{"path": "/users", "method": "GET"}],
            "downstream_calls": [
                {"service": "graph", "method": "GET", "url_pattern": "/users",
                 "select": "id,displayName,mail", "is_passthru": False},
            ],
        }
    }
    stats = enrich_registry(registry, endpoint_index)
    assert registry["graph"]["GET /users"]["source"] == "curated"
    assert "displayName" not in registry["graph"]["GET /users"]["select_fields"]
    assert stats["curated_preserved"] >= 1
    assert stats["new_entries_added"] == 0


def test_enrich_updates_existing_discovered():
    from stage_passthru_builder import enrich_registry
    registry = {
        "version": "1.0",
        "graph": {
            "GET /widgets": {
                "source": "discovered",
                "request": None,
                "response": None,
                "select_fields": {"id": "string"},
            }
        },
        "exo": {},
    }
    endpoint_index = {
        "ListWidgets": {
            "graph_calls": [{"path": "/widgets", "method": "GET"}],
            "downstream_calls": [
                {"service": "graph", "method": "GET", "url_pattern": "/widgets",
                 "select": "id,displayName,version", "is_passthru": False},
            ],
        }
    }
    stats = enrich_registry(registry, endpoint_index)
    entry = registry["graph"]["GET /widgets"]
    assert entry["source"] == "discovered"
    assert "displayName" in entry["select_fields"]
    assert "version" in entry["select_fields"]
    assert "id" in entry["select_fields"]
    assert entry["response"] is not None
    assert stats["entries_updated"] == 1


def test_enrich_no_select_adds_null_entry():
    from stage_passthru_builder import enrich_registry
    registry = {"version": "1.0", "graph": {}, "exo": {}}
    endpoint_index = {
        "ExecDeleteUser": {
            "graph_calls": [{"path": "/users/{id}", "method": "DELETE"}],
            "downstream_calls": [],
        }
    }
    stats = enrich_registry(registry, endpoint_index)
    assert "DELETE /users/{id}" in registry["graph"]
    entry = registry["graph"]["DELETE /users/{id}"]
    assert entry["source"] == "discovered"
    assert entry["select_fields"] is None
    assert entry["response"] is None
    assert stats["discovered_without_select"] == 1


def test_enrich_merges_select_from_multiple_endpoints():
    from stage_passthru_builder import enrich_registry
    registry = {"version": "1.0", "graph": {}, "exo": {}}
    endpoint_index = {
        "ListUsers": {
            "graph_calls": [{"path": "/users", "method": "GET"}],
            "downstream_calls": [
                {"service": "graph", "method": "GET", "url_pattern": "/users",
                 "select": "id,displayName", "is_passthru": False},
            ],
        },
        "ListUserMailbox": {
            "graph_calls": [{"path": "/users", "method": "GET"}],
            "downstream_calls": [
                {"service": "graph", "method": "GET", "url_pattern": "/users",
                 "select": "id,mail,userPrincipalName", "is_passthru": False},
            ],
        },
    }
    stats = enrich_registry(registry, endpoint_index)
    entry = registry["graph"]["GET /users"]
    assert "id" in entry["select_fields"]
    assert "displayName" in entry["select_fields"]
    assert "mail" in entry["select_fields"]
    assert "userPrincipalName" in entry["select_fields"]


def test_enrich_uses_graph_field_types():
    from stage_passthru_builder import enrich_registry
    registry = {"version": "1.0", "graph": {}, "exo": {}}
    endpoint_index = {
        "ListUsers": {
            "graph_calls": [{"path": "/users", "method": "GET"}],
            "downstream_calls": [
                {"service": "graph", "method": "GET", "url_pattern": "/users",
                 "select": "id,accountEnabled,createdDateTime", "is_passthru": False},
            ],
        }
    }
    stats = enrich_registry(registry, endpoint_index)
    fields = registry["graph"]["GET /users"]["select_fields"]
    assert fields["id"] == "string"
    assert fields["accountEnabled"] == "boolean"
    assert fields["createdDateTime"] == {"type": "string", "format": "date-time"}


def test_fetch_schemas_stamps_curated_source(tmp_path, monkeypatch):
    import fetch_schemas
    monkeypatch.setattr(fetch_schemas, "_SCHEMAS_DIR", tmp_path)
    fetch_schemas.run()
    data = json.loads((tmp_path / "schema-registry.json").read_text())
    for key, entry in data["graph"].items():
        assert entry.get("source") == "curated", f"{key} missing source=curated"
    for key, entry in data["exo"].items():
        assert entry.get("source") == "curated", f"{key} missing source=curated"
