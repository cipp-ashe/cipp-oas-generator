"""Tests for helper function traversal in Stage 1."""
import re
import pytest
from pathlib import Path


def test_extract_helper_function_names():
    from stage1_api_scanner import _extract_helper_function_names
    ps1 = '''
    $Result = Set-CIPPCloudManaged @Params
    $Data = Get-CIPPPerUserMFA -TenantFilter $TenantFilter
    $Table = Get-CIPPTable -TableName 'foo'
    '''
    names = _extract_helper_function_names(ps1)
    assert "Set-CIPPCloudManaged" in names
    assert "Get-CIPPPerUserMFA" in names
    assert "Get-CIPPTable" not in names


def test_extract_helper_ignores_config_list():
    from stage1_api_scanner import _extract_helper_function_names
    ps1 = '''
    Get-CIPPAuthentication
    Get-CIPPFeatureFlag
    Get-CIPPAzDataTableEntity @Table
    Add-CIPPScheduledTask @TaskParams
    New-CIPPQueue @QueueParams
    '''
    names = _extract_helper_function_names(ps1)
    assert len(names) == 0


def test_extract_helper_deduplicates():
    from stage1_api_scanner import _extract_helper_function_names
    ps1 = '''
    Set-CIPPSignInState @Params
    Set-CIPPSignInState @OtherParams
    '''
    names = _extract_helper_function_names(ps1)
    assert names.count("Set-CIPPSignInState") == 1


def test_resolve_helper_file(tmp_path):
    from stage1_api_scanner import _resolve_helper_file
    helper_dir = tmp_path / "Modules" / "CIPPCore" / "Public"
    helper_dir.mkdir(parents=True)
    helper_file = helper_dir / "Set-CIPPCloudManaged.ps1"
    helper_file.write_text("function Set-CIPPCloudManaged { }")
    result = _resolve_helper_file("Set-CIPPCloudManaged", tmp_path)
    assert result is not None
    assert result.name == "Set-CIPPCloudManaged.ps1"


def test_resolve_helper_file_not_found(tmp_path):
    from stage1_api_scanner import _resolve_helper_file
    result = _resolve_helper_file("Set-CIPPNonexistent", tmp_path)
    assert result is None


def test_resolve_helper_file_nested(tmp_path):
    from stage1_api_scanner import _resolve_helper_file
    subdir = tmp_path / "Modules" / "CIPPCore" / "Public" / "Identity"
    subdir.mkdir(parents=True)
    helper_file = subdir / "Set-CIPPUserJITAdmin.ps1"
    helper_file.write_text("function Set-CIPPUserJITAdmin { }")
    result = _resolve_helper_file("Set-CIPPUserJITAdmin", tmp_path)
    assert result is not None


def test_scan_helper_extracts_graph_calls(tmp_path):
    from stage1_api_scanner import _scan_helper
    helper_dir = tmp_path / "Modules" / "CIPPCore" / "Public"
    helper_dir.mkdir(parents=True)
    helper_file = helper_dir / "Set-CIPPCloudManaged.ps1"
    helper_file.write_text('''
    function Set-CIPPCloudManaged {
        $URI = "https://graph.microsoft.com/beta/users/$Id/onPremisesSyncBehavior"
        New-GraphPOSTRequest -uri $URI -type PATCH -tenantid $TenantFilter -body $Body
    }
    ''')
    visited = set()
    result = _scan_helper("Set-CIPPCloudManaged", tmp_path, visited)
    assert result is not None
    assert len(result["graph_calls"]) >= 1


def test_scan_helper_extracts_computed_fields(tmp_path):
    from stage1_api_scanner import _scan_helper
    helper_dir = tmp_path / "Modules" / "CIPPCore" / "Public"
    helper_dir.mkdir(parents=True)
    helper_file = helper_dir / "Get-CIPPSomething.ps1"
    helper_file.write_text('''
    function Get-CIPPSomething {
        $result | Add-Member -MemberType NoteProperty -Name 'customField' -Value $true -Force
    }
    ''')
    visited = set()
    result = _scan_helper("Get-CIPPSomething", tmp_path, visited)
    assert any(f["name"] == "customField" for f in result["computed_fields"])


def test_scan_helper_circular_ref_protection(tmp_path):
    from stage1_api_scanner import _scan_helper
    helper_dir = tmp_path / "Modules" / "CIPPCore" / "Public"
    helper_dir.mkdir(parents=True)
    (helper_dir / "Get-CIPPA.ps1").write_text('function Get-CIPPA { Get-CIPPB }')
    (helper_dir / "Get-CIPPB.ps1").write_text('function Get-CIPPB { Get-CIPPA }')
    visited = set()
    result = _scan_helper("Get-CIPPA", tmp_path, visited)
    assert result is not None


def test_scan_helper_recursive(tmp_path):
    from stage1_api_scanner import _scan_helper
    helper_dir = tmp_path / "Modules" / "CIPPCore" / "Public"
    helper_dir.mkdir(parents=True)
    (helper_dir / "Get-CIPPOuter.ps1").write_text(
        'function Get-CIPPOuter { $result = Get-CIPPInner -Tenant $t }')
    (helper_dir / "Get-CIPPInner.ps1").write_text('''
    function Get-CIPPInner {
        New-GraphGetRequest -uri "https://graph.microsoft.com/v1.0/users" -tenantid $t
    }
    ''')
    visited = set()
    result = _scan_helper("Get-CIPPOuter", tmp_path, visited)
    assert result is not None
    assert len(result["nested_helpers"]) == 1
    all_graph = result["graph_calls"]
    for nh in result["nested_helpers"]:
        all_graph.extend(nh.get("graph_calls", []))
    assert any("/users" in c.get("path", "") for c in all_graph)


def test_scan_helper_returns_none_for_missing(tmp_path):
    from stage1_api_scanner import _scan_helper
    visited = set()
    result = _scan_helper("Set-CIPPNonexistent", tmp_path, visited)
    assert result is None
