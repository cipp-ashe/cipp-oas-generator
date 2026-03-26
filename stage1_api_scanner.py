"""
Stage 1: API Scanner
Walks CIPP-API HTTP Functions tree, extracts endpoint metadata via regex/pattern analysis.
Outputs: out/endpoint-index.json

Why not a real PS AST parser? Python doesn't have one. We use targeted regex patterns
that match the specific idioms CIPP uses — validated against real endpoints.

Coverage tier assigned per endpoint:
  FULL    — params found from direct $Request.Query/Body access (high confidence)
  PARTIAL — params found via blob alias access (medium confidence)
  BLIND   — API function found, but zero params extracted (pure passthrough)
  ORPHAN  — (not assigned here; Stage 3 assigns this for frontend-only endpoints)
"""

import re
import json
import hashlib
import argparse
from pathlib import Path
from config import (
    API_REPO, HTTP_FUNCTIONS_ROOT, OUT_DIR, SIDECARS_DIR,
    DOWNSTREAM_PATTERNS, PARAM_NOISE, PS_NOISE, PASSTHRU_ENDPOINTS,
    HELPER_FUNCTION_IGNORE,
)

# ── Schema cache loading ───────────────────────────────────────────────────────
# Optional files built by fetch_schemas.py. If absent, downstream required
# inference and response schema resolution are silently skipped.
_SCHEMAS_DIR = Path(__file__).parent / "schemas"

def _load_schema(filename: str) -> dict:
    p = _SCHEMAS_DIR / filename
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

GRAPH_SCHEMA    = _load_schema("graph-required.json")
# graph-responses.json no longer loaded here — response schema assembly
# moved to stage_passthru_builder.py (registry + computed fields merge)
EXO_SCHEMA      = _load_schema("exo-required.json")

# ── Compiled patterns ─────────────────────────────────────────────────────────

# Named query param: $Request.Query.SomeName (case-insensitive)
QUERY_PARAM_RE = re.compile(r'\$[Rr]equest\.(?:Query|query)\.(\w+)')

# Direct body param: $Request.Body.SomeName
BODY_DIRECT_RE = re.compile(r'\$[Rr]equest\.(?:Body|body)\.(\w+)')

# Body blob assignment: $Alias = $Request.Body
# Captures the alias variable name only (not array index, not property access).
# Also handles cast forms used when iterating/inspecting the body:
#   $Alias = [pscustomobject]$Request.Body
#   $Alias = ($Request.Body -as [pscustomobject])
# These are functionally equivalent — the entire body is aliased to $Alias.
BODY_BLOB_RE = re.compile(
    r'^\s*\$(\w+)\s*=\s*'
    r'(?:\[pscustomobject\]\s*)?'       # optional [pscustomobject] cast
    r'\$[Rr]equest\.(?:Body|body)'
    r'(?:\s+-as\s+\[pscustomobject\])?' # optional -as [pscustomobject]
    r'\s*$',
    re.MULTILINE | re.IGNORECASE,
)

# Comment block metadata
ROLE_RE         = re.compile(r'\.ROLE\s*\r?\n\s*(.+)')
SYNOPSIS_RE     = re.compile(r'\.SYNOPSIS\s*\r?\n\s*(.+)')
DESCRIPTION_RE  = re.compile(r'\.DESCRIPTION\s*\r?\n((?:[ \t]*.+\r?\n)+)')
FUNCTIONALITY_RE = re.compile(r'\.FUNCTIONALITY\s*\r?\n\s*(.+)')

# Scheduled branch: body reads Scheduled.Enabled
SCHEDULED_BRANCH_RE = re.compile(
    r'\$(?:[Rr]equest\.(?:Body|body)|[A-Za-z]\w*)\.Scheduled\.(?:Enabled|enabled)\b'
)

# Dynamic options: Select-Object * -ExcludeProperty (open schema passthrough)
DYNAMIC_OPTIONS_RE = re.compile(r'Select-Object\s+\*\s+-ExcludeProperty', re.IGNORECASE)

# Dynamic extension fields: .defaultAttributes or .customData on any var
DYNAMIC_FIELDS_RE = re.compile(r'\$\w+\.(defaultAttributes|customData)\b', re.IGNORECASE)

# Value-unwrap pattern: $x.field.value ?? $x.field  OR  $x.field.value ? ... : $x.field
VALUE_WRAPPED_RE = re.compile(
    r'\$\w+\.(\w+)\.value\s*(?:\?\?|\?)\s*\$\w+\.\1\b', re.IGNORECASE
)

# HTTP method hints in Graph calls
PATCH_RE  = re.compile(r'["\']PATCH["\']|method\s*=\s*["\']PATCH["\']', re.IGNORECASE)
DELETE_RE = re.compile(r'["\']DELETE["\']|method\s*=\s*["\']DELETE["\']', re.IGNORECASE)

# foreach loop over a body sub-array: foreach ($Item in $Request.Body.someArray)
# Captures the loop variable name and the sub-array property name.
# The loop var is registered as a blob alias so $Item.field accesses are extracted.
FOREACH_BODY_RE = re.compile(
    r'foreach\s*\(\s*\$(\w+)\s+in\s+\$[Rr]equest\.(?:Body|body)\.(\w+)\s*\)',
    re.IGNORECASE
)

# Array body: $Request.Body -is [array] or foreach ($x in $Request.Body) (no sub-property)
# When the entire body is an array of objects rather than a single object,
# direct field access like $Request.Body.fieldname won't appear — it'll be
# $item.id / $item.tenantFilter after iterating. Detect this so Stage 1 can
# emit ARRAY_BODY coverage rather than BLIND.
ARRAY_BODY_RE = re.compile(
    r'\$[Rr]equest\.(?:Body|body)\s+-is\s+\[array\]'
    r'|foreach\s*\(\s*\$\w+\s+in\s+\$[Rr]equest\.(?:Body|body)\s*\)',
    re.IGNORECASE,
)

# Required array-item fields detected from Where-Object null/empty validation:
#   $Items | Where-Object { [string]::IsNullOrWhiteSpace($_.fieldName) }
# Captures fieldName as a required param on each array element.
ARRAY_ITEM_REQUIRED_RE = re.compile(
    r'IsNullOrWhiteSpace\s*\(\s*\$_\.(\w+)\s*\)',
    re.IGNORECASE,
)

# Array item field access: $item.fieldName (where $item is loop var over $Request.Body)
# Captured separately per loop var via extract_blob_params — this just detects
# the array-level loop var when iterating the body directly (no sub-property).
ARRAY_BODY_LOOPVAR_RE = re.compile(
    r'foreach\s*\(\s*\$(\w+)\s+in\s+\$[Rr]equest\.(?:Body|body)\s*\)',
    re.IGNORECASE,
)

# Pure passthrough: function just calls another Invoke- or CIPP function and returns
PASSTHROUGH_RE = re.compile(r'Invoke-CIPPStandardsRun|New-CIPPUserTask|Set-CIPPUser\b', re.IGNORECASE)

_DOWNSTREAM_COMPILED = {
    svc: re.compile(pat, re.IGNORECASE)
    for svc, pat in DOWNSTREAM_PATTERNS.items()
}

# ── Required inference patterns ───────────────────────────────────────────────

# Validation: IsNullOrWhiteSpace / IsNullOrEmpty in a direct if-condition on a field.
# Matches: if ([string]::IsNullOrWhiteSpace($Alias.fieldName))
#          if ([string]::IsNullOrEmpty($Request.Body.fieldName))
# Note: ARRAY_ITEM_REQUIRED_RE already handles $_.field (array-body Where-Object).
REQUIRED_VALIDATION_RE = re.compile(
    r'if\s*\(\s*(?:\[string\]::)?IsNullOr(?:WhiteSpace|Empty)\s*\(\s*\$\w+\.(\w+)\s*\)',
    re.IGNORECASE,
)

# Downstream extraction: Graph POST/GET URI + optional -type argument.
# Matches single-line and backtick-continued calls (content is pre-joined).
# Two URI capture groups — double-quoted (1) and single-quoted (2) — so that a
# single quote inside a double-quoted URI (e.g. appId='...') is not treated as
# a string terminator. Group (3) is the optional -type value.
GRAPH_CALL_URI_RE = re.compile(
    r"""New-Graph(?:Post|Get)Request\b.*?-uri\s+(?:"(https://graph\.microsoft\.com/[^"]+)"|'(https://graph\.microsoft\.com/[^']+)')"""
    r"""(?:.*?-type\s+["']?(\w+)["']?)?""",
    re.IGNORECASE,
)

# Extract -select parameter value from Graph calls.
# Matches: -select "field1,field2" or -select 'field1,field2'
GRAPH_SELECT_RE = re.compile(
    r'New-Graph(?:Post|Get)Request\b[^;\n]*?-select\s+["\']([^"\']+)["\']',
    re.IGNORECASE,
)

# Detect when Graph URI comes from a variable (passthru indicator).
# Matches: $Request.Query.Endpoint, $Request.Body.Endpoint, or $Endpoint variable
PASSTHRU_URI_SOURCE_RE = re.compile(
    r'\$Request\.(?:Query|Body)\.Endpoint|\$Endpoint\b',
    re.IGNORECASE,
)

# Template for detecting conditional branches on a specific param.
# Used by _detect_dual_mode to find: if ($ParamName) or if ($Request.Query.ParamName)
CONDITIONAL_PARAM_RE_TEMPLATE = r'if\s*\(\s*\$(?:Request\.Query\.)?{param}\s*\)'

ADD_MEMBER_RE = re.compile(
    r'Add-Member\s+(?:'
    r'-MemberType\s+NoteProperty\s+-Name\s+["\'](\w+)["\'].*?-Value\s+([^-\n]+)'
    r'|'
    r'-NotePropertyName\s+["\'](\w+)["\'].*?-NotePropertyValue\s+([^-\n]+)'
    r')',
    re.IGNORECASE,
)

SELECT_COMPUTED_RE = re.compile(
    r"@\{\s*Name\s*=\s*['\"](\w+)['\"]\s*;\s*Expression\s*=\s*\{([^}]+)\}",
    re.IGNORECASE,
)

# Helper function calls: matches CIPP-prefixed verb-noun calls (e.g. Set-CIPPFoo, Get-CIPPBar).
# Anchored to a word boundary before the verb so it does not match mid-string.
# Excludes Invoke- prefix (those are entrypoints, not helpers).
HELPER_FUNCTION_RE = re.compile(
    r'\b((?:Get|Set|New|Add|Remove|Invoke|Update|Enable|Disable|Start|Stop|Test|Reset|Send|Push|Pop|Out|Convert|Export|Import|Select|Format|Write|Read|Measure|Compare|Copy|Move|Rename|Clear|Find|Register|Unregister|Request|Confirm|Deny|Approve|Grant|Revoke|Publish|Unpublish|Deploy|Connect|Disconnect|Sync|Backup|Restore|Install|Uninstall|Enable|Disable|Suspend|Resume|Wait|Lock|Unlock|Mount|Dismount|Open|Close|Join|Split|Merge|Watch|Protect|Unprotect|Assert|Debug|Trace|Show|Hide|Block|Unblock)-CIPP\w+)\b',
    re.IGNORECASE,
)

# Body property map: hash key assignment → PS alias.field
# Matches: displayName = $UserObj.displayName
#          'ExternalEmailAddress' = $obj.email
# Anchored to start-of-line (after whitespace) to avoid matching variable assignments.
BODY_PROP_MAP_RE = re.compile(
    r"^\s+'?(\w+)'?\s*=\s*\$\w+\.(\w+)\b",
    re.IGNORECASE | re.MULTILINE,
)

# EXO cmdlet name extraction from New-ExoRequest calls (single-line + joined).
# Captures: cmdlet_name
EXOCMDLET_RE = re.compile(
    r"New-ExoRequest\b.*?-cmdlet\s+'([^']+)'",
    re.IGNORECASE,
)

# Response transform detection — suppress inferred response schema when the PS1
# pipes the Graph response through a shaping cmdlet.
RESPONSE_TRANSFORM_RE = re.compile(
    r'\|\s*(?:Select-Object|ForEach-Object|Where-Object|Sort-Object|Group-Object)\b',
    re.IGNORECASE,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def file_hash(path: Path) -> str:
    """SHA256 of file contents — used to detect staleness between runs."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def folder_to_tag(path: Path) -> list[str]:
    """
    Derive UI tag segments from the PS1 file path.

    Strips leading structural directories that aren't meaningful domain segments:
      - "Entrypoints"    — the primary HTTP entrypoint directory
      - "HTTP Functions" — a subdirectory inside Entrypoints used by most endpoints

    Examples (relative to Modules/CIPPCore/Public/):
      Entrypoints/HTTP Functions/Identity/Administration/Users/Invoke-X.ps1
        → ['Identity', 'Administration', 'Users']
      Entrypoints/Invoke-X.ps1           → []
      CippQueue/Invoke-ListCippQueue.ps1  → ['CippQueue']
      Webhooks/Invoke-RemoveWebhookAlert.ps1 → ['Webhooks']
    """
    parts = path.relative_to(HTTP_FUNCTIONS_ROOT).parts
    for prefix in ("Entrypoints", "HTTP Functions"):
        if parts and parts[0] == prefix:
            parts = parts[1:]
    return list(parts[:-1]) if len(parts) > 1 else []


def infer_http_method(content: str, has_query: bool, has_body: bool) -> list[str]:
    """
    Infer HTTP method(s) from access patterns and explicit Graph call hints.
    Returns a list (usually one element) — sidecar can override if wrong.
    """
    if PATCH_RE.search(content):
        return ["PATCH"]
    if DELETE_RE.search(content):
        return ["DELETE"]
    if has_body:
        return ["POST"]
    if has_query or not has_body:
        return ["GET"]
    return ["GET"]


def extract_blob_params(content: str, aliases: list[str]) -> list[dict]:
    """
    For each blob alias ($Alias = $Request.Body), find all $Alias.Field accesses.
    Filters PS noise. Handles LabelValue unwrap pattern.

    LabelValue unwrap: when the API accesses $alias.field.value (and never $alias.field
    directly), that means 'field' is a LabelValue — the API is unwrapping it. We emit
    'field' with type=LabelValue rather than emitting 'field.value' or skipping entirely.

    If both $alias.field and $alias.field.value appear, the parent ($alias.field) wins
    and .value is suppressed (collapse_value_paths in Stage 3 also handles this).
    """
    seen: set[str] = set()
    # First pass: collect everything (field paths → raw access counts)
    # IGNORECASE: PowerShell variables are case-insensitive — $Profbod and $ProfBod
    # are the same variable. The alias is captured from the assignment site (one casing)
    # but accessed with potentially different casing elsewhere in the function.
    raw: list[tuple[str, str]] = []  # (field_path, alias)
    for alias in aliases:
        pat = re.compile(rf'\${re.escape(alias)}\.(\w+(?:\.\w+)*)\b', re.IGNORECASE)
        for m in pat.finditer(content):
            raw.append((m.group(1), alias))

    # Index: which top-level field names appear without .value suffix
    bare_fields: set[str] = set()
    for field_path, _ in raw:
        top = field_path.split(".")[0].lower()
        if top in PS_NOISE:
            continue
        if "." not in field_path:
            bare_fields.add(field_path.lower())

    params: list[dict] = []
    for field_path, _ in raw:
        top = field_path.split(".")[0].lower()
        if top in PS_NOISE:
            continue

        fp_lower = field_path.lower()

        # $alias.field.value — LabelValue unwrap pattern
        if fp_lower.endswith(".value"):
            parent_lower = fp_lower[: -len(".value")]
            parent_name  = field_path[: -len(".value")]  # preserve original casing
            if parent_lower in seen or parent_lower in bare_fields:
                continue  # parent already emitted bare — suppress .value artifact
            if parent_lower not in seen:
                seen.add(parent_lower)
                params.append({
                    "name":       parent_name,
                    "in":         "body",
                    "confidence": "medium",
                    "source":     "ast_blob",
                    "type":       "LabelValue",
                })
            continue

        # $alias.field.label — always noise (LabelValue display string, never sent separately)
        if fp_lower.endswith(".label"):
            continue

        key = fp_lower
        if key not in seen:
            seen.add(key)
            params.append({
                "name":       field_path,
                "in":         "body",
                "confidence": "medium",
                "source":     "ast_blob",
            })
    return params


def determine_coverage(
    has_query: bool,
    has_body_direct: bool,
    blob_aliases: list[str],
    body_params: list[dict],
    is_passthrough: bool,
    is_array_body: bool = False,
) -> str:
    """Assign coverage tier based on what Stage 1 was able to observe."""
    if is_array_body:
        # Body is an array of objects — schema is per-element, not per-field at root.
        # Required element fields may be extractable; rest is open.
        return "ARRAY_BODY"
    if not has_query and not has_body_direct and not body_params:
        return "BLIND"  # function exists but we saw nothing readable
    if has_body_direct and not blob_aliases:
        return "FULL"   # all params named directly
    if blob_aliases and has_body_direct:
        return "PARTIAL"  # mix of direct + blob
    if blob_aliases and not has_body_direct:
        return "PARTIAL"  # only blob access — medium confidence throughout
    return "FULL"


def _join_continued_lines(content: str) -> str:
    """Join PowerShell backtick-continued lines so single-line patterns match multi-line calls."""
    return re.sub(r'`\s*\r?\n\s*', ' ', content)


def _normalize_graph_uri(uri: str) -> str:
    """
    Normalize a raw Graph URI string to a schema-cache lookup key.

    Steps:
      1. Strip base URL + version: https://graph.microsoft.com/beta/ → /
      2. Detect collection-mode OData params ($top, $filter, $count) BEFORE stripping
         the query string — these are exclusive to collection calls and indicate that a
         trailing optional variable (e.g. $($userid)) should be dropped so the path
         resolves to the collection endpoint rather than a single-item endpoint.
         ($select, $expand, $orderby apply to both single and collection, so ignored.)
      3. Strip OData query string (?$filter=..., ?$select=...)
      4. Replace $($Alias.field) interpolations → {field}
      5. Replace bare $Variable interpolations → {variable}
      6. Collapse OData function value args: (key='$val') → /{id}
      7. If collection hints were found and the path ends with a single /{varname},
         strip that trailing segment — the variable is an optional filter, not a
         mandatory resource selector.
    """
    # Strip base + version
    path = re.sub(
        r'^https://graph\.microsoft\.com/(?:beta|v1\.0)/',
        '/', uri, flags=re.IGNORECASE
    )
    # Detect collection-only OData params in the query string.
    # PowerShell backtick-escapes $ in double-quoted strings: ?`$top=  or  ?$top=
    _is_collection = bool(re.search(r'[?&]`?\$?(top|filter|count)=', path, re.IGNORECASE))
    # Strip OData query string
    path = path.split('?')[0].rstrip('/')
    # Strip PowerShell backtick escapes (e.g. `$ref → $ref before variable substitution)
    path = path.replace('`', '')
    # Replace $(expression) interpolations → {last_segment}
    # Handles: $($Alias.field), $($Request.Query.field), $($varname) (no dot)
    def _sub_interp(m: re.Match) -> str:
        inner = m.group(1).lstrip('$')   # strip leading $
        return '{' + inner.split('.')[-1].lower() + '}'
    path = re.sub(r'\$\(\s*(\$?\w+(?:\.\w+)*)\s*\)', _sub_interp, path)
    # Replace remaining bare $Variable with {variable}
    path = re.sub(r'\$(\w+)', lambda m: '{' + m.group(1).lower() + '}', path)
    # Collapse OData function value args like (appId='value') → /{id}
    path = re.sub(r"\([^)]+='[^']+'\)", '/{id}', path)
    if not path.startswith('/'):
        path = '/' + path
    # Drop a trailing /{varname} when the URI was clearly a collection call — the
    # variable was an optional ID filter, not a mandatory resource selector.
    # Strip ALL consecutive trailing {variable} segments for collection calls.
    # e.g. /groups/{groupid}/{members}?$top=999 → /groups
    # e.g. /users/{userid}?$top=999            → /users
    # Sub-resource collections like /users/{id}/managedDevices?$top=999 are safe:
    # "managedDevices" is a literal, not a variable, so the loop stops there.
    if _is_collection:
        while re.search(r'/\{[^}]+\}$', path):
            path = re.sub(r'/\{[^}]+\}$', '', path)
    return path


def _extract_graph_calls(joined: str) -> list[dict]:
    """
    Extract Graph API calls from PS1 content (backtick-joined).
    Returns list of {path: str, method: str}.
    """
    calls: list[dict] = []
    seen:  set[str]   = set()
    for m in GRAPH_CALL_URI_RE.finditer(joined):
        uri_str   = m.group(1) or m.group(2)   # group 1 = double-quoted, group 2 = single-quoted
        type_arg  = m.group(3)
        # Infer method: explicit -type wins; else POST for PostRequest, GET for GetRequest
        if type_arg:
            method = type_arg.upper()
        elif 'New-GraphPostRequest' in m.group(0)[:30]:
            method = 'POST'
        else:
            method = 'GET'
        path = _normalize_graph_uri(uri_str)
        key  = f"{method} {path}"
        if key not in seen:
            seen.add(key)
            calls.append({"path": path, "method": method})
    return calls


def _extract_body_prop_map(content: str) -> dict[str, str]:
    """
    Extract hash key → cippParam mapping from @{} body construction blocks.
    e.g.  displayName = $UserObj.displayName  →  {"displayname": "displayName"}
          'ExternalEmailAddress' = $obj.email  →  {"externalemailaddress": "email"}
    Keys are lowercased for case-insensitive matching; values keep original case.
    """
    prop_map: dict[str, str] = {}
    for m in BODY_PROP_MAP_RE.finditer(content):
        graph_prop = m.group(1)
        cipp_param = m.group(2)
        if graph_prop.lower() in PS_NOISE or graph_prop.lower() in PARAM_NOISE:
            continue
        prop_map[graph_prop.lower()] = cipp_param
    return prop_map


def _extract_exo_calls(joined: str) -> list[str]:
    """Extract EXO cmdlet names from New-ExoRequest calls (deduplicated, original casing)."""
    return list(dict.fromkeys(EXOCMDLET_RE.findall(joined)))


def _extract_select_param(content: str, graph_path: str) -> str | None:
    """Extract the -select string associated with a Graph call to the given path."""
    for m in GRAPH_SELECT_RE.finditer(content):
        call_text = m.group(0)
        if graph_path.lstrip("/") in call_text or graph_path in call_text:
            return m.group(1)
    # Fallback: if only one select in the file and one graph call, associate them
    selects = GRAPH_SELECT_RE.findall(content)
    if len(selects) == 1:
        return selects[0]
    return None


def _detect_passthru_uri_sources(content: str) -> list[str]:
    """Detect if Graph URIs come from request variables (passthru pattern)."""
    return PASSTHRU_URI_SOURCE_RE.findall(content)


def _build_downstream_calls(
    graph_calls: list[dict],
    exo_calls: list[str],
    select_map: dict[str, str | None],
    is_passthru: bool,
) -> list[dict]:
    """Build the spec-format downstream_calls list."""
    result: list[dict] = []
    for call in graph_calls:
        result.append({
            "service": "graph",
            "method": call["method"],
            "url_pattern": call["path"],
            "select": select_map.get(call["path"]),
            "is_passthru": is_passthru,
        })
    for cmdlet in exo_calls:
        result.append({
            "service": "exo",
            "cmdlet": cmdlet,
            "is_passthru": False,
        })
    return result


def _detect_dual_mode(
    query_params: list[dict],
    content: str,
    graph_calls: list[dict],
    endpoint_name: str | None = None,
) -> dict | None:
    """
    Detect if an endpoint has dual-mode behavior (collection vs single-item).

    Two detection paths:
    A. Explicit branch: if ($param) { single } else { collection }
    B. Inline URL: /resource/$($param) — when param is empty, Graph returns collection

    Both require:
    1. An ID-like query param (name ends in ID/Id/GUID)
    2. The param (or its variable alias) interpolated in a Graph URL

    Returns {discriminator_param, collection_graph_path, single_item_graph_path} or None.
    """
    if endpoint_name and endpoint_name in PASSTHRU_ENDPOINTS:
        return None

    id_param_re = re.compile(r'(?:id|guid)$', re.IGNORECASE)
    id_params = [p for p in query_params if id_param_re.search(p["name"])]
    if not id_params:
        return None

    for param in id_params:
        pname = param["name"]

        # Find variable aliases: $varname = $Request.Query.ParamName
        aliases = {pname}  # include the param name itself
        alias_re = re.compile(
            rf'\$(\w+)\s*=\s*\$Request\.Query\.{re.escape(pname)}\b',
            re.IGNORECASE,
        )
        for m in alias_re.finditer(content):
            aliases.add(m.group(1))

        # Signal: Graph URL interpolation of param or any alias
        graph_interp_found = False
        for alias in aliases:
            interp_re = re.compile(
                rf'\$\(\s*\${re.escape(alias)}\s*\)|\$\b{re.escape(alias)}\b',
                re.IGNORECASE,
            )
            for m in interp_re.finditer(content):
                start = max(0, m.start() - 200)
                window = content[start:m.end()]
                if re.search(r'New-Graph(?:Get|Post|Bulk)Request|graph\.microsoft\.com', window, re.IGNORECASE):
                    graph_interp_found = True
                    break
            if graph_interp_found:
                break

        if not graph_interp_found:
            # Also check: conditional on param without direct interpolation
            # (e.g., param controls a code path that builds different requests)
            cond_re = re.compile(
                CONDITIONAL_PARAM_RE_TEMPLATE.format(param=re.escape(pname)),
                re.IGNORECASE,
            )
            # Also check aliases in conditionals
            has_conditional = bool(cond_re.search(content))
            if not has_conditional:
                for alias in aliases:
                    alias_cond = re.compile(
                        rf'if\s*\(\s*\${re.escape(alias)}\s*\)',
                        re.IGNORECASE,
                    )
                    if alias_cond.search(content):
                        has_conditional = True
                        break
            if not has_conditional:
                continue
            # Has conditional but no Graph interpolation — check if any Graph
            # call path contains the {param} placeholder (already normalized)
            if not any('{' in c.get('path', '') for c in graph_calls):
                continue

        # Derive collection and single-item paths
        collection_path = None
        for call in graph_calls:
            path = call["path"]
            if '{' not in path:
                collection_path = path
                break
        if not collection_path:
            for call in graph_calls:
                path = call["path"]
                if '{' in path:
                    collection_path = re.sub(r'/\{[^}]+\}$', '', path)
                    break
        if not collection_path:
            continue

        single_item_path = f"{collection_path}/{{{pname.lower()}}}"

        return {
            "discriminator_param": pname,
            "collection_graph_path": collection_path,
            "single_item_graph_path": single_item_path,
        }

    return None


def _infer_type_from_value(value_str: str) -> str:
    v = value_str.strip()
    if re.search(r'\$true|\$false', v, re.IGNORECASE):
        return "boolean"
    if re.search(r'\[int\]|\.Count\b|\.Length\b', v, re.IGNORECASE):
        return "integer"
    if re.search(r'@\(', v):
        return "array"
    return "string"


def _extract_computed_fields(content: str) -> list[dict]:
    fields: list[dict] = []
    seen: set[str] = set()

    for m in ADD_MEMBER_RE.finditer(content):
        name = m.group(1) or m.group(3)
        value = m.group(2) or m.group(4) or ""
        if name and name.lower() not in seen:
            seen.add(name.lower())
            fields.append({
                "name": name,
                "type": _infer_type_from_value(value),
                "source": "ps1_add_member",
            })

    for m in SELECT_COMPUTED_RE.finditer(content):
        name = m.group(1)
        expr = m.group(2) or ""
        if name and name.lower() not in seen:
            seen.add(name.lower())
            fields.append({
                "name": name,
                "type": _infer_type_from_value(expr),
                "source": "ps1_select_computed",
            })

    return fields


def _extract_helper_function_names(content: str) -> list[str]:
    """
    Extract unique CIPP helper function names called in PS1 content.

    Only returns names that are not in HELPER_FUNCTION_IGNORE.
    The list is deduplicated while preserving first-seen order.
    """
    seen: set[str] = set()
    result: list[str] = []
    for m in HELPER_FUNCTION_RE.finditer(content):
        name = m.group(1)
        if name in HELPER_FUNCTION_IGNORE:
            continue
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


def _resolve_helper_file(func_name: str, repo_root: Path) -> "Path | None":
    """
    Locate the PS1 file for a CIPP helper function under repo_root/Modules/CIPPCore/Public/.

    Searches recursively. Returns the first match or None if not found.
    """
    search_root = repo_root / "Modules" / "CIPPCore" / "Public"
    if not search_root.exists():
        return None
    target = f"{func_name}.ps1"
    for p in search_root.rglob("*.ps1"):
        if p.name == target:
            return p
    return None


def _scan_helper(
    func_name: str,
    repo_root: Path,
    visited: set,
) -> "dict | None":
    """
    Recursively scan a CIPP helper function file.

    Returns a dict with keys:
      - graph_calls:      list[dict]  — from _extract_graph_calls
      - exo_calls:        list[str]   — from _extract_exo_calls
      - computed_fields:  list[dict]  — from _extract_computed_fields
      - nested_helpers:   list[dict]  — results of recursive _scan_helper calls

    Returns None if the file cannot be found or has already been visited
    (circular-reference protection via the shared `visited` set).
    """
    if func_name in visited:
        return None
    visited.add(func_name)

    helper_path = _resolve_helper_file(func_name, repo_root)
    if helper_path is None:
        return None

    raw_content = helper_path.read_text(encoding="utf-8", errors="ignore")
    joined = _join_continued_lines(raw_content)

    # Inline variable URI assignments so GRAPH_CALL_URI_RE can match them.
    # Handles: $URI = "https://graph.microsoft.com/..."
    # followed by: New-GraphPOSTRequest -uri $URI ...
    # We replace $VARNAME with the literal URI string in Graph call lines.
    _uri_var_re = re.compile(
        r'\$(\w+)\s*=\s*"(https://graph\.microsoft\.com/[^"]+)"',
        re.IGNORECASE,
    )
    uri_vars: dict[str, str] = {}
    for m in _uri_var_re.finditer(joined):
        uri_vars[m.group(1).lower()] = m.group(2)

    if uri_vars:
        def _inline_uri_var(line: str) -> str:
            """Replace $VAR with its URI literal in Graph call lines."""
            if not re.search(r'New-Graph(?:Post|Get)Request', line, re.IGNORECASE):
                return line
            def _sub(m: re.Match) -> str:
                vname = m.group(1).lower()
                if vname in uri_vars:
                    return f'"{uri_vars[vname]}"'
                return m.group(0)
            return re.sub(r'\$(\w+)', _sub, line)

        joined = "\n".join(_inline_uri_var(ln) for ln in joined.splitlines())

    graph_calls = _extract_graph_calls(joined)

    # Fallback: extract Graph URIs from string literals anywhere in the file.
    # Handles patterns like: switch { 'User' { "https://graph.microsoft.com/..." } }
    # where the URI isn't on the same line as New-Graph*Request.
    if not graph_calls:
        _GRAPH_URI_LITERAL_RE = re.compile(
            r'"(https://graph\.microsoft\.com/(?:v1\.0|beta)/[^"]+)"',
            re.IGNORECASE,
        )
        seen_paths: set[str] = set()
        for m in _GRAPH_URI_LITERAL_RE.finditer(joined):
            path = _normalize_graph_uri(m.group(1))
            if path not in seen_paths:
                seen_paths.add(path)
                # Infer method from context: check for -type PATCH/DELETE nearby
                start = max(0, m.start() - 300)
                window = joined[start:m.end() + 200]
                method = "GET"
                if re.search(r'New-GraphPOSTRequest', window, re.IGNORECASE):
                    type_match = re.search(r'-type\s+["\']?(\w+)', window, re.IGNORECASE)
                    method = type_match.group(1).upper() if type_match else "POST"
                graph_calls.append({"path": path, "method": method})

    exo_calls = _extract_exo_calls(joined)
    computed_fields = _extract_computed_fields(raw_content)

    nested_helpers: list[dict] = []
    for nested_name in _extract_helper_function_names(raw_content):
        nested_result = _scan_helper(nested_name, repo_root, visited)
        if nested_result is not None:
            nested_helpers.append(nested_result)

    return {
        "function":        func_name,
        "graph_calls":     graph_calls,
        "exo_calls":       exo_calls,
        "computed_fields": computed_fields,
        "nested_helpers":  nested_helpers,
    }


def _resolve_downstream_required(
    params: list[dict],
    graph_calls: list[dict],
    exo_calls: list[str],
    body_prop_map: dict[str, str],
) -> dict[str, str]:
    """
    Cross-reference downstream calls against schema caches.
    Returns dict of {param_name_lower → required_source} for params inferred as required.
    """
    required: dict[str, str] = {}
    param_names_lower = {p["name"].lower() for p in params}

    # Graph schema lookup
    for call in graph_calls:
        key = f"{call['method']} {call['path']}"
        for graph_prop in GRAPH_SCHEMA.get(key, []):
            gp_lower = graph_prop.lower()
            # Direct name match
            if gp_lower in param_names_lower:
                required[gp_lower] = "graph_schema"
            # Reverse body map: graphProp → cippParam
            cipp = body_prop_map.get(gp_lower)
            if cipp and cipp.lower() in param_names_lower:
                required[cipp.lower()] = "graph_schema"

    # EXO schema lookup
    for cmdlet in exo_calls:
        exo_req = EXO_SCHEMA.get(cmdlet) or EXO_SCHEMA.get(cmdlet.lower(), [])
        for exo_param in exo_req:
            ep_lower = exo_param.lower()
            if ep_lower in param_names_lower:
                required[ep_lower] = "exo_schema"
            cipp = body_prop_map.get(ep_lower)
            if cipp and cipp.lower() in param_names_lower:
                required[cipp.lower()] = "exo_schema"

    return required


def scan_endpoint(ps1_path: Path) -> dict | None:
    """
    Scan a single Invoke-*.ps1 file.
    Returns structured metadata dict, or None if file should be skipped.
    """
    content = ps1_path.read_text(encoding="utf-8", errors="ignore")
    stem     = ps1_path.stem                     # Invoke-EditUser
    endpoint = stem.removeprefix("Invoke-")      # EditUser

    # ── Entrypoint gate ───────────────────────────────────────────────────
    fn_m = FUNCTIONALITY_RE.search(content)
    _in_legacy_entrypoints_dir = (
        HTTP_FUNCTIONS_ROOT / "Entrypoints"
    ).resolve() in ps1_path.resolve().parents
    if fn_m:
        if "entrypoint" not in fn_m.group(1).lower():
            return None  # Activity trigger, helper, etc. — not an HTTP endpoint
    else:
        # No .FUNCTIONALITY block: allow through only for files in the legacy
        # Entrypoints/ subtree (older PS1 style). Files in other directories
        # (Tests/, Standards/, CustomData/, etc.) must declare Entrypoint explicitly.
        if not _in_legacy_entrypoints_dir:
            return None

    # ── Comment block metadata ────────────────────────────────────────────
    role_m  = ROLE_RE.search(content)
    syn_m   = SYNOPSIS_RE.search(content)
    desc_m  = DESCRIPTION_RE.search(content)

    # ── Query params ──────────────────────────────────────────────────────
    query_raw = {
        p for p in QUERY_PARAM_RE.findall(content)
        if p.lower() not in PARAM_NOISE
    }
    query_params = [
        {"name": p, "in": "query", "confidence": "high", "source": "ast_direct"}
        for p in sorted(query_raw)
    ]

    # ── Direct body params ────────────────────────────────────────────────
    body_direct_raw = {
        p for p in BODY_DIRECT_RE.findall(content)
        if p.lower() not in PARAM_NOISE
    }
    body_direct = [
        {"name": p, "in": "body", "confidence": "high", "source": "ast_direct"}
        for p in sorted(body_direct_raw)
    ]

    # ── Blob aliases (deduplicated) ───────────────────────────────────────
    blob_aliases = list(dict.fromkeys(BODY_BLOB_RE.findall(content)))

    # ── Array body detection: $Request.Body -is [array] ──────────────────
    # When the whole body is an array (not a single object), direct $Request.Body.field
    # access won't appear. Detect this so coverage tier can be ARRAY_BODY, not BLIND.
    is_array_body = bool(ARRAY_BODY_RE.search(content))
    array_item_required_fields: list[dict] = []
    array_body_loopvars: list[str] = []
    if is_array_body:
        # Detect loop var from foreach ($item in $Request.Body) for blob-style extraction
        for m in ARRAY_BODY_LOOPVAR_RE.finditer(content):
            lv = m.group(1)
            if lv not in array_body_loopvars:
                array_body_loopvars.append(lv)
            if lv not in blob_aliases:
                blob_aliases.append(lv)
        # Extract required fields from Where-Object validation (e.g. id, tenantFilter)
        for field in ARRAY_ITEM_REQUIRED_RE.findall(content):
            if field.lower() not in PARAM_NOISE and field.lower() not in PS_NOISE:
                array_item_required_fields.append({
                    "name":        field,
                    "in":          "body",
                    "confidence":  "high",
                    "source":      "ast_array_item_required",
                    "description": "Required field on each array element (validated in handler).",
                })

    # ── foreach loop aliases: foreach ($Item in $Request.Body.someArray) ──
    # The loop var acts as a blob alias for the sub-array element shape.
    # The array property itself (e.g. "bulkSites") is added as a direct body param.
    foreach_matches = FOREACH_BODY_RE.findall(content)
    foreach_array_params: list[dict] = []
    for loop_var, array_prop in foreach_matches:
        if loop_var not in blob_aliases:
            blob_aliases.append(loop_var)
        # Record the array container param (e.g. bulkSites) as a direct body param
        if array_prop.lower() not in PARAM_NOISE and array_prop.lower() not in PS_NOISE:
            foreach_array_params.append({
                "name":       array_prop,
                "in":         "body",
                "confidence": "high",
                "source":     "ast_foreach_array",
                "description": f"Array of objects. Each element accessed via ${loop_var}.<field>.",
            })

    # ── Blob-accessed body params ─────────────────────────────────────────
    body_blob = extract_blob_params(content, blob_aliases)

    # Merge: direct wins over blob for same name; foreach array params are high-confidence.
    # array_item_required_fields (from Where-Object validation) are high-confidence element params.
    direct_names = {p["name"].lower() for p in body_direct}
    array_req_names = {p["name"].lower() for p in array_item_required_fields}
    body_params = (
        body_direct
        + [p for p in array_item_required_fields if p["name"].lower() not in direct_names]
        + [p for p in foreach_array_params if p["name"].lower() not in direct_names
           and p["name"].lower() not in array_req_names]
        + [p for p in body_blob if p["name"].lower() not in direct_names
           and p["name"].lower() not in array_req_names
           and p["name"].lower() not in {f["name"].lower() for f in foreach_array_params}]
    )

    # ── Pattern flags ─────────────────────────────────────────────────────
    has_scheduled_branch = bool(SCHEDULED_BRANCH_RE.search(content))
    has_dynamic_options  = bool(DYNAMIC_OPTIONS_RE.search(content))
    dynamic_fields       = list({m.lower() for m in DYNAMIC_FIELDS_RE.findall(content)})
    value_wrapped_fields = list(set(VALUE_WRAPPED_RE.findall(content)))
    is_passthrough       = bool(PASSTHROUGH_RE.search(content))
    downstream           = [svc for svc, pat in _DOWNSTREAM_COMPILED.items() if pat.search(content)]

    # ── Downstream extraction (Graph + EXO) ───────────────────────────────
    # Join backtick-continued lines so multi-line PS calls match single-line patterns.
    joined       = _join_continued_lines(content)
    graph_calls  = _extract_graph_calls(joined)
    exo_calls    = _extract_exo_calls(joined)
    body_prop_map = _extract_body_prop_map(content)
    has_transform = bool(RESPONSE_TRANSFORM_RE.search(content))

    dual_mode = _detect_dual_mode(query_params, content, graph_calls, endpoint_name=endpoint)

    computed_fields = _extract_computed_fields(content)

    # ── Helper function traversal ─────────────────────────────────────────
    visited: set = {ps1_path.resolve()}
    helper_names = _extract_helper_function_names(content)
    helper_calls: list[dict] = []
    for hname in helper_names:
        hresult = _scan_helper(hname, API_REPO, visited)
        if hresult:
            helper_calls.append(hresult)

    # Flatten helper discoveries into entrypoint's existing fields
    def _flatten_helper(helper: dict, prefix: str) -> None:
        for gc in helper.get("graph_calls", []):
            gc_tagged = dict(gc)
            gc_tagged["source"] = f"helper:{prefix}"
            graph_calls.append(gc_tagged)
        for cf in helper.get("computed_fields", []):
            cf_tagged = dict(cf)
            cf_tagged["source"] = f"helper:{prefix}:{cf.get('source', 'unknown')}"
            computed_fields.append(cf_tagged)
        for ec in helper.get("exo_calls", []):
            if ec not in exo_calls:
                exo_calls.append(ec)
        for nested in helper.get("nested_helpers", []):
            _flatten_helper(nested, f"{prefix}>{nested['function']}")

    for hc in helper_calls:
        _flatten_helper(hc, hc["function"])

    # ── Build downstream_calls (spec format) ─────────────────────────────
    # Built AFTER helper flattening so helper-discovered graph/exo calls are included.
    select_map: dict[str, str | None] = {}
    for call in graph_calls:
        select_map[call["path"]] = _extract_select_param(joined, call["path"])
    has_passthru_uri = len(_detect_passthru_uri_sources(content)) > 0
    downstream_calls = _build_downstream_calls(
        graph_calls, exo_calls, select_map, has_passthru_uri or is_passthrough
    )

    # ── HTTP method ───────────────────────────────────────────────────────
    http_methods = infer_http_method(content, bool(query_params), bool(body_params))

    # ── Coverage tier ─────────────────────────────────────────────────────
    coverage = determine_coverage(
        has_query=bool(query_params),
        has_body_direct=bool(body_direct),
        blob_aliases=blob_aliases,
        body_params=body_params,
        is_passthrough=is_passthrough,
        is_array_body=is_array_body,
    )

    # ── Required inference ────────────────────────────────────────────────
    # Three signal sources (all annotate params in-place; sidecar overrides in Stage 3):
    #   1. IsNullOrWhiteSpace/IsNullOrEmpty in if-conditions → HIGH signal
    #   2. Graph/EXO downstream schema lookup → MEDIUM signal
    # URI interpolation is implicitly covered: Stage 1 emits high-confidence ast_direct
    # params for $Request.Body.id and $Alias.id; the URI pattern validates those are used.

    validation_required: set[str] = {
        m.lower() for m in REQUIRED_VALIDATION_RE.findall(content)
    }
    downstream_required = _resolve_downstream_required(
        body_params, graph_calls, exo_calls, body_prop_map
    )

    for p in body_params:
        pn = p["name"].lower()
        if pn in validation_required:
            p["required"] = True
            p["required_source"] = "ast_validation"
        elif pn in downstream_required and not p.get("required"):
            p["required"] = True
            p["required_source"] = downstream_required[pn]

    for p in query_params:
        pn = p["name"].lower()
        if pn in validation_required and not p.get("required"):
            p["required"] = True
            p["required_source"] = "ast_validation"

    return {
        "endpoint": endpoint,
        "function": stem,
        "file": str(ps1_path.relative_to(API_REPO)),
        "file_hash": file_hash(ps1_path),
        "tags": folder_to_tag(ps1_path),
        "role": role_m.group(1).strip() if role_m else None,
        "synopsis": syn_m.group(1).strip() if syn_m else None,
        "description": desc_m.group(1).strip() if desc_m else None,
        "http_methods": http_methods,
        "query_params": query_params,
        "body_params": body_params,
        "body_blob_aliases": blob_aliases,
        "has_scheduled_branch": has_scheduled_branch,
        "has_dynamic_options": has_dynamic_options,
        "dynamic_fields": dynamic_fields,
        "value_wrapped_fields": value_wrapped_fields,
        "is_passthrough": is_passthrough,
        "is_array_body": is_array_body,
        "downstream": downstream,
        "graph_calls": graph_calls,
        "downstream_calls": downstream_calls,
        "dual_mode": dual_mode,
        "exo_calls": exo_calls,
        "computed_fields": computed_fields,
        "helper_calls": helper_calls,
        "coverage": coverage,
        "_source": "stage1_api_scanner",
    }


def run(endpoint_filter: str | None = None) -> dict:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    ps1_files = sorted(HTTP_FUNCTIONS_ROOT.rglob("Invoke-*.ps1"))
    if not ps1_files:
        raise RuntimeError(f"No Invoke-*.ps1 files found under {HTTP_FUNCTIONS_ROOT}. Check CIPP_API_REPO.")

    results: dict = {}
    skipped: list = []
    errors:  list = []
    no_functionality_block: list = []  # passed through without .FUNCTIONALITY marker

    for ps1 in ps1_files:
        name = ps1.stem.removeprefix("Invoke-")
        if endpoint_filter and name.lower() != endpoint_filter.lower():
            continue
        try:
            result = scan_endpoint(ps1)
            if result is None:
                skipped.append({"endpoint": name, "reason": "non-entrypoint .FUNCTIONALITY"})
                continue
            # Track files that were allowed through without a .FUNCTIONALITY block
            if not FUNCTIONALITY_RE.search(ps1.read_text(encoding="utf-8", errors="ignore")):
                no_functionality_block.append(name)
            results[name] = result
        except Exception as e:
            errors.append({"endpoint": name, "error": str(e)})

    # Coverage summary
    coverage_counts: dict[str, int] = {}
    for ep in results.values():
        t = ep.get("coverage", "UNKNOWN")
        coverage_counts[t] = coverage_counts.get(t, 0) + 1

    output = {
        "stage": "1_api_scanner",
        "api_repo": str(API_REPO),
        "total_scanned": len(ps1_files),
        "total_endpoints": len(results),
        "coverage_summary": coverage_counts,
        "skipped": skipped,
        "errors": errors,
        "no_functionality_block": no_functionality_block,
        "endpoints": results,
    }

    # Write to endpoint-specific file if filtering, else full corpus
    if endpoint_filter:
        out_path = OUT_DIR / f"endpoint-index-{endpoint_filter}.json"
    else:
        out_path = OUT_DIR / "endpoint-index.json"

    out_path.write_text(json.dumps(output, indent=2))
    print(f"[Stage 1] Scanned {len(results)} endpoints "
          f"(FULL={coverage_counts.get('FULL',0)}, "
          f"PARTIAL={coverage_counts.get('PARTIAL',0)}, "
          f"BLIND={coverage_counts.get('BLIND',0)}) "
          f"-> {out_path}")
    if errors:
        print(f"[Stage 1] ⚠ {len(errors)} errors: {[e['endpoint'] for e in errors]}")
    if skipped:
        print(f"[Stage 1]   {len(skipped)} skipped (non-entrypoints)")
    if no_functionality_block:
        print(f"[Stage 1]   {len(no_functionality_block)} included without .FUNCTIONALITY block "
              f"(older style or changed pattern) — verify these are real entrypoints: "
              f"{no_functionality_block[:5]}{'...' if len(no_functionality_block) > 5 else ''}")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CIPP API Scanner — Stage 1")
    parser.add_argument("--endpoint", help="Scan only this endpoint (e.g. EditUser)")
    args = parser.parse_args()
    run(endpoint_filter=args.endpoint)
