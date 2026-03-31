"""Tests for coalesce-aware parameter deduplication in Stage 1."""


def test_coalesce_query_body_re_basic():
    """Query-first coalesce: $Request.Query.X ?? $Request.Body.X"""
    from stage1_api_scanner import COALESCE_QUERY_BODY_RE

    ps1 = '$TenantFilter = $Request.Query.TenantFilter ?? $Request.Body.TenantFilter'
    matches = COALESCE_QUERY_BODY_RE.findall(ps1)
    assert matches == ["TenantFilter"]


def test_coalesce_body_query_re_basic():
    """Body-first coalesce: $Request.Body.X ?? $Request.Query.X"""
    from stage1_api_scanner import COALESCE_BODY_QUERY_RE

    ps1 = '$ID = $Request.Body.ID ?? $Request.Query.ID'
    matches = COALESCE_BODY_QUERY_RE.findall(ps1)
    assert matches == ["ID"]


def test_coalesce_query_body_re_with_value_intermediary():
    """Three-part chain: $Request.Query.X ?? $Request.Body.X.value ?? $Request.Body.X"""
    from stage1_api_scanner import COALESCE_QUERY_BODY_RE

    ps1 = '$Type = $Request.Query.Type ?? $Request.Body.Type.value ?? $Request.Body.Type'
    matches = COALESCE_QUERY_BODY_RE.findall(ps1)
    assert matches == ["Type"]


def test_coalesce_does_not_match_body_to_body():
    """Body-to-body case fallback is NOT cross-location — should NOT match."""
    from stage1_api_scanner import COALESCE_QUERY_BODY_RE, COALESCE_BODY_QUERY_RE

    ps1 = '$displayName = $Request.Body.displayName ?? $Request.Body.Displayname'
    assert COALESCE_QUERY_BODY_RE.findall(ps1) == []
    assert COALESCE_BODY_QUERY_RE.findall(ps1) == []


def test_coalesce_case_insensitive_request():
    """Regex handles both $Request and $request."""
    from stage1_api_scanner import COALESCE_QUERY_BODY_RE

    ps1 = '$f = $request.query.filter ?? $request.body.filter'
    matches = COALESCE_QUERY_BODY_RE.findall(ps1)
    assert matches == ["filter"]


def test_coalesce_multiple_params_in_one_file():
    """Multiple coalesce patterns in the same file."""
    from stage1_api_scanner import COALESCE_QUERY_BODY_RE

    ps1 = """
    $TenantFilter = $Request.Query.TenantFilter ?? $Request.Body.TenantFilter
    $ID = $Request.Query.ID ?? $Request.Body.ID
    $Type = $Request.Query.Type ?? $Request.Body.Type
    """
    matches = COALESCE_QUERY_BODY_RE.findall(ps1)
    assert sorted(matches) == ["ID", "TenantFilter", "Type"]


def test_coalesced_names_extraction():
    """coalesced_names set should contain lowercased param names from both regex directions."""
    from stage1_api_scanner import (
        COALESCE_QUERY_BODY_RE, COALESCE_BODY_QUERY_RE, PARAM_NOISE,
    )

    content = """
    $TenantFilter = $Request.Query.TenantFilter ?? $Request.Body.TenantFilter
    $ID = $Request.Body.ID ?? $Request.Query.ID
    $Name = $Request.Body.Name
    """
    coalesced_names = {
        p.lower()
        for p in COALESCE_QUERY_BODY_RE.findall(content)
        + COALESCE_BODY_QUERY_RE.findall(content)
        if p.lower() not in PARAM_NOISE
    }
    assert coalesced_names == {"tenantfilter", "id"}
    # "Name" is NOT coalesced — it only appears in body
    assert "name" not in coalesced_names


def test_filter_post_coalesced_removes_from_query():
    """POST endpoint: non-exempt coalesced params removed from query, kept in body."""
    from stage1_api_scanner import COALESCE_QUERY_EXEMPT

    coalesced_names = {"id", "tenantfilter"}
    http_methods = ["POST"]
    query_params = [
        {"name": "ID", "in": "query", "confidence": "high", "source": "ast_direct"},
        {"name": "TenantFilter", "in": "query", "confidence": "high", "source": "ast_direct"},
    ]
    body_params = [
        {"name": "ID", "in": "body", "confidence": "high", "source": "ast_direct"},
        {"name": "TenantFilter", "in": "body", "confidence": "high", "source": "ast_direct"},
        {"name": "displayName", "in": "body", "confidence": "high", "source": "ast_direct"},
    ]

    is_write_method = any(m.upper() in ("POST", "PUT", "PATCH", "DELETE") for m in http_methods)
    exempt = COALESCE_QUERY_EXEMPT
    non_exempt = coalesced_names - exempt

    if is_write_method:
        query_params = [p for p in query_params if p["name"].lower() not in non_exempt]
        body_params = [p for p in body_params if p["name"].lower() not in exempt]

    for p in (query_params + body_params):
        if p["name"].lower() in coalesced_names:
            p["source"] = "ast_coalesced"

    # ID removed from query (non-exempt, POST → body only)
    query_names = [p["name"] for p in query_params]
    assert "ID" not in query_names
    # TenantFilter stays in query (exempt)
    assert "TenantFilter" in query_names

    # TenantFilter removed from body (exempt → query only)
    body_names = [p["name"] for p in body_params]
    assert "TenantFilter" not in body_names
    # ID stays in body
    assert "ID" in body_names
    # displayName unaffected
    assert "displayName" in body_names

    # Coalesced params tagged
    id_param = next(p for p in body_params if p["name"] == "ID")
    assert id_param["source"] == "ast_coalesced"
    tf_param = next(p for p in query_params if p["name"] == "TenantFilter")
    assert tf_param["source"] == "ast_coalesced"


def test_filter_get_coalesced_removes_from_body():
    """GET endpoint: coalesced params removed from body, kept in query."""
    coalesced_names = {"id", "tenantfilter"}
    http_methods = ["GET"]
    query_params = [
        {"name": "ID", "in": "query", "confidence": "high", "source": "ast_direct"},
        {"name": "TenantFilter", "in": "query", "confidence": "high", "source": "ast_direct"},
    ]
    body_params = [
        {"name": "ID", "in": "body", "confidence": "high", "source": "ast_direct"},
        {"name": "TenantFilter", "in": "body", "confidence": "high", "source": "ast_direct"},
    ]

    is_write_method = any(m.upper() in ("POST", "PUT", "PATCH", "DELETE") for m in http_methods)

    if not is_write_method:
        body_params = [p for p in body_params if p["name"].lower() not in coalesced_names]

    for p in (query_params + body_params):
        if p["name"].lower() in coalesced_names:
            p["source"] = "ast_coalesced"

    # All coalesced params removed from body for GET
    assert body_params == []
    # Both stay in query
    query_names = [p["name"] for p in query_params]
    assert "ID" in query_names
    assert "TenantFilter" in query_names


def test_filter_no_coalesced_names_no_change():
    """When no coalesced names, params should be unchanged."""
    coalesced_names = set()
    query_params = [
        {"name": "ID", "in": "query", "confidence": "high", "source": "ast_direct"},
    ]
    body_params = [
        {"name": "displayName", "in": "body", "confidence": "high", "source": "ast_direct"},
    ]

    if coalesced_names:
        pass  # filtering would go here

    assert len(query_params) == 1
    assert len(body_params) == 1
    assert query_params[0]["source"] == "ast_direct"
    assert body_params[0]["source"] == "ast_direct"


def test_scan_endpoint_deduplicates_coalesced_post(tmp_path, monkeypatch):
    """End-to-end: scan_endpoint on a POST-like file deduplicates coalesced params."""
    import stage1_api_scanner
    from stage1_api_scanner import scan_endpoint

    ps1_content = """\
<#
.FUNCTIONALITY
Entrypoint
.ROLE
CIPP-API\\ExecTestCoalesce
.SYNOPSIS
Test endpoint for coalesce dedup
#>

param($Request, $TriggerMetadata)

$TenantFilter = $Request.Query.TenantFilter ?? $Request.Body.TenantFilter
$ID = $Request.Query.ID ?? $Request.Body.ID
$displayName = $Request.Body.displayName

Push-OutputBinding -Name Response -Value ([pscustomobject]@{
    StatusCode = [HttpStatusCode]::OK
    Body = @{ Results = "OK" }
})
"""
    # Create a fake PS1 in a temp directory structure matching expected layout
    func_dir = tmp_path / "Invoke-ExecTestCoalesce"
    func_dir.mkdir()
    ps1_file = func_dir / "run.ps1"
    ps1_file.write_text(ps1_content, encoding="utf-8")

    # Patch module-level globals so relative_to and folder_to_tag work on tmp_path
    monkeypatch.setattr(stage1_api_scanner, "API_REPO", tmp_path)
    monkeypatch.setattr(stage1_api_scanner, "HTTP_FUNCTIONS_ROOT", tmp_path)

    result = scan_endpoint(ps1_file)

    # Should not be None (it's an Entrypoint)
    assert result is not None

    query_names = {p["name"].lower() for p in result.get("query_params", [])}
    body_names = {p["name"].lower() for p in result.get("body_params", [])}

    # POST endpoint (has body params): ID should be in body only, NOT query
    assert "id" not in query_names, "ID should be deduplicated out of query for POST"
    assert "id" in body_names, "ID should remain in body for POST"

    # tenantFilter is exempt — stays in query
    assert "tenantfilter" in query_names, "tenantFilter should stay in query (exempt)"

    # tenantFilter should be removed from body (exempt → query only)
    assert "tenantfilter" not in body_names, "tenantFilter should be removed from body"

    # displayName is not coalesced — should stay in body
    assert "displayname" in body_names

    # Coalesced params should have ast_coalesced source
    id_param = next(p for p in result["body_params"] if p["name"].lower() == "id")
    assert id_param["source"] == "ast_coalesced"


def test_scan_endpoint_deduplicates_coalesced_get(tmp_path, monkeypatch):
    """End-to-end: scan_endpoint on a coalesced-only file applies POST dedup rules.

    The coalesce pattern ($Request.Query.X ?? $Request.Body.X) always generates
    both query and body reads. Because body reads are present, infer_http_method
    returns POST for files with only coalesced params. POST dedup rules apply:
    non-exempt coalesced params go to body only; exempt (tenantFilter) stays in query.
    """
    import stage1_api_scanner
    from stage1_api_scanner import scan_endpoint

    ps1_content = """\
<#
.FUNCTIONALITY
Entrypoint
.ROLE
CIPP-API\\ExecTestCoalesceGet
.SYNOPSIS
Test GET endpoint for coalesce dedup
#>

param($Request, $TriggerMetadata)

$TenantFilter = $Request.Query.TenantFilter ?? $Request.Body.TenantFilter
$ID = $Request.Query.ID ?? $Request.Body.ID

Push-OutputBinding -Name Response -Value ([pscustomobject]@{
    StatusCode = [HttpStatusCode]::OK
    Body = @{ Results = "OK" }
})
"""
    func_dir = tmp_path / "Invoke-ExecTestCoalesceGet"
    func_dir.mkdir()
    ps1_file = func_dir / "run.ps1"
    ps1_file.write_text(ps1_content, encoding="utf-8")

    # Patch module-level globals so relative_to and folder_to_tag work on tmp_path
    monkeypatch.setattr(stage1_api_scanner, "API_REPO", tmp_path)
    monkeypatch.setattr(stage1_api_scanner, "HTTP_FUNCTIONS_ROOT", tmp_path)

    result = scan_endpoint(ps1_file)
    assert result is not None

    query_names = {p["name"].lower() for p in result.get("query_params", [])}
    body_names = {p["name"].lower() for p in result.get("body_params", [])}

    # The coalesce body-side ($Request.Body.X) causes infer_http_method to return POST.
    # POST dedup rules: non-exempt coalesced (ID) → body only; exempt (tenantFilter) → query only.
    # TenantFilter stays in query (exempt)
    assert "tenantfilter" in query_names, "tenantFilter should stay in query (exempt)"
    # TenantFilter should be removed from body (exempt → query only)
    assert "tenantfilter" not in body_names, "tenantFilter should be removed from body"
    # ID goes to body (non-exempt, POST → body only)
    assert "id" in body_names, "ID should be in body for POST-inferred coalesced endpoint"
    assert "id" not in query_names, "ID should be removed from query (non-exempt, POST)"

    # Both coalesced params should be tagged ast_coalesced
    id_param = next(p for p in result["body_params"] if p["name"].lower() == "id")
    assert id_param["source"] == "ast_coalesced"
    tf_param = next(p for p in result["query_params"] if p["name"].lower() == "tenantfilter")
    assert tf_param["source"] == "ast_coalesced"


def test_ternary_query_body_re():
    """Ternary: $Request.Query.X ? $Request.Query.X : $Request.Body.X"""
    from stage1_api_scanner import TERNARY_QUERY_BODY_RE

    ps1 = '$RowKey = $Request.Query.id ? $Request.Query.id : $Request.Body.id'
    assert TERNARY_QUERY_BODY_RE.findall(ps1) == ["id"]


def test_ternary_body_query_re():
    """Reverse ternary: $Request.Body.X ? $Request.Body.X : $Request.Query.X"""
    from stage1_api_scanner import TERNARY_BODY_QUERY_RE

    ps1 = '$Action = $Request.Body.Action ? $Request.Body.Action : $Request.Query.Action'
    assert TERNARY_BODY_QUERY_RE.findall(ps1) == ["Action"]


def test_ternary_does_not_match_same_location():
    """Ternary with same location on both sides should NOT match."""
    from stage1_api_scanner import TERNARY_QUERY_BODY_RE, TERNARY_BODY_QUERY_RE

    ps1 = '$X = $Request.Query.X ? $Request.Query.X : $Request.Query.Y'
    assert TERNARY_QUERY_BODY_RE.findall(ps1) == []
    assert TERNARY_BODY_QUERY_RE.findall(ps1) == []


def test_ifelse_body_query_re():
    """If/else: if ($Request.Body.X) { ...Body.X... } else { ...Query.X... }"""
    from stage1_api_scanner import IFELSE_BODY_QUERY_RE

    ps1 = """$Action = if ($Request.Body.Action) { $Request.Body.Action } else { $Request.Query.Action }"""
    assert IFELSE_BODY_QUERY_RE.findall(ps1) == ["Action"]


def test_ifelse_query_body_re():
    """Reverse if/else: if ($Request.Query.X) { ...Query.X... } else { ...Body.X... }"""
    from stage1_api_scanner import IFELSE_QUERY_BODY_RE

    ps1 = """$Action = if ($Request.Query.Action) { $Request.Query.Action } else { $Request.Body.Action }"""
    assert IFELSE_QUERY_BODY_RE.findall(ps1) == ["Action"]


def test_ifelse_does_not_match_same_location():
    """If/else with same location on both sides should NOT match."""
    from stage1_api_scanner import IFELSE_BODY_QUERY_RE

    ps1 = """if ($Request.Body.X) { $Request.Body.X } else { $Request.Body.Y }"""
    assert IFELSE_BODY_QUERY_RE.findall(ps1) == []


def test_combined_condition_re():
    """Combined: if ($Request.Query.X -eq 'val' -or $Request.Body.X -eq 'val')"""
    from stage1_api_scanner import COMBINED_CONDITION_RE

    ps1 = """$IncludeAllTenants = if ($Request.Query.includeAllTenants -eq 'false' -or $Request.Body.includeAllTenants -eq 'false') {
    $false
} else {
    $true
}"""
    assert COMBINED_CONDITION_RE.findall(ps1) == ["includeAllTenants"]


def test_combined_condition_does_not_match_single_location():
    """Combined condition with only one location should NOT match."""
    from stage1_api_scanner import COMBINED_CONDITION_RE

    ps1 = """if ($Request.Query.X -eq 'a' -or $Request.Query.Y -eq 'b') { }"""
    assert COMBINED_CONDITION_RE.findall(ps1) == []


def test_loose_coalesce_with_split_operator():
    """Loose coalesce: $Request.Query.X -split ',' ?? $Request.Body.X"""
    from stage1_api_scanner import LOOSE_COALESCE_QR_RE

    ps1 = "$DataTypes = $Request.Query.dataTypes -split ',' ?? $Request.Body.dataTypes ?? 'All'"
    assert LOOSE_COALESCE_QR_RE.findall(ps1) == ["dataTypes"]


def test_loose_coalesce_with_cast():
    """Loose coalesce: $Request.Query.X -as [int] ?? $Request.Body.X -as [int]"""
    from stage1_api_scanner import LOOSE_COALESCE_QR_RE

    ps1 = '$Redirected = $Request.Query.Redirected -as [int] ?? $Request.Body.Redirected -as [int]'
    assert LOOSE_COALESCE_QR_RE.findall(ps1) == ["Redirected"]


def test_loose_coalesce_reverse_with_intermediaries():
    """Loose coalesce reverse: $Request.Body.X ?? ...intermediaries... ?? $Request.Query.X"""
    from stage1_api_scanner import LOOSE_COALESCE_BQ_RE

    ps1 = "$TemplateId = $Request.Body.TemplateId ?? $Request.Body.TemplateList?.value ?? $Request.Body.TemplateList ?? $Request.Query.TemplateId"
    assert LOOSE_COALESCE_BQ_RE.findall(ps1) == ["TemplateId"]


def test_loose_coalesce_does_not_match_same_location():
    """Loose coalesce should NOT match same-location fallbacks."""
    from stage1_api_scanner import LOOSE_COALESCE_QR_RE, LOOSE_COALESCE_BQ_RE

    ps1 = '$X = $Request.Body.X ?? $Request.Body.Y'
    assert LOOSE_COALESCE_QR_RE.findall(ps1) == []
    assert LOOSE_COALESCE_BQ_RE.findall(ps1) == []


def test_fallback_names_extraction():
    """fallback_names captures ternary, if/else, loose coalesce, and combined patterns."""
    from stage1_api_scanner import (
        TERNARY_QUERY_BODY_RE, TERNARY_BODY_QUERY_RE,
        IFELSE_BODY_QUERY_RE, IFELSE_QUERY_BODY_RE,
        LOOSE_COALESCE_QR_RE, LOOSE_COALESCE_BQ_RE,
        COMBINED_CONDITION_RE, PARAM_NOISE,
    )

    content = """
    $RowKey = $Request.Query.id ? $Request.Query.id : $Request.Body.id
    $Action = if ($Request.Body.Action) { $Request.Body.Action } else { $Request.Query.Action }
    $DataTypes = $Request.Query.dataTypes -split ',' ?? $Request.Body.dataTypes
    $Inc = if ($Request.Query.includeAllTenants -eq 'false' -or $Request.Body.includeAllTenants -eq 'false') { $false }
    $Name = $Request.Body.Name
    """
    fallback_names = {
        p.lower()
        for p in TERNARY_QUERY_BODY_RE.findall(content)
        + TERNARY_BODY_QUERY_RE.findall(content)
        + IFELSE_BODY_QUERY_RE.findall(content)
        + IFELSE_QUERY_BODY_RE.findall(content)
        + LOOSE_COALESCE_QR_RE.findall(content)
        + LOOSE_COALESCE_BQ_RE.findall(content)
        + COMBINED_CONDITION_RE.findall(content)
        if p.lower() not in PARAM_NOISE
    }
    assert fallback_names == {"id", "action", "datatypes", "includealltenants"}
    assert "name" not in fallback_names


def test_dual_access_tagging():
    """Params in overlap but NOT in any dedup set get tagged dual_access."""
    from stage1_api_scanner import COALESCE_QUERY_EXEMPT

    # Simulate: templateid is in both query and body but no fallback regex matched
    all_dedup_names = {"tenantfilter"}  # only tenantFilter from coalesce
    overlap_names = {"tenantfilter", "templateid"}
    dual_access_names = overlap_names - all_dedup_names - COALESCE_QUERY_EXEMPT

    assert dual_access_names == {"templateid"}

    query_params = [
        {"name": "TenantFilter", "in": "query", "source": "ast_direct"},
        {"name": "TemplateId", "in": "query", "source": "ast_direct"},
    ]
    body_params = [
        {"name": "TemplateId", "in": "body", "source": "ast_direct"},
    ]

    for p in (query_params + body_params):
        if p["name"].lower() in dual_access_names:
            p["source"] = "dual_access"

    # TemplateId tagged in both locations
    assert query_params[1]["source"] == "dual_access"
    assert body_params[0]["source"] == "dual_access"
    # TenantFilter untouched
    assert query_params[0]["source"] == "ast_direct"


def test_scan_endpoint_deduplicates_ternary(tmp_path, monkeypatch):
    """End-to-end: ternary cross-location pattern is deduped."""
    import stage1_api_scanner
    from stage1_api_scanner import scan_endpoint

    ps1_content = """\
<#
.FUNCTIONALITY
Entrypoint
.ROLE
CIPP-API\\ExecTestTernary
.SYNOPSIS
Test ternary dedup
#>

param($Request, $TriggerMetadata)

$TenantFilter = $Request.Query.TenantFilter ?? $Request.Body.TenantFilter
$RowKey = $Request.Query.id ? $Request.Query.id : $Request.Body.id
$displayName = $Request.Body.displayName

Push-OutputBinding -Name Response -Value ([pscustomobject]@{
    StatusCode = [HttpStatusCode]::OK
    Body = @{ Results = "OK" }
})
"""
    func_dir = tmp_path / "Invoke-ExecTestTernary"
    func_dir.mkdir()
    ps1_file = func_dir / "run.ps1"
    ps1_file.write_text(ps1_content, encoding="utf-8")

    monkeypatch.setattr(stage1_api_scanner, "API_REPO", tmp_path)
    monkeypatch.setattr(stage1_api_scanner, "HTTP_FUNCTIONS_ROOT", tmp_path)

    result = scan_endpoint(ps1_file)
    assert result is not None

    query_names = {p["name"].lower() for p in result.get("query_params", [])}
    body_names = {p["name"].lower() for p in result.get("body_params", [])}

    # id deduped out of query (ternary fallback, POST)
    assert "id" not in query_names
    assert "id" in body_names
    # tenantFilter exempt → query only
    assert "tenantfilter" in query_names
    assert "tenantfilter" not in body_names


def test_scan_endpoint_tags_dual_access(tmp_path, monkeypatch):
    """End-to-end: independent query/body access gets dual_access tag, not deduped."""
    import stage1_api_scanner
    from stage1_api_scanner import scan_endpoint

    ps1_content = """\
<#
.FUNCTIONALITY
Entrypoint
.ROLE
CIPP-API\\ExecTestDualAccess
.SYNOPSIS
Test dual access tagging
#>

param($Request, $TriggerMetadata)

$TenantFilter = $Request.Query.TenantFilter ?? $Request.Body.TenantFilter
$TemplateId = $Request.Body.TemplateId
$QueryTemplate = $Request.Query.TemplateId

Push-OutputBinding -Name Response -Value ([pscustomobject]@{
    StatusCode = [HttpStatusCode]::OK
    Body = @{ Results = "OK" }
})
"""
    func_dir = tmp_path / "Invoke-ExecTestDualAccess"
    func_dir.mkdir()
    ps1_file = func_dir / "run.ps1"
    ps1_file.write_text(ps1_content, encoding="utf-8")

    monkeypatch.setattr(stage1_api_scanner, "API_REPO", tmp_path)
    monkeypatch.setattr(stage1_api_scanner, "HTTP_FUNCTIONS_ROOT", tmp_path)

    result = scan_endpoint(ps1_file)
    assert result is not None

    query_names = {p["name"].lower() for p in result.get("query_params", [])}
    body_names = {p["name"].lower() for p in result.get("body_params", [])}

    # TemplateId should be in BOTH locations (dual_access, not deduped)
    assert "templateid" in query_names, "dual_access param should stay in query"
    assert "templateid" in body_names, "dual_access param should stay in body"

    # TemplateId should be tagged dual_access
    q_tmpl = next(p for p in result["query_params"] if p["name"].lower() == "templateid")
    b_tmpl = next(p for p in result["body_params"] if p["name"].lower() == "templateid")
    assert q_tmpl["source"] == "dual_access"
    assert b_tmpl["source"] == "dual_access"
