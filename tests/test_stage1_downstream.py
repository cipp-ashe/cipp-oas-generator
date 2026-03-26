"""Tests for Stage 1 downstream_calls extraction."""
from stage1_api_scanner import _extract_graph_calls, _extract_select_param, _join_continued_lines


def test_extract_graph_calls_gets_path_and_method():
    ps1 = 'New-GraphGetRequest -uri "https://graph.microsoft.com/v1.0/users" -tenantid $tenant'
    calls = _extract_graph_calls(ps1)
    assert len(calls) == 1
    assert calls[0]["path"] == "/users"
    assert calls[0]["method"] == "GET"


def test_extract_graph_calls_post_request():
    ps1 = 'New-GraphPostRequest -uri "https://graph.microsoft.com/v1.0/groups" -body $body'
    calls = _extract_graph_calls(ps1)
    assert len(calls) == 1
    assert calls[0]["method"] == "POST"


def test_extract_select_param_from_string_literal():
    ps1 = 'New-GraphGetRequest -uri "https://graph.microsoft.com/v1.0/users" -select "id,displayName,userPrincipalName"'
    select = _extract_select_param(ps1, "/users")
    assert select == "id,displayName,userPrincipalName"


def test_extract_select_param_returns_none_when_absent():
    ps1 = 'New-GraphGetRequest -uri "https://graph.microsoft.com/v1.0/users"'
    select = _extract_select_param(ps1, "/users")
    assert select is None


def test_extract_select_param_single_quoted():
    ps1 = "New-GraphGetRequest -uri 'https://graph.microsoft.com/v1.0/groups' -select 'id,displayName'"
    select = _extract_select_param(ps1, "/groups")
    assert select == "id,displayName"


def test_passthru_detected_when_uri_from_variable():
    """When the URI comes from $Request.Query.Endpoint, is_passthru should be true."""
    ps1 = 'New-GraphGetRequest -uri "https://graph.microsoft.com/v1.0/$($Request.Query.Endpoint)"'
    from stage1_api_scanner import _detect_passthru_uri_sources
    sources = _detect_passthru_uri_sources(ps1)
    assert len(sources) > 0


def test_build_downstream_calls_structure():
    """downstream_calls list should have the spec-defined shape."""
    from stage1_api_scanner import _build_downstream_calls
    graph_calls = [{"path": "/users", "method": "GET"}]
    exo_calls = ["Get-Mailbox"]
    select_map = {"/users": "id,displayName"}
    is_passthru = False

    result = _build_downstream_calls(graph_calls, exo_calls, select_map, is_passthru)
    assert len(result) == 2

    graph_entry = result[0]
    assert graph_entry["service"] == "graph"
    assert graph_entry["method"] == "GET"
    assert graph_entry["url_pattern"] == "/users"
    assert graph_entry["select"] == "id,displayName"
    assert graph_entry["is_passthru"] is False

    exo_entry = result[1]
    assert exo_entry["service"] == "exo"
    assert exo_entry["cmdlet"] == "Get-Mailbox"
    assert exo_entry["is_passthru"] is False
