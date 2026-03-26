"""Shared fixtures for cipp-oas-generator tests."""
import json
import pytest
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def tmp_out(tmp_path):
    """Provide a temporary output directory."""
    out = tmp_path / "out"
    out.mkdir()
    return out


@pytest.fixture
def sample_registry():
    """Minimal schema registry for testing."""
    return {
        "version": "1.0",
        "graph": {
            "GET /users": {
                "request": None,
                "response": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"id": {"type": "string"}, "displayName": {"type": "string"}}}
                },
                "select_fields": {
                    "id": "string",
                    "displayName": "string",
                    "userPrincipalName": "string",
                    "accountEnabled": "boolean",
                }
            },
            "GET /groups": {
                "request": None,
                "response": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"id": {"type": "string"}, "displayName": {"type": "string"}}}
                },
                "select_fields": {
                    "id": "string",
                    "displayName": "string",
                    "mailEnabled": "boolean",
                }
            },
            "POST /groups": {
                "request": {
                    "required": ["displayName", "mailEnabled", "mailNickname", "securityEnabled"],
                    "properties": {"displayName": {"type": "string"}}
                },
                "response": {"type": "object", "properties": {"id": {"type": "string"}}}
            }
        },
        "exo": {
            "Get-Mailbox": {
                "request": {"required": ["Identity"], "properties": {"Identity": {"type": "string"}}},
                "response": {"type": "array", "items": {"type": "object", "additionalProperties": True}}
            }
        }
    }


@pytest.fixture
def sample_registry_file(tmp_path, sample_registry):
    """Write sample registry to a temp file and return path."""
    path = tmp_path / "schema-registry.json"
    path.write_text(json.dumps(sample_registry, indent=2))
    return path
