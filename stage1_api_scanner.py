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
    DOWNSTREAM_PATTERNS, PARAM_NOISE, PS_NOISE
)

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

# ── Helpers ───────────────────────────────────────────────────────────────────

def file_hash(path: Path) -> str:
    """SHA256 of file contents — used to detect staleness between runs."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def folder_to_tag(path: Path) -> list[str]:
    """
    HTTP Functions/Identity/Administration/Users/Invoke-X.ps1 → ['Identity','Administration','Users']
    Identity/Administration/Users/Invoke-X.ps1 → ['Identity','Administration','Users']
    Invoke-X.ps1 (root level) → []
    
    Strips "HTTP Functions" from the path since some endpoints live in Entrypoints
    root and others in Entrypoints/HTTP Functions subdirectory.
    """
    parts = path.relative_to(HTTP_FUNCTIONS_ROOT).parts
    # Strip "HTTP Functions" if present (for subdirectory files)
    if parts and parts[0] == "HTTP Functions":
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
    if fn_m:
        if "entrypoint" not in fn_m.group(1).lower():
            return None  # Activity trigger, helper, etc. — not an HTTP endpoint
    # If no .FUNCTIONALITY block at all, allow through (older style)

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
          f"→ {out_path}")
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
