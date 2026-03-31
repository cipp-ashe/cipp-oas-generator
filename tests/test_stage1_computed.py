"""Tests for PS1 computed field extraction."""


def test_extract_add_member_basic():
    from stage1_api_scanner import _extract_computed_fields
    ps1 = '''
    $_ | Add-Member -MemberType NoteProperty -Name 'teamsEnabled' -Value $false -Force
    $_ | Add-Member -MemberType NoteProperty -Name 'primDomain' -Value ($_.mail -split '@' | Select-Object -Last 1) -Force
    '''
    fields = _extract_computed_fields(ps1)
    names = {f["name"] for f in fields}
    assert "teamsEnabled" in names
    assert "primDomain" in names


def test_extract_add_member_boolean_type():
    from stage1_api_scanner import _extract_computed_fields
    ps1 = '$_ | Add-Member -MemberType NoteProperty -Name \'dynamicGroupBool\' -Value $false -Force'
    fields = _extract_computed_fields(ps1)
    f = next(f for f in fields if f["name"] == "dynamicGroupBool")
    assert f["type"] == "boolean"


def test_extract_add_member_string_type():
    from stage1_api_scanner import _extract_computed_fields
    ps1 = "$_ | Add-Member -MemberType NoteProperty -Name 'Aliases' -Value ($_.ProxyAddresses -join ', ') -Force"
    fields = _extract_computed_fields(ps1)
    f = next(f for f in fields if f["name"] == "Aliases")
    assert f["type"] == "string"


def test_extract_select_object_computed():
    from stage1_api_scanner import _extract_computed_fields
    ps1 = """Select-Object *, @{Name='primDomain'; Expression={ $_.mail -split '@' | Select-Object -Last 1 }}, @{Name='teamsEnabled'; Expression={ if ($_.resourceProvisioningOptions -like '*Team*') { $true } else { $false } }}"""
    fields = _extract_computed_fields(ps1)
    names = {f["name"] for f in fields}
    assert "primDomain" in names
    assert "teamsEnabled" in names


def test_extract_select_object_boolean_from_expression():
    from stage1_api_scanner import _extract_computed_fields
    ps1 = "@{Name='teamsEnabled'; Expression={ if ($_.x) { $true } else { $false } }}"
    fields = _extract_computed_fields(ps1)
    f = next(f for f in fields if f["name"] == "teamsEnabled")
    assert f["type"] == "boolean"


def test_extract_deduplicates():
    from stage1_api_scanner import _extract_computed_fields
    ps1 = '''
    $_ | Add-Member -MemberType NoteProperty -Name 'Tenant' -Value $TenantFilter -Force
    $_ | Add-Member -MemberType NoteProperty -Name 'Tenant' -Value $TenantFilter -Force
    '''
    fields = _extract_computed_fields(ps1)
    tenant_fields = [f for f in fields if f["name"] == "Tenant"]
    assert len(tenant_fields) == 1


def test_extract_notepropertyname_shorthand():
    from stage1_api_scanner import _extract_computed_fields
    ps1 = "$Rule | Add-Member -NotePropertyName 'Tenant' -NotePropertyValue $TenantFilter -Force"
    fields = _extract_computed_fields(ps1)
    assert any(f["name"] == "Tenant" for f in fields)


def test_extract_source_tags():
    from stage1_api_scanner import _extract_computed_fields
    ps1 = '''
    $_ | Add-Member -MemberType NoteProperty -Name 'foo' -Value 'bar' -Force
    Select-Object @{Name='baz'; Expression={ $_.x }}
    '''
    fields = _extract_computed_fields(ps1)
    foo = next(f for f in fields if f["name"] == "foo")
    baz = next(f for f in fields if f["name"] == "baz")
    assert foo["source"] == "ps1_add_member"
    assert baz["source"] == "ps1_select_computed"


def test_extract_integer_type():
    from stage1_api_scanner import _extract_computed_fields
    ps1 = "$_ | Add-Member -MemberType NoteProperty -Name 'DeviceCount' -Value ($devices.Count) -Force"
    fields = _extract_computed_fields(ps1)
    f = next(f for f in fields if f["name"] == "DeviceCount")
    assert f["type"] == "integer"


def test_extract_default_type_is_string():
    from stage1_api_scanner import _extract_computed_fields
    ps1 = "$_ | Add-Member -MemberType NoteProperty -Name 'CippStatus' -Value 'Good' -Force"
    fields = _extract_computed_fields(ps1)
    f = next(f for f in fields if f["name"] == "CippStatus")
    assert f["type"] == "string"
