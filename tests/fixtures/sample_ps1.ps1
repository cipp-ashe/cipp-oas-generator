<#
.FUNCTIONALITY Entrypoint
.ROLE Reader
.SYNOPSIS List Graph Request
#>
function Invoke-ListGraphRequest {
    param($Request)
    $Endpoint = $Request.Query.Endpoint
    $TenantFilter = $Request.Query.tenantFilter
    $GraphResult = New-GraphGetRequest -uri "https://graph.microsoft.com/v1.0/$($Endpoint)" -tenantid $TenantFilter
    return $GraphResult
}
