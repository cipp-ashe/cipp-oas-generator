"""Tests for schema-registry.json format and fetch_schemas output."""
import json
import pytest


def test_registry_has_version(sample_registry):
    assert sample_registry["version"] == "1.0"


def test_registry_graph_entry_has_required_keys(sample_registry):
    entry = sample_registry["graph"]["GET /users"]
    assert "request" in entry
    assert "response" in entry
    assert "select_fields" in entry


def test_registry_graph_request_can_be_null(sample_registry):
    """GET endpoints have no request body."""
    assert sample_registry["graph"]["GET /users"]["request"] is None


def test_registry_graph_post_has_request_schema(sample_registry):
    entry = sample_registry["graph"]["POST /groups"]
    assert entry["request"] is not None
    assert "required" in entry["request"]
    assert "properties" in entry["request"]


def test_registry_select_fields_maps_name_to_type(sample_registry):
    fields = sample_registry["graph"]["GET /users"]["select_fields"]
    assert fields["id"] == "string"
    assert fields["accountEnabled"] == "boolean"


def test_registry_exo_entry_has_required_keys(sample_registry):
    entry = sample_registry["exo"]["Get-Mailbox"]
    assert "request" in entry
    assert "response" in entry


def test_fetch_schemas_produces_registry_format(tmp_path, monkeypatch):
    """fetch_schemas.run() should write schema-registry.json."""
    import fetch_schemas
    monkeypatch.setattr(fetch_schemas, "_SCHEMAS_DIR", tmp_path)
    fetch_schemas.run()
    registry_path = tmp_path / "schema-registry.json"
    assert registry_path.exists(), "schema-registry.json not created"
    data = json.loads(registry_path.read_text())
    assert data["version"] == "1.0"
    assert "graph" in data
    assert "exo" in data
    # Spot-check a known entry
    assert "GET /users" in data["graph"]
    users = data["graph"]["GET /users"]
    assert "select_fields" in users
    assert "response" in users


def test_registry_graph_entries_have_select_fields(tmp_path, monkeypatch):
    """Every GET entry in the registry should have select_fields populated."""
    import fetch_schemas
    monkeypatch.setattr(fetch_schemas, "_SCHEMAS_DIR", tmp_path)
    fetch_schemas.run()
    data = json.loads((tmp_path / "schema-registry.json").read_text())
    for key, entry in data["graph"].items():
        if key.startswith("GET "):
            assert "select_fields" in entry, f"{key} missing select_fields"
            assert isinstance(entry["select_fields"], dict), f"{key} select_fields not a dict"
