"""Tests for Stage 1 dual-mode endpoint detection heuristic."""
import re


def test_detect_dual_mode_basic():
    """Endpoint with ID param + conditional + Graph interpolation is dual-mode."""
    from stage1_api_scanner import _detect_dual_mode
    query_params = [{"name": "UserID", "in": "query"}]
    ps1_content = '''
    $userid = $Request.Query.UserID
    if ($userid) {
        $result = New-GraphGetRequest -uri "https://graph.microsoft.com/v1.0/users/$($userid)"
    } else {
        $result = New-GraphGetRequest -uri "https://graph.microsoft.com/v1.0/users"
    }
    '''
    graph_calls = [{"path": "/users", "method": "GET"}]
    result = _detect_dual_mode(query_params, ps1_content, graph_calls)
    assert result is not None
    assert result["discriminator_param"] == "UserID"
    assert result["collection_graph_path"] == "/users"
    assert result["single_item_graph_path"] == "/users/{userid}"


def test_detect_dual_mode_groupid():
    """GroupID variant."""
    from stage1_api_scanner import _detect_dual_mode
    query_params = [{"name": "GroupID", "in": "query"}, {"name": "tenantFilter", "in": "query"}]
    ps1_content = '''
    $GroupID = $Request.Query.GroupID
    if ($Request.Query.GroupID) {
        New-GraphGetRequest -uri "https://graph.microsoft.com/beta/groups/$($GroupID)"
    }
    '''
    graph_calls = [{"path": "/groups", "method": "GET"}]
    result = _detect_dual_mode(query_params, ps1_content, graph_calls)
    assert result is not None
    assert result["discriminator_param"] == "GroupID"
    assert result["collection_graph_path"] == "/groups"


def test_detect_dual_mode_inline_url_interpolation():
    """ID param interpolated directly in Graph URL (no if/else) IS dual-mode."""
    from stage1_api_scanner import _detect_dual_mode
    query_params = [{"name": "UserID", "in": "query"}]
    ps1_content = '''
    $userid = $Request.Query.UserID
    $result = New-GraphGetRequest -uri "https://graph.microsoft.com/v1.0/users/$($userid)"
    '''
    graph_calls = [{"path": "/users/{userid}", "method": "GET"}]
    result = _detect_dual_mode(query_params, ps1_content, graph_calls)
    # This IS dual-mode: when $userid is empty, Graph returns the collection
    assert result is not None
    assert result["discriminator_param"] == "UserID"
    assert result["collection_graph_path"] == "/users"


def test_detect_dual_mode_no_graph_interpolation():
    """ID param + conditional but no Graph URL interpolation -- not dual-mode."""
    from stage1_api_scanner import _detect_dual_mode
    query_params = [{"name": "LogId", "in": "query"}]
    ps1_content = '''
    $LogId = $Request.Query.LogId
    if ($LogId) {
        $result = Get-CIPPAzDataTableEntity @Table | Where-Object { $_.RowKey -eq $LogId }
    }
    '''
    graph_calls = []
    result = _detect_dual_mode(query_params, ps1_content, graph_calls)
    assert result is None


def test_detect_dual_mode_non_id_param():
    """Param that doesn't match ID naming -- not detected."""
    from stage1_api_scanner import _detect_dual_mode
    query_params = [{"name": "graphFilter", "in": "query"}]
    ps1_content = '''
    $filter = $Request.Query.graphFilter
    if ($filter) {
        New-GraphGetRequest -uri "https://graph.microsoft.com/v1.0/users?$filter=$filter"
    }
    '''
    graph_calls = [{"path": "/users", "method": "GET"}]
    result = _detect_dual_mode(query_params, ps1_content, graph_calls)
    assert result is None


def test_detect_dual_mode_skips_passthru():
    """Passthru endpoints are not dual-mode candidates."""
    from stage1_api_scanner import _detect_dual_mode
    query_params = [{"name": "QueueId", "in": "query"}]
    ps1_content = '$QueueId = $Request.Query.QueueId\nif ($QueueId) { }'
    graph_calls = []
    result = _detect_dual_mode(query_params, ps1_content, graph_calls, endpoint_name="ListGraphRequest")
    assert result is None


def test_detect_dual_mode_guid_param():
    """GUID-named params should also be detected."""
    from stage1_api_scanner import _detect_dual_mode
    query_params = [{"name": "GUID", "in": "query"}]
    ps1_content = '''
    $GUID = $Request.Query.GUID
    if ($GUID) {
        New-GraphGetRequest -uri "https://graph.microsoft.com/v1.0/applications/$($GUID)"
    }
    '''
    graph_calls = [{"path": "/applications", "method": "GET"}]
    result = _detect_dual_mode(query_params, ps1_content, graph_calls)
    assert result is not None
    assert result["discriminator_param"] == "GUID"
