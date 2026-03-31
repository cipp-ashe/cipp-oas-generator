"""Tests for Stage 2 passthru_calls extraction."""
from stage2_frontend_scanner import _extract_passthru_calls


def test_extract_passthru_calls_static_endpoint():
    """Static endpoint= value in URL string should be extracted."""
    jsx = '''
    const { data } = ApiGetCall({
        url: "/api/ListGraphRequest",
        queryParams: { Endpoint: "/users", tenantFilter: tenant, $select: "id,displayName" },
    });
    '''
    calls = _extract_passthru_calls(jsx, "ListGraphRequest", "src/pages/Users.jsx")
    assert len(calls) == 1
    assert calls[0]["graph_url"] == "/users"
    assert calls[0]["select"] == "id,displayName"
    assert calls[0]["source_file"] == "src/pages/Users.jsx"


def test_extract_passthru_calls_endpoint_in_url_param():
    """endpoint= in URL query string should be extracted."""
    jsx = '''
    const { data } = ApiGetCall({
        url: "/api/ListGraphRequest?Endpoint=/groups&tenantFilter=" + tenant,
    });
    '''
    calls = _extract_passthru_calls(jsx, "ListGraphRequest", "src/pages/Groups.jsx")
    assert len(calls) == 1
    assert calls[0]["graph_url"] == "/groups"


def test_extract_passthru_calls_with_select_in_url():
    """$select= embedded in URL string should be captured."""
    jsx = '''
    url: `/api/ListGraphRequest?Endpoint=/users&$select=id,displayName,mail`
    '''
    calls = _extract_passthru_calls(jsx, "ListGraphRequest", "src/pages/Users.jsx")
    assert len(calls) == 1
    assert calls[0]["select"] == "id,displayName,mail"


def test_extract_passthru_calls_no_match():
    """JSX that doesn't call this passthru endpoint returns empty list."""
    jsx = '''
    const { data } = ApiGetCall({ url: "/api/ListUsers" });
    '''
    calls = _extract_passthru_calls(jsx, "ListGraphRequest", "src/pages/Users.jsx")
    assert calls == []


def test_extract_passthru_calls_multiple_calls():
    """Multiple calls to same passthru endpoint in one file."""
    jsx = '''
    ApiGetCall({ url: "/api/ListGraphRequest?Endpoint=/users&$select=id" });
    ApiGetCall({ url: "/api/ListGraphRequest?Endpoint=/groups" });
    '''
    calls = _extract_passthru_calls(jsx, "ListGraphRequest", "src/pages/Mixed.jsx")
    assert len(calls) == 2
    urls = {c["graph_url"] for c in calls}
    assert "/users" in urls
    assert "/groups" in urls


def test_extract_passthru_calls_computed_endpoint_flagged():
    """Computed endpoint= value should be flagged as unresolvable."""
    jsx = '''
    ApiGetCall({ url: `/api/ListGraphRequest?Endpoint=${graphEndpoint}` });
    '''
    calls = _extract_passthru_calls(jsx, "ListGraphRequest", "src/pages/Dynamic.jsx")
    # Computed values are unresolvable — returned with graph_url=None
    assert len(calls) == 1
    assert calls[0]["graph_url"] is None
    assert calls[0].get("unresolvable") is True


def test_extract_passthru_calls_data_block_static():
    """Real CIPP pattern: Endpoint in data: {} block with no leading slash."""
    jsx = '''
    const deviceRequest = ApiGetCall({
        url: "/api/ListGraphRequest",
        data: {
            Endpoint: "deviceManagement/managedDevices",
            tenantFilter: tenant,
        },
        queryKey: "devices",
    });
    '''
    calls = _extract_passthru_calls(jsx, "ListGraphRequest", "src/pages/Devices.jsx")
    assert len(calls) == 1
    assert calls[0]["graph_url"] == "/deviceManagement/managedDevices"


def test_extract_passthru_calls_data_block_with_slash():
    """Endpoint in data block with leading slash."""
    jsx = '''
    ApiGetCall({
        url: "/api/ListGraphRequest",
        data: { Endpoint: "/users", tenantFilter: tenant },
    });
    '''
    calls = _extract_passthru_calls(jsx, "ListGraphRequest", "src/pages/Users.jsx")
    assert len(calls) == 1
    assert calls[0]["graph_url"] == "/users"


def test_extract_passthru_calls_data_block_backtick_static():
    """Backtick-quoted static Endpoint value (no interpolation)."""
    jsx = '''
    ApiGetCall({
        url: "/api/ListGraphRequest",
        data: { Endpoint: `users`, tenantFilter: tenant },
    });
    '''
    calls = _extract_passthru_calls(jsx, "ListGraphRequest", "src/pages/Users.jsx")
    assert len(calls) == 1
    assert calls[0]["graph_url"] == "/users"


def test_extract_passthru_calls_data_block_backtick_dynamic():
    """Backtick-quoted Endpoint with ${} interpolation is unresolvable."""
    jsx = '''
    ApiGetCall({
        url: "/api/ListGraphRequest",
        data: {
            Endpoint: `deviceManagement/managedDevices/${deviceId}`,
            tenantFilter: tenant,
        },
    });
    '''
    calls = _extract_passthru_calls(jsx, "ListGraphRequest", "src/pages/Device.jsx")
    assert len(calls) == 1
    assert calls[0]["graph_url"] is None
    assert calls[0].get("unresolvable") is True


def test_extract_passthru_calls_data_block_multiple():
    """Multiple data-block calls in one file."""
    jsx = '''
    ApiGetCall({
        url: "/api/ListGraphRequest",
        data: { Endpoint: "users", tenantFilter: t },
    });
    ApiGetCall({
        url: "/api/ListGraphRequest",
        data: { Endpoint: "groups", tenantFilter: t },
    });
    '''
    calls = _extract_passthru_calls(jsx, "ListGraphRequest", "src/pages/Both.jsx")
    assert len(calls) == 2
    urls = {c["graph_url"] for c in calls}
    assert "/users" in urls
    assert "/groups" in urls
