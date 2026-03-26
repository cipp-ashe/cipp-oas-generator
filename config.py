"""
config.py — Single source of truth for all CIPP OAS Generator constants.

Every tunable value lives here. Stage scripts import from here — they never
hardcode paths, patterns, or sets. When the codebase evolves, update this file.
"""
from pathlib import Path
import os

_SCRIPT_DIR = Path(__file__).parent

# ── Repo paths ────────────────────────────────────────────────────────────────
# Resolved from env vars when present, otherwise falls back to sibling directory
# conventions (../cipp-api-master, ../cipp-main). run.sh also supports --fetch
# to shallow-clone KelvinTegelaar/CIPP-API@master and KelvinTegelaar/CIPP@main
# into a temp directory automatically. See run.sh for resolution order.
_api_repo_default = _SCRIPT_DIR.parent / "cipp-api-master"
# Frontend repo: CIPP project uses "cipp-main" in cipp-repos/ layout.
# Fallback tries both the canonical name and the old "CIPP" name for
# compatibility with manually cloned layouts.
_fe_candidate_a = _SCRIPT_DIR.parent / "cipp-main"
_fe_candidate_b = _SCRIPT_DIR.parent / "CIPP"
_fe_repo_default = _fe_candidate_a if _fe_candidate_a.exists() else _fe_candidate_b

API_REPO      = Path(os.environ.get("CIPP_API_REPO",      str(_api_repo_default)))
FRONTEND_REPO = Path(os.environ.get("CIPP_FRONTEND_REPO", str(_fe_repo_default)))

# Scan the full Public/ tree so entrypoints in subdirectories like CippQueue/
# and Webhooks/ are included. The .FUNCTIONALITY Entrypoint filter in stage1
# correctly excludes Standards (Internal), activity triggers, and helpers.
HTTP_FUNCTIONS_ROOT = API_REPO / "Modules/CIPPCore/Public"
FRONTEND_SRC_ROOT   = FRONTEND_REPO / "src"

# ── Output / sidecar paths ────────────────────────────────────────────────────
OUT_DIR      = _SCRIPT_DIR / "out"
DOMAIN_DIR   = OUT_DIR / "domain"
SIDECARS_DIR = _SCRIPT_DIR / "sidecars"

# ── Shared OAS parameter $refs ────────────────────────────────────────────────
# Params that appear on nearly every endpoint and should be $ref'd, not inlined.
# Keys are lowercase for case-insensitive matching.
SHARED_PARAM_REFS: dict[str, str] = {
    "tenantfilter":    "#/components/parameters/tenantFilter",
    "selectedtenants": "#/components/parameters/selectedTenants",
}

# ── Frontend-only UI state fields ─────────────────────────────────────────────
# These are form fields that control UI behaviour but are never sent to the API.
# Lowercase. Extend as new UI-only fields are discovered.
FRONTEND_NOISE: set[str] = {
    "autopassword",   # toggles manual password entry section
    "sherweb",        # toggles Sherweb license purchase section
    "userproperties", # copy-from-user meta-field; splits into real fields client-side
}

# ── PowerShell object member noise ────────────────────────────────────────────
# Property names to suppress when walking blob alias accesses ($Alias.Field).
# Includes PS automatic members and LabelValue sub-fields (parent already captured).
PS_NOISE: set[str] = {
    "psobject", "psbase", "count", "length", "gettype",
    "exception", "message", "innerexception", "tostring",
    "label", "value",  # LabelValue sub-fields — parent param captures the shape
}

# ── API call parameter noise ──────────────────────────────────────────────────
# Names that appear in $Request.Query/Body access but aren't real endpoint params.
PARAM_NOISE: set[str] = {
    "params", "headers", "method", "body", "query", "rawbody",
}

# ── Downstream service detection patterns ─────────────────────────────────────
# Used by Stage 1 to flag which downstream services an endpoint touches.
# Values are regex strings (compiled by Stage 1).
DOWNSTREAM_PATTERNS: dict[str, str] = {
    "graph":     r"New-GraphGetRequest|New-GraphPostRequest",
    "exo":       r"New-ExoRequest",
    "scheduler": r"Add-CIPPScheduledTask",
    "sherweb":   r"Set-SherwebSubscription|sherwebLicense",
    "lancedb":   r"Get-CIPPTable|Get-CIPPAzDataTableEntity",
}

# ── Passthru endpoint configuration ──────────────────────────────────────────
# Endpoints that proxy arbitrary Graph URLs — Stage 2 extracts endpoint= values for these
PASSTHRU_ENDPOINTS: set[str] = {"ListGraphRequest"}

# ── Helper function ignore list ───────────────────────────────────────────────
# CIPP helper functions that are infrastructure/utility calls — not domain logic.
# When Stage 1 traverses helper calls from an entrypoint, these names are skipped
# so we don't recurse into table accessors, auth helpers, queue primitives, etc.
HELPER_FUNCTION_IGNORE: set[str] = {
    "Get-CIPPTable", "Get-CIPPAzDataTableEntity", "Get-CIPPAuthentication",
    "Get-CIPPFeatureFlag", "Add-CIPPScheduledTask", "Get-CIPPException",
    "Write-LogMessage", "Get-NormalizedError", "Get-CIPPDbItem",
    "New-CIPPDbRequest", "Get-CIPPTestResults", "Get-CIPPRolesystem",
    "Get-CIPPQueue", "New-CIPPQueue",
}

# Regex to normalize Graph URLs to OAS path form
# Strips base URL, collapses GUIDs and specific IDs to {id}
GRAPH_URL_NORMALIZE_RE: str = r'https://graph\.microsoft\.com/(?:v1\.0|beta)(.+)'

# ── Example hints ─────────────────────────────────────────────────────────────
# Field name (lowercase) → example value for OAS `example` field.
# Used by Stage 4 to populate request samples in doc tools (Redocly, Swagger UI).
# Keys are lowercased for case-insensitive matching.
EXAMPLE_HINTS: dict[str, str | bool | int] = {
    # Identifiers — GUID placeholders
    "tenantfilter":     "example.onmicrosoft.com",
    "selectedtenants":  "example.onmicrosoft.com",
    "userid":           "00000000-0000-0000-0000-000000000000",
    "groupid":          "00000000-0000-0000-0000-000000000000",
    "deviceid":         "00000000-0000-0000-0000-000000000000",
    "id":               "00000000-0000-0000-0000-000000000000",
    "guid":             "00000000-0000-0000-0000-000000000000",
    "tenantid":         "00000000-0000-0000-0000-000000000000",
    "appid":            "00000000-0000-0000-0000-000000000000",
    "filterid":         "00000000-0000-0000-0000-000000000000",
    "siteid":           "contoso.sharepoint.com,00000000-0000-0000-0000-000000000000",
    "roomid":           "room@example.onmicrosoft.com",
    "equipmentid":      "equipment@example.onmicrosoft.com",
    "searchid":         "00000000-0000-0000-0000-000000000000",
    "relationshipid":   "00000000-0000-0000-0000-000000000000",
    "templateid":       "00000000-0000-0000-0000-000000000000",
    "reportid":         "00000000-0000-0000-0000-000000000000",
    # Strings
    "graphfilter":      "startsWith(displayName, 'A')",
    "displayname":      "Example Display Name",
    "userprincipalname": "user@example.onmicrosoft.com",
    "mailnickname":     "user",
    "domain":           "example.onmicrosoft.com",
    "username":         "user",
    "mail":             "user@example.onmicrosoft.com",
    "endpoint":         "/users",
    # Booleans
    "enabled":          True,
    "disabled":         False,
    "mustchangepass":   False,
    "includelogondetails": "true",
    "nopagination":     "true",
    "skipcache":        "true",
    "asapp":            "false",
    "countonly":        "false",
    # Date-times
    "date":             "2026-01-15T00:00:00Z",
    "scheduledtime":    "2026-01-15T09:00:00Z",
}

# ── Type hints ────────────────────────────────────────────────────────────────
# Field name (lowercase) → OAS 3.1 schema fragment.
# Used by Stage 4 to emit richer schemas than the "string" default.
# $ref entries must be the sole key — Stage 4 wraps them in allOf when
# extensions need to be added alongside.
TYPE_HINTS: dict[str, dict] = {
    # Booleans
    "enabled":           {"type": "boolean"},
    "disabled":          {"type": "boolean"},
    "mustchangepass":    {"type": "boolean"},
    "removelicenses":    {"type": "boolean"},
    "accountenabled":    {"type": "boolean"},
    # Plain strings
    "id":                {"type": "string"},
    "userid":            {"type": "string"},
    "tenantid":          {"type": "string"},
    "graphfilter":       {"type": "string", "description": "OData $filter expression passed to Graph"},
    # Date-times
    "date":              {"type": "string", "format": "date-time"},
    "scheduledtime":     {"type": "string", "format": "date-time"},
    # Arrays of strings
    "businessphones":    {"type": "array", "items": {"type": "string"}},
    "othermails":        {"type": "array", "items": {"type": "string"}},
    "licenses":          {"type": "array", "items": {"type": "string"},
                          "description": "License SKU IDs to assign"},
    # addedAliases: newline-delimited string on the wire (server does $x -split '\n').
    # NOT an array — despite the name. AddSharedMailbox sidecar confirms this pattern.
    "addedaliases":      {"type": "string",
                          "description": "Additional SMTP aliases, newline-delimited (server splits on \\n)"},
    # Arrays of $ref objects
    "addtogroups":       {"type": "array", "items": {"$ref": "#/components/schemas/GroupRef"}},
    "removefromgroups":  {"type": "array", "items": {"$ref": "#/components/schemas/GroupRef"}},
    # LabelValue selects
    "primdomain":        {"$ref": "#/components/schemas/LabelValue"},
    "usagelocation":     {"$ref": "#/components/schemas/LabelValue"},
    "sherweblicense":    {"$ref": "#/components/schemas/LabelValue"},
    "setmanager":        {"$ref": "#/components/schemas/LabelValue"},
    "setsponsor":        {"$ref": "#/components/schemas/LabelValue"},
    "copyfrom":          {"$ref": "#/components/schemas/LabelValue"},
    "usertemplate":      {"$ref": "#/components/schemas/LabelValue"},
    # Structured objects
    "defaultattributes": {"$ref": "#/components/schemas/DynamicExtensionFields"},
    "customdata":        {"$ref": "#/components/schemas/DynamicExtensionFields"},
    "postexecution":     {"$ref": "#/components/schemas/PostExecution"},
    "scheduled":         {"$ref": "#/components/schemas/ScheduledTask"},
}

# ── Graph API field type hints ────────────────────────────────────────────────
# Graph API field name → OAS type descriptor.
# Used to infer response schemas from $select field lists.
# Keys are lowercased field names. Value format same as TYPE_HINTS:
#   "string" | "boolean" | "integer" | "number" | {"type": "array", ...} | {"type": "string", "format": "..."}
GRAPH_FIELD_TYPES: dict[str, str | dict] = {
    # Identifiers — always strings
    "id":                           "string",
    "appid":                        "string",
    "objectid":                     "string",
    "tenantid":                     "string",
    "userid":                       "string",
    "groupid":                      "string",
    "roleid":                       "string",
    "deviceid":                     "string",
    "subscriptionid":               "string",

    # Boolean flags — common naming patterns
    "accountenabled":               "boolean",
    "isenabled":                    "boolean",
    "isverified":                   "boolean",
    "isdefault":                    "boolean",
    "isinitial":                    "boolean",
    "iscompliant":                  "boolean",
    "ismanaged":                    "boolean",
    "isencrypted":                  "boolean",
    "isresourceaccount":            "boolean",
    "showinaddresslist":            "boolean",
    "onpremisessyncenabled":        "boolean",
    "dirsyncenabled":               "boolean",
    "mailenabled":                  "boolean",
    "securityenabled":              "boolean",
    "manualpagination":             "boolean",

    # DateTime fields — match suffix
    "createddatetime":              {"type": "string", "format": "date-time"},
    "lastmodifieddatetime":         {"type": "string", "format": "date-time"},
    "deleteddatetime":              {"type": "string", "format": "date-time"},
    "enrolleddatetime":             {"type": "string", "format": "date-time"},
    "registrationdatetime":         {"type": "string", "format": "date-time"},
    "lastsyncdatetime":             {"type": "string", "format": "date-time"},
    "onpremiseslastsyncdatetime":   {"type": "string", "format": "date-time"},
    "approximatelastsignindatetime":{"type": "string", "format": "date-time"},
    "startdatetime":                {"type": "string", "format": "date-time"},
    "enddatetime":                  {"type": "string", "format": "date-time"},
    "receiveddatetime":             {"type": "string", "format": "date-time"},

    # Array fields — collections
    "businessphones":               {"type": "array", "items": {"type": "string"}},
    "othermails":                   {"type": "array", "items": {"type": "string"}},
    "proxyaddresses":               {"type": "array", "items": {"type": "string"}},
    "grouptypes":                   {"type": "array", "items": {"type": "string"}},
    "supportedservices":            {"type": "array", "items": {"type": "string"}},
    "roles":                        {"type": "array", "items": {"type": "string"}},
    "resourceprovisioningoptions":  {"type": "array", "items": {"type": "string"}},
    "assignedlicenses":             {"type": "array", "items": {"type": "object", "additionalProperties": True}},
    "licenseassignmentstates":      {"type": "array", "items": {"type": "object", "additionalProperties": True}},
    "keycredentials":               {"type": "array", "items": {"type": "object", "additionalProperties": True}},
    "passwordcredentials":          {"type": "array", "items": {"type": "object", "additionalProperties": True}},
    "approles":                     {"type": "array", "items": {"type": "object", "additionalProperties": True}},
    "assignedplans":                {"type": "array", "items": {"type": "object", "additionalProperties": True}},
    "verifieddomains":              {"type": "array", "items": {"type": "object", "additionalProperties": True}},
    "ipranges":                     {"type": "array", "items": {"type": "object", "additionalProperties": True}},
    "countriesandregions":          {"type": "array", "items": {"type": "string"}},
    "approlemembers":               {"type": "array", "items": {"type": "object", "additionalProperties": True}},
    "torecipients":                 {"type": "array", "items": {"type": "object", "additionalProperties": True}},
    "ccrecipients":                 {"type": "array", "items": {"type": "object", "additionalProperties": True}},

    # Integer fields
    "consumedunits":                "integer",
    "sizeinbyte":                   "integer",
    "devicecount":                  "integer",
    "priority":                     "integer",

    # Object fields
    "manager":                      {"type": "object", "additionalProperties": True},
    "prepaidunits":                 {"type": "object", "additionalProperties": True},
    "status":                       {"type": "object", "additionalProperties": True},
    "passwordprofile":              {"type": "object", "additionalProperties": True},
    "from":                         {"type": "object", "additionalProperties": True},
    "body":                         {"type": "object", "additionalProperties": True},
    "location":                     {"type": "object", "additionalProperties": True},
    "vendorinformation":            {"type": "object", "additionalProperties": True},
    "windowsprotectionstate":       {"type": "object", "additionalProperties": True},
    "addedfields":                  {"type": "object", "additionalProperties": True},
}

# ── Always-required params ────────────────────────────────────────────────────
# These params are required on every endpoint that has them.
# Endpoint-specific required params belong in sidecars.
ALWAYS_REQUIRED_BODY:  set[str] = {"tenantfilter"}
ALWAYS_REQUIRED_QUERY: set[str] = {"tenantfilter"}

# ── Dynamic extension field parent names ──────────────────────────────────────
# Dotted-path params whose parent is one of these collapse to DynamicExtensionFields
# rather than generating a nested object schema.
DYNAMIC_EXTENSION_PARENTS: set[str] = {"defaultattributes", "customdata"}

# ── Known static analysis gaps ────────────────────────────────────────────────
# Patterns the scanner cannot resolve. Documented in stage output so gaps are
# explicit and traceable, never silent.
KNOWN_FRONTEND_GAPS: list[str] = [
    "Shared form components (e.g. CippAddEditUser) render different fields based on "
    "formType='add' vs formType='edit'. All fields in the file are attributed to all "
    "endpoints that file serves. Sidecar required to split by operation.",

    "CippFormUserSelector submits a LabelValue object. Scanner captures the field name; "
    "TYPE_HINTS maps known names (setManager, setSponsor, copyFrom) to LabelValue schema. "
    "Unknown selector names default to string.",

    "Dynamic extension fields: defaultAttributes.${attribute.label}.Value — field names "
    "are tenant-specific runtime data. Emitted as DynamicExtensionFields $ref.",

    "Direct axios.get / axios.post calls not wrapped in ApiGetCall/ApiPostCall.",
    "fetch() calls.",
    "Computed endpoint names: url: `/api/${endpointName}`.",

    "Specialised selector components: only CippFormDomainSelector, "
    "CippFormLicenseSelector, and CippFormUserSelector are scanned by name. "
    "Any new *Selector component added to the frontend will be invisible to Stage 2 "
    "until added to SELECTOR_COMPONENT_RES in stage2_frontend_scanner.py.",

    "HTTP method classification: POST is detected via ApiPostCall and .mutate. "
    "If new wrappers are introduced (ApiPatchCall, ApiDeleteCall, etc.) they will "
    "be classified as GET unless added to POST_CONTEXT_RE.",

    "CippFormPage postUrl= pattern: form fields from external child components "
    "(e.g. <CippAddGroupForm />) are not extracted — the component import is not "
    "followed. Endpoints using CippFormPage with an external form component are "
    "flagged has_external_form_component=true in Stage 2 output. Add a sidecar "
    "to document those contracts.",

    "customDataformatter prop on CippFormPage: field names and/or types sent to the "
    "API may differ from the form field names. Endpoints flagged has_data_transform=true "
    "in Stage 2 output require a sidecar to document the actual POST schema.",

    "Handler-constructed POST bodies: when a submit handler builds its own data object "
    "rather than submitting form values directly, the scanner cannot resolve param names. "
    "Endpoints flagged has_constructed_body=true in Stage 2 output require a sidecar.",
]

# ── Known URL aliases ────────────────────────────────────────────────────────
# !! THIS MAP SHOULD STAY EMPTY !!
#
# CIPP uses a single HTTP entrypoint (New-CippCoreRequest) that routes all calls
# via: $FunctionName = 'Invoke-{0}' -f $Request.Params.CIPPEndpoint
#
# This means the URL path segment IS the PS1 function name suffix, verbatim and
# case-sensitive. There is NO routing layer that maps ListX → ExecX.
# A frontend call to /api/ListNotificationConfig will return 404 because
# Invoke-ListNotificationConfig does not exist — the actual PS1 is
# Invoke-ExecNotificationConfig.
#
# Therefore: ORPHAN endpoints are not scanner artifacts — they are genuine
# production 404s. The alias map would hide real bugs. Do not populate it.
#
# If a new CIPP routing layer is introduced that does alias mapping, document
# it here and re-evaluate whether aliases belong in the scanner or in the API.
KNOWN_URL_ALIASES: dict[str, str] = {}

# ── Platform-level endpoints (never backed by a PS1) ─────────────────────────
# These endpoint names appear in frontend calls but are handled by the Azure
# Function host, middleware, or external services — not by Invoke-*.ps1 files.
# Excluded from ORPHAN classification entirely.
KNOWN_NON_PS1_ENDPOINTS: set[str] = {
    "me",  # Azure AD auth/profile endpoint — handled by MSAL/host middleware
}

# ── Coverage tier definitions ─────────────────────────────────────────────────
COVERAGE_TIERS: dict[str, str] = {
    "FULL":       "Both API and frontend sources present; all params high confidence",
    "PARTIAL":    "One source only, or mix of confidence levels, or blob-only access",
    "BLIND":      "API function found but zero params extracted (pure blob passthrough)",
    "ARRAY_BODY": "Body is an array of objects; required element fields detected, rest is open schema",
    "PASSTHRU":   "Endpoint proxies Graph; oneOf variants resolved (fully or partially)",
    "ORPHAN":     "Frontend calls endpoint but no Invoke-*.ps1 found",
    "STUB":       "No sources at all — sidecar-only documentation",
}

# ── Tag display names ─────────────────────────────────────────────────────────
# Maps the first folder-path segment (from the PS1 repo) to the UI label a
# CIPP user sees in the sidebar (CIPP/src/layouts/config.js nativeMenuItems).
# Only the first segment is renamed; sub-folder parts (e.g. "Autopilot", "MEM")
# are passed through unchanged as they match the UI sub-section labels.
TAG_DISPLAY_NAMES: dict[str, str] = {
    "Identity":         "Identity Management",
    "Tenant":           "Tenant Administration",
    "Security":         "Security & Compliance",
    "Endpoint":         "Intune",
    "Teams-Sharepoint": "Teams & SharePoint",
    "Email-Exchange":   "Email & Exchange",
    "Tools":            "Tools",
    "CIPP":             "CIPP",
}

# ── Tag ordering ──────────────────────────────────────────────────────────────
# Canonical section order matching the CIPP UI nav (nativeMenuItems display order).
# Swagger UI, ReDoc, and Scalar all render tag groups in the order they appear
# in the top-level `tags:` array. Sections not listed here sort to the end.
TAG_ORDER: list[str] = [
    "Identity Management",
    "Tenant Administration",
    "Security & Compliance",
    "Intune",
    "Teams & SharePoint",
    "Email & Exchange",
    "Tools",
    "CIPP",
    "Uncategorized",
]
