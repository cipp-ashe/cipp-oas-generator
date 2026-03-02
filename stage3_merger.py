"""
Stage 3: Merger + Coverage Classifier
Reconciles Stage 1 (API scan) + Stage 2 (frontend scan) + sidecars (human overrides).

Outputs:
  out/merged-params.json     — authoritative param list per endpoint
  out/mismatch-report.json   — structured drift report per endpoint
  out/coverage-report.json   — coverage tier breakdown with sidecar candidates

Coverage tiers (assigned per endpoint):
  FULL    — both sources present, all params high confidence, no unresolved blob
  PARTIAL — one source only, or mix of confidence levels
  BLIND   — API found but zero params extracted (pure blob passthrough)
  ORPHAN  — frontend calls endpoint but no API implementation found
  STUB    — neither source found (sidecar-only or totally unknown)

Confidence rules:
  HIGH   — param found in BOTH API AST and frontend, or sidecar-asserted high
  MEDIUM — param found in one source only, or blob access
  LOW    — dynamic field, or sidecar-asserted low

Sidecar authority: highest. Sidecar overrides beat both API and frontend.
"""

import json
import argparse
from pathlib import Path
from config import (
    OUT_DIR, SIDECARS_DIR, SHARED_PARAM_REFS, FRONTEND_NOISE,
    COVERAGE_TIERS, KNOWN_URL_ALIASES, KNOWN_NON_PS1_ENDPOINTS,
)

# ── Sidecar schema (validated on load) ───────────────────────────────────────
_SIDECAR_ALLOWED_KEYS = {
    "override_synopsis", "override_description", "override_methods",
    "override_confidence", "override_role", "add_params", "remove_params",
    "add_responses", "notes", "_comment",
    # Raw escape hatches — used when add_params can't express the schema
    "raw_request_body",   # Full OAS 3.1 requestBody object — replaces generated body entirely
    "raw_response_body",  # Dict of {statusCode: full OAS 3.1 response object}
    # Provenance / trust
    "trust_level",        # "reversed" | "tested" | "inferred" (default: "reversed")
    "tested_version",     # CIPP version this sidecar was validated against e.g. "v10.1.2"
    # Warnings for consumers
    "x_cipp_warnings",    # List of strings — forwarded to OAS as x-cipp-warnings extension
    # Deprecation
    "deprecated",         # true/false — marks the endpoint deprecated in OAS
}
_ADD_PARAM_REQUIRED = {"name", "in"}
_ADD_PARAM_ALLOWED = {"name", "in", "required", "type", "description", "confidence"}

# Known OAS 3.1 scalar types for validation
_OAS_SCALAR_TYPES = {"string", "number", "integer", "boolean", "array", "object"}
# Known $ref schema names the generator defines in components/schemas
_KNOWN_SCHEMA_REFS = {
    "LabelValue", "LabelValueNumber", "GroupRef",
    "PostExecution", "ScheduledTask", "StandardResults", "DynamicExtensionFields",
}
# Valid trust levels
_VALID_TRUST_LEVELS = {"reversed", "tested", "verified", "inferred"}
# Trust level semantics:
#   inferred  — auto-generated, not human-reviewed
#   reversed  — human reverse-engineered from source, not live-tested
#   verified  — human spot-checked (code review + logic confirmed), not live-tested
#   tested    — validated against a live tenant / real API call


def validate_sidecar(sidecar: dict, endpoint: str) -> list[str]:
    """
    Validate sidecar structure. Returns list of warning strings.
    Raises ValueError for fatal errors that would produce wrong output.

    Fatal errors: missing required keys, bad 'in' values, raw_request_body not a dict.
    Warnings: unknown keys, unrecognised type names, unrecognised trust_level,
              raw_request_body present alongside add_params (ambiguous authority).
    """
    warnings: list[str] = []
    unknown_keys = set(sidecar) - _SIDECAR_ALLOWED_KEYS
    if unknown_keys:
        warnings.append(f"Unknown sidecar keys (ignored): {sorted(unknown_keys)}")

    # ── add_params validation ─────────────────────────────────────────────
    for i, p in enumerate(sidecar.get("add_params", [])):
        missing = _ADD_PARAM_REQUIRED - set(p)
        if missing:
            raise ValueError(
                f"Sidecar {endpoint}: add_params[{i}] missing required keys: {missing}"
            )
        if p.get("in") not in {"query", "body"}:
            raise ValueError(
                f"Sidecar {endpoint}: add_params[{i}].in must be 'query' or 'body', got: {p.get('in')!r}"
            )
        # Warn on unknown type values — likely a typo (e.g. "Labelvalue" vs "LabelValue")
        t = p.get("type")
        if t and t not in _OAS_SCALAR_TYPES and t not in _KNOWN_SCHEMA_REFS:
            warnings.append(
                f"add_params[{i}] ({p['name']}): unknown type '{t}' — "
                f"expected one of {sorted(_OAS_SCALAR_TYPES)} or "
                f"{sorted(_KNOWN_SCHEMA_REFS)}"
            )

    # ── raw_request_body validation ───────────────────────────────────────
    if "raw_request_body" in sidecar:
        rrb = sidecar["raw_request_body"]
        if not isinstance(rrb, dict):
            raise ValueError(
                f"Sidecar {endpoint}: raw_request_body must be a dict (OAS 3.1 requestBody object), "
                f"got {type(rrb).__name__}"
            )
        if "content" not in rrb:
            warnings.append(
                "raw_request_body is missing 'content' key — "
                "OAS 3.1 requestBody requires content: {'application/json': {schema: ...}}"
            )
        if sidecar.get("add_params"):
            warnings.append(
                "raw_request_body and add_params are both present. "
                "raw_request_body wins and replaces the generated body entirely — "
                "add_params body entries will be ignored for this endpoint."
            )

    # ── raw_response_body validation ──────────────────────────────────────
    if "raw_response_body" in sidecar:
        rrb = sidecar["raw_response_body"]
        if not isinstance(rrb, dict):
            raise ValueError(
                f"Sidecar {endpoint}: raw_response_body must be a dict "
                f"({{statusCode: OAS response object}}), got {type(rrb).__name__}"
            )
        for code, resp in rrb.items():
            if not isinstance(resp, dict):
                warnings.append(
                    f"raw_response_body[{code}]: expected a dict (OAS response object), "
                    f"got {type(resp).__name__} — will be ignored"
                )

    # ── Trust level ───────────────────────────────────────────────────────
    if "trust_level" in sidecar:
        tl = sidecar["trust_level"]
        if tl not in _VALID_TRUST_LEVELS:
            warnings.append(
                f"trust_level '{tl}' unrecognised — expected one of {sorted(_VALID_TRUST_LEVELS)}. "
                "Will be emitted as-is but may not render correctly in tooling."
            )
        if tl == "tested" and not sidecar.get("tested_version"):
            warnings.append(
                "trust_level is 'tested' but tested_version is absent or null. "
                "Set tested_version to the CIPP release this sidecar was validated against "
                "(e.g. \"v10.1.0\") so regressions can be detected on upgrade."
            )

    # ── override_methods ──────────────────────────────────────────────────
    if "override_methods" in sidecar:
        valid_methods = {"GET", "POST", "PATCH", "DELETE", "PUT"}
        for m in sidecar["override_methods"]:
            if m.upper() not in valid_methods:
                warnings.append(f"override_methods: unrecognised HTTP method '{m}'")

    # ── x_cipp_warnings ───────────────────────────────────────────────────
    if "x_cipp_warnings" in sidecar:
        if not isinstance(sidecar["x_cipp_warnings"], list):
            raise ValueError(
                f"Sidecar {endpoint}: x_cipp_warnings must be a list of strings"
            )

    return warnings


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_stage_output(filename: str) -> dict:
    path = OUT_DIR / filename
    if not path.exists():
        raise FileNotFoundError(
            f"Stage output not found: {path}\n"
            f"Run the previous stage first."
        )
    return json.loads(path.read_text())


def load_sidecar(endpoint: str) -> tuple[dict, list[str]]:
    """Load and validate sidecar. Returns (sidecar_dict, warnings)."""
    path = SIDECARS_DIR / f"{endpoint}.json"
    if not path.exists():
        return {}, []
    raw = json.loads(path.read_text())
    warnings = validate_sidecar(raw, endpoint)
    return raw, warnings


def collapse_value_paths(params: list[dict]) -> list[dict]:
    """
    Collapse LabelValue unwrap artifacts.
    If both 'foo' and 'foo.value' appear, keep 'foo' only.
    Pattern: $x.field.value ?? $x.field  →  field is LabelValue, field.value is noise.
    """
    names = {p["name"].lower() for p in params}
    out: list[dict] = []
    for p in params:
        nl = p["name"].lower()
        if nl.endswith(".value"):
            parent = nl[: -len(".value")]
            if parent in names:
                continue  # unwrap artifact — parent already present
        if nl.endswith(".label"):
            parent = nl[: -len(".label")]
            if parent in names:
                continue
        out.append(p)
    return out


def canonical_name(name: str) -> str:
    """Lowercase for comparison — preserves original case in output."""
    return name.lower()


def merge_param_lists(
    api_params: list[dict],
    fe_params:  list[dict],
    remove_set: set[str],
) -> tuple[list[dict], list[dict]]:
    """
    Merge API and frontend param lists. Sidecar additions handled separately.
    Returns (merged_params, mismatch_notes).

    Precedence for conflicts:
      1. If param in both sources → HIGH confidence, API name wins for casing
      2. If API only → param's own confidence, mismatch note
      3. If frontend only → MEDIUM confidence, mismatch note
    Location conflict (api says query, frontend says body) → mismatch note, API wins.
    """
    api_params = collapse_value_paths(api_params)
    fe_params  = collapse_value_paths(fe_params)

    api_idx: dict[str, dict] = {canonical_name(p["name"]): p for p in api_params}
    fe_idx:  dict[str, dict] = {canonical_name(p["name"]): p for p in fe_params}

    all_keys = sorted(set(api_idx) | set(fe_idx))
    merged:    list[dict] = []
    mismatches: list[dict] = []

    for key in all_keys:
        if key in remove_set:
            continue
        if key in FRONTEND_NOISE:
            continue

        api_p = api_idx.get(key)
        fe_p  = fe_idx.get(key)

        if api_p and fe_p:
            confidence  = "high"
            in_location = api_p.get("in", "body")
            # Location conflict
            if api_p.get("in") and fe_p.get("in") and api_p["in"] != fe_p["in"]:
                mismatches.append({
                    "param":  api_p["name"],
                    "issue":  "location_mismatch",
                    "detail": (
                        f"API reads from '{api_p['in']}', "
                        f"frontend sends as '{fe_p['in']}' — API wins"
                    ),
                })
            sources = ["api", "frontend"]
            name = api_p["name"]  # API casing wins

        elif api_p:
            confidence  = api_p.get("confidence", "medium")
            in_location = api_p.get("in", "body")
            sources = ["api"]
            name = api_p["name"]
            # Shared-ref params (tenantFilter, selectedTenants) are a global contract —
            # the frontend doesn't need an explicit per-call mention for them to be valid.
            # Suppress api_only mismatch; they're correct by construction.
            #
            # Dotted-path suppression: if any frontend param is a dotted-path child of
            # this API param name (e.g. API has "ASR", frontend has "ASR.BlockAdobeChild"),
            # the API is reading the parent blob object — the children are the real fields.
            # Emitting api_only for the parent would be misleading noise.
            _is_dotted_parent = any(
                fe_key.startswith(key + ".") for fe_key in fe_idx
            )
            if key not in SHARED_PARAM_REFS and not _is_dotted_parent:
                mismatches.append({
                    "param":  api_p["name"],
                    "issue":  "api_only",
                    "detail": "API reads this param; no matching frontend call site detected",
                })

        else:  # fe_p only
            confidence  = "medium"
            in_location = fe_p.get("in", "body")
            sources = ["frontend"]
            name = fe_p["name"]
            # Dotted-path suppression: if this frontend param is a dotted-path child of
            # an API param name (e.g. frontend sends "ASR.BlockAdobeChild", API reads "ASR"),
            # this is not a mismatch — it's a sub-property of the parent blob the API reads.
            _is_dotted_child = any(
                key.startswith(api_key + ".") for api_key in api_idx
            )
            if not _is_dotted_child:
                mismatches.append({
                    "param":  fe_p["name"],
                    "issue":  "frontend_only",
                    "detail": (
                        "Frontend sends this param; not seen in API AST "
                        "(may be consumed via body blob in downstream function)"
                    ),
                })

        is_shared = key in SHARED_PARAM_REFS

        entry: dict = {
            "name":         name,
            "in":           in_location,
            "confidence":   confidence,
            "is_shared_ref": is_shared,
            "sources":      sources,
        }
        # Carry through schema metadata from whichever source has it.
        # Resolution priority per field:
        #   type     — API wins when both present (API AST is more reliable for wire types);
        #              if API has no type, use frontend's (now carries real inference).
        #   enum     — frontend wins (inferred from radio/select options= props in JSX);
        #              if both have enum, take the longer list (more specific).
        #   format   — either source; API wins if both present.
        #   items    — frontend wins (from array-type selector components).
        #   description — either source; API wins if both present.
        both   = [p for p in (api_p, fe_p) if p is not None]
        first  = both[0]  # primary source (API if present, else frontend)

        # type: prefer API, fall back to frontend
        t_api = api_p.get("type") if api_p else None
        t_fe  = fe_p.get("type")  if fe_p  else None
        resolved_type = t_api or t_fe
        if resolved_type:
            entry["type"] = resolved_type

        # enum: longest list wins
        enums = [p.get("enum") for p in both if p.get("enum")]
        if enums:
            entry["enum"] = max(enums, key=len)

        # format: first source wins (API then frontend)
        fmt = next((p.get("format") for p in both if p.get("format")), None)
        if fmt:
            entry["format"] = fmt

        # items (array element schema): frontend wins
        items_fe = fe_p.get("items") if fe_p else None
        items_api = api_p.get("items") if api_p else None
        items_val = items_fe or items_api
        if items_val:
            entry["items"] = items_val

        # description: first source wins
        desc = next((p.get("description") for p in both if p.get("description")), None)
        if desc:
            entry["description"] = desc

        merged.append(entry)

    return merged, mismatches


def apply_sidecar_additions(
    merged_query: list[dict],
    merged_body:  list[dict],
    sidecar:      dict,
) -> tuple[list[dict], list[dict]]:
    """
    Apply sidecar add_params to the correct list based on their 'in' field.
    Sidecar wins: if a sidecar param matches an existing name, the existing
    entry is REPLACED (not just supplemented) — sidecar is authoritative.
    """
    all_names = {canonical_name(p["name"]) for p in merged_query + merged_body}

    for p in sidecar.get("add_params", []):
        cn = canonical_name(p["name"])
        entry = {**p, "sources": ["sidecar"]}
        entry.setdefault("confidence", "high")

        if cn in all_names:
            # Replace existing entry — sidecar override
            if p.get("in", "body") == "query":
                merged_query = [e for e in merged_query if canonical_name(e["name"]) != cn]
                merged_query.append(entry)
            else:
                merged_body = [e for e in merged_body if canonical_name(e["name"]) != cn]
                merged_body.append(entry)
        else:
            if p.get("in", "body") == "query":
                merged_query.append(entry)
            else:
                merged_body.append(entry)

    return merged_query, merged_body


def assign_coverage_tier(
    api_data:      dict,
    fe_data:       dict,
    merged_query:  list[dict],
    merged_body:   list[dict],
    sidecar_applied: bool,
) -> str:
    """
    Assign a coverage tier based on source availability and param confidence.

    FULL    — both sources, all params high confidence, no blob-accessed params
    PARTIAL — one source, mixed confidence, or blob-access params present
    BLIND   — API found, zero params extracted before sidecar
    ORPHAN  — frontend found, no API implementation
    STUB    — neither source, sidecar-only
    """
    has_api      = bool(api_data)
    has_frontend = bool(fe_data)

    if not has_api and not has_frontend:
        return "STUB"
    if has_frontend and not has_api:
        return "ORPHAN"

    # API was found — check Stage 1 coverage tag first
    if api_data.get("coverage") == "ARRAY_BODY":
        return "ARRAY_BODY"  # body is an array; schema is per-element, not per-field at root

    # API was found — check what it yielded before sidecar
    api_query = api_data.get("query_params", [])
    api_body  = api_data.get("body_params", [])
    if not api_query and not api_body:
        return "BLIND"  # pure passthrough or empty function

    all_params = merged_query + merged_body
    # Filter sidecar-only params for tier assessment (they're human-verified anyway)
    scan_params = [p for p in all_params if "sidecar" not in p.get("sources", [])]

    if not scan_params:
        # All params are sidecar-only — effectively BLIND but sidecar-covered
        return "PARTIAL"

    if not has_frontend:
        return "PARTIAL"  # API found, no frontend call site

    # FULL requires all scan params to be high confidence AND none sourced only
    # from blob access. Check the source tag directly from Stage 1 data — using
    # body_blob_aliases presence was too coarse (it downgraded all api-only params
    # even when some were ast_direct high confidence).
    api_blob_names: set[str] = {
        p["name"].lower() for p in (api_data.get("body_params") or [])
        if p.get("source") == "ast_blob"
    }
    all_high = all(p.get("confidence") == "high" for p in scan_params)
    any_blob = any(p["name"].lower() in api_blob_names for p in scan_params)

    if all_high and not any_blob and has_frontend:
        return "FULL"
    return "PARTIAL"


def run(endpoint_filter: str | None = None) -> dict:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    SIDECARS_DIR.mkdir(parents=True, exist_ok=True)

    # Load stage inputs — use filtered files if available, else full corpus
    def _load(base: str, ep: str | None) -> dict:
        if ep:
            filtered = OUT_DIR / f"{base}-{ep}.json"
            if filtered.exists():
                return json.loads(filtered.read_text())
        return load_stage_output(f"{base}.json")

    stage1 = _load("endpoint-index", endpoint_filter)
    stage2 = _load("frontend-calls", endpoint_filter)

    api_endpoints = stage1["endpoints"]
    fe_endpoints  = stage2["endpoints"]

    # Normalise Stage 2 endpoint names to match Stage 1 casing.
    # Stage 2 captures endpoint names verbatim from JSX URL strings (e.g. "/api/listUsers")
    # while Stage 1 derives names from PS1 filenames (e.g. "Invoke-ListUsers" → "ListUsers").
    # A case-insensitive join maps both to the same canonical key so the merger can reconcile
    # them — without this, mismatched casing produces false ORPHAN entries.
    _api_name_lower: dict[str, str] = {n.lower(): n for n in api_endpoints}
    _fe_normalised: dict[str, dict] = {}
    for fe_name, fe_data in fe_endpoints.items():
        canonical = _api_name_lower.get(fe_name.lower(), fe_name)
        if canonical != fe_name and canonical not in _fe_normalised:
            # Remap to API casing; merge call_sites if both somehow existed
            _fe_normalised[canonical] = fe_data
        elif canonical not in _fe_normalised:
            _fe_normalised[canonical] = fe_data
        # else: canonical already present (duplicate after normalisation) — keep first
    fe_endpoints = _fe_normalised

    # Apply known URL aliases: remap frontend endpoint names that differ from the
    # canonical PS1-derived name. E.g. frontend calls /api/ListIPWhitelist but the
    # PS1 is Invoke-ExecAddTrustedIP → remap to "ExecAddTrustedIP".
    _fe_aliased: dict[str, dict] = {}
    for fe_name, fe_data in fe_endpoints.items():
        canonical = KNOWN_URL_ALIASES.get(fe_name, fe_name)
        if canonical not in _fe_aliased:
            _fe_aliased[canonical] = fe_data
        # else: canonical already present — keep first (shouldn't happen with clean aliases)
    fe_endpoints = _fe_aliased

    # Remove platform-level endpoints that are never backed by a PS1.
    # These would otherwise show as ORPHAN, which is misleading.
    for platform_ep in KNOWN_NON_PS1_ENDPOINTS:
        fe_endpoints.pop(platform_ep, None)

    all_names = sorted(set(api_endpoints) | set(fe_endpoints))

    merged_output:   dict = {}
    all_mismatches:  dict = {}
    sidecar_warnings: dict = {}
    coverage_counts: dict = {}
    blind_endpoints: list = []
    orphan_endpoints: list = []
    sidecar_candidates: list = []

    for endpoint in all_names:
        if endpoint_filter and endpoint.lower() != endpoint_filter.lower():
            continue

        api_data = api_endpoints.get(endpoint, {})
        fe_data  = fe_endpoints.get(endpoint, {})

        try:
            sidecar, warnings = load_sidecar(endpoint)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"[Stage 3] ⚠ Sidecar error for {endpoint}: {e}")
            sidecar, warnings = {}, [str(e)]

        if warnings:
            sidecar_warnings[endpoint] = warnings

        remove_set = {canonical_name(r) for r in sidecar.get("remove_params", [])}

        # Merge query and body separately
        api_query = api_data.get("query_params", [])
        api_body  = api_data.get("body_params",  [])
        fe_query  = fe_data.get("query_params",  [])
        fe_body   = fe_data.get("body_params",   [])

        merged_query, q_mismatches = merge_param_lists(api_query, fe_query, remove_set)
        merged_body,  b_mismatches = merge_param_lists(api_body,  fe_body,  remove_set)

        # Apply sidecar additions (sidecar authority: highest)
        merged_query, merged_body = apply_sidecar_additions(merged_query, merged_body, sidecar)

        # Build mismatch list
        mismatches: list[dict] = q_mismatches + b_mismatches

        # Endpoint-level mismatch notes
        if api_data and not fe_data:
            mismatches.insert(0, {
                "param":  "_endpoint",
                "issue":  "no_frontend_call",
                "detail": "Endpoint exists in API but no frontend call site found",
            })
        if fe_data and not api_data:
            mismatches.insert(0, {
                "param":  "_endpoint",
                "issue":  "no_api_implementation",
                "detail": "Frontend calls this endpoint but no Invoke-*.ps1 found",
            })

        # Stage 2 flag-based mismatch notes — surface what the scanner detected
        # but could not resolve, so sidecar authors have explicit direction.
        if fe_data.get("has_external_form_component"):
            components = fe_data.get("external_form_components", [])
            mismatches.append({
                "param":  "_endpoint",
                "issue":  "external_form_component",
                "detail": (
                    f"Form fields live in external component(s): {components}. "
                    f"Import is not followed — add a sidecar to document the body contract."
                ),
            })
        if fe_data.get("has_data_transform"):
            mismatches.append({
                "param":  "_endpoint",
                "issue":  "data_transform",
                "detail": (
                    "customDataformatter detected on CippFormPage — field names or types "
                    "sent to the API may differ from form field names. "
                    "Add a sidecar to document the actual POST schema."
                ),
            })
        if fe_data.get("has_constructed_body"):
            mismatches.append({
                "param":  "_endpoint",
                "issue":  "constructed_body",
                "detail": (
                    "Submit handler builds its own data object rather than submitting "
                    "form values directly. Form field names may not match API param names. "
                    "Add a sidecar to document the actual POST schema."
                ),
            })

        if sidecar.get("notes"):
            mismatches.append({
                "param":  "_sidecar",
                "issue":  "sidecar_note",
                "detail": sidecar["notes"],
            })

        # Coverage tier
        tier = assign_coverage_tier(api_data, fe_data, merged_query, merged_body, bool(sidecar))
        coverage_counts[tier] = coverage_counts.get(tier, 0) + 1

        if tier == "BLIND" and not sidecar:
            blind_endpoints.append(endpoint)
            sidecar_candidates.append(endpoint)
        if tier == "ORPHAN":
            orphan_endpoints.append(endpoint)
        # Flag-based sidecar candidates — external component / transform / constructed body
        # that don't yet have a sidecar. These need human authoring to complete the contract.
        if not sidecar and (
            fe_data.get("has_external_form_component")
            or fe_data.get("has_data_transform")
            or fe_data.get("has_constructed_body")
        ):
            if endpoint not in sidecar_candidates:
                sidecar_candidates.append(endpoint)

        # Overall param confidence (post-sidecar)
        all_params = merged_query + merged_body
        if not all_params:
            param_confidence = "low"
        elif all(p["confidence"] == "high" for p in all_params):
            param_confidence = "high"
        elif any(p["confidence"] == "low" for p in all_params):
            param_confidence = "low"
        else:
            param_confidence = "medium"

        # Sidecar can assert a different overall confidence
        final_confidence = sidecar.get("override_confidence", param_confidence)
        if sidecar.get("override_confidence") and sidecar["override_confidence"] != param_confidence:
            mismatches.append({
                "param":  "_sidecar",
                "issue":  "confidence_override",
                "detail": (
                    f"Sidecar asserts confidence={sidecar['override_confidence']}; "
                    f"scanner computed {param_confidence}"
                ),
            })

        merged_output[endpoint] = {
            "endpoint":            endpoint,
            "confidence":          final_confidence,
            "coverage_tier":       tier,
            "role":                api_data.get("role") or sidecar.get("override_role"),
            "synopsis":            sidecar.get("override_synopsis") or api_data.get("synopsis"),
            "description":         sidecar.get("override_description") or api_data.get("description"),
            "tags":                api_data.get("tags", []),
            "http_methods": (
                sidecar.get("override_methods")
                or api_data.get("http_methods")
                or fe_data.get("methods")
                or ["GET"]
            ),
            "query_params":        merged_query,
            "body_params":         merged_body,
            "downstream":          api_data.get("downstream", []),
            "has_scheduled_branch": api_data.get("has_scheduled_branch", False),
            "has_dynamic_options": api_data.get("has_dynamic_options", False),
            "dynamic_fields":      api_data.get("dynamic_fields", []),
            "value_wrapped_fields": api_data.get("value_wrapped_fields", []),
            "extra_responses":     sidecar.get("add_responses", {}),
            "call_sites":          fe_data.get("call_sites", []),
            "url_patterns":        fe_data.get("url_patterns", []),
            "sidecar_applied":     bool(sidecar),
            # Frontend scan flags — passed through for Stage 4 OAS extensions
            "has_data_transform":          fe_data.get("has_data_transform", False),
            "has_constructed_body":        fe_data.get("has_constructed_body", False),
            "has_external_form_component": fe_data.get("has_external_form_component", False),
            "external_form_components":    fe_data.get("external_form_components", []),
            # Raw escape hatches — present only when sidecar provides them
            "raw_request_body":    sidecar.get("raw_request_body"),
            "raw_response_body":   sidecar.get("raw_response_body"),
            # Provenance / trust
            "trust_level":         sidecar.get("trust_level", "reversed" if sidecar else None),
            "tested_version":      sidecar.get("tested_version"),
            "deprecated":          sidecar.get("deprecated", False),
            "x_cipp_warnings":     sidecar.get("x_cipp_warnings", []),
            "_source":             "stage3_merger",
        }

        if mismatches:
            all_mismatches[endpoint] = mismatches

    # ── Summary stats ─────────────────────────────────────────────────────
    total = len(merged_output)
    high  = sum(1 for v in merged_output.values() if v["confidence"] == "high")
    low   = sum(1 for v in merged_output.values() if v["confidence"] == "low")
    with_sidecar = sum(1 for v in merged_output.values() if v["sidecar_applied"])

    merged_file_output = {
        "stage":                "3_merger",
        "total_endpoints":      total,
        "confidence_breakdown": {
            "high":   high,
            "medium": total - high - low,
            "low":    low,
        },
        "coverage_breakdown":   coverage_counts,
        "with_sidecar_overrides": with_sidecar,
        "sidecar_warnings":     sidecar_warnings,
        "endpoints":            merged_output,
    }

    mismatch_file_output = {
        "total_endpoints_with_mismatches": len(all_mismatches),
        "mismatches": all_mismatches,
    }

    # Categorise sidecar candidates by reason for traceability
    _external_candidates = sorted(
        ep for ep in sidecar_candidates
        if merged_output.get(ep, {}).get("has_external_form_component")
    )
    _transform_candidates = sorted(
        ep for ep in sidecar_candidates
        if merged_output.get(ep, {}).get("has_data_transform")
    )
    _constructed_candidates = sorted(
        ep for ep in sidecar_candidates
        if merged_output.get(ep, {}).get("has_constructed_body")
    )
    _blind_candidates = sorted(
        ep for ep in sidecar_candidates
        if merged_output.get(ep, {}).get("coverage_tier") == "BLIND"
    )

    # ── Probable 404s ─────────────────────────────────────────────────────
    # CIPP routes all API calls via New-CippCoreRequest which constructs:
    #   $FunctionName = 'Invoke-{0}' -f $Request.Params.CIPPEndpoint
    # This means the URL path IS the PS1 suffix, verbatim and case-sensitive.
    # An ORPHAN endpoint with active frontend call sites is a genuine production 404.
    # Flag these explicitly so they surface as bugs, not scanner noise.
    _probable_404s = []
    for ep in sorted(orphan_endpoints):
        ep_data = merged_output.get(ep, {})
        call_sites = ep_data.get("call_sites", [])
        if call_sites:
            _probable_404s.append({
                "endpoint":   ep,
                "call_sites": call_sites,
                "call_count": len(call_sites),
                "deprecated": ep_data.get("deprecated", False),
                "note": (
                    "Frontend calls this URL but no Invoke-{endpoint}.ps1 exists. "
                    "In CIPP's single-entrypoint router, this returns 404 at runtime. "
                    "Either the PS1 is missing, or the frontend URL is wrong."
                ).format(endpoint=ep),
            })

    # ── ORPHAN delta (new vs previous run) ────────────────────────────────
    # Compare current orphans against the previous coverage-report.json.
    # New ORPHANs surface immediately rather than accumulating silently.
    _prev_orphans: set[str] = set()
    _prev_coverage = OUT_DIR / "coverage-report.json"
    if _prev_coverage.exists() and not endpoint_filter:
        try:
            _prev = json.loads(_prev_coverage.read_text())
            _prev_orphans = set(_prev.get("orphan_endpoints", []))
        except (json.JSONDecodeError, KeyError):
            pass
    _current_orphans = set(orphan_endpoints)
    _new_orphans     = sorted(_current_orphans - _prev_orphans)
    _resolved_orphans = sorted(_prev_orphans - _current_orphans)

    # ── Type inference coverage metric ───────────────────────────────────
    # Counts all params across all merged endpoints and measures how many
    # have a richer type than the default "string". Tracks improvement over
    # time as sidecars are added and TYPE_HINTS expanded.
    _all_typed_params = []
    for ep_data in merged_output.values():
        for param in ep_data.get("query_params", []) + ep_data.get("body_params", []):
            t = param.get("type") or "string"
            _all_typed_params.append(t)
    _total_params = len(_all_typed_params)
    _non_string_params = sum(1 for t in _all_typed_params if t != "string")
    _type_coverage_pct = (
        round(_non_string_params / _total_params * 100, 1) if _total_params else 0.0
    )

    coverage_file_output = {
        "summary": coverage_counts,
        "tier_definitions": COVERAGE_TIERS,
        "blind_endpoints": sorted(blind_endpoints),
        "orphan_endpoints": sorted(orphan_endpoints),
        # Genuine production 404s: ORPHAN endpoints with active frontend call sites.
        # In CIPP's router, these will return 404 at runtime. Treat as bugs.
        "probable_404s": _probable_404s,
        # Delta vs previous run — new ORPHANs need immediate investigation.
        "orphan_delta": {
            "new":      _new_orphans,
            "resolved": _resolved_orphans,
        },
        "sidecar_candidates": sorted(sidecar_candidates),
        "sidecar_candidates_by_reason": {
            "blind":                    _blind_candidates,
            "external_form_component":  _external_candidates,
            "data_transform":           _transform_candidates,
            "constructed_body":         _constructed_candidates,
        },
        # Percentage of params with a non-default type (anything other than "string").
        # Increases as sidecars and TYPE_HINTS improve type fidelity. Track over time.
        "type_inference_coverage": {
            "total_params":      _total_params,
            "non_string_params": _non_string_params,
            "pct_typed":         _type_coverage_pct,
        },
        "note": (
            "blind_endpoints: API function found but zero params extracted. "
            "probable_404s: ORPHAN endpoints with active call sites — genuine runtime 404s in CIPP. "
            "orphan_delta.new: endpoints that became ORPHAN since last run — investigate immediately. "
            "sidecar_candidates = all endpoints without a sidecar that need one. "
            "type_inference_coverage.pct_typed: % params with a richer type than default 'string' — "
            "increases as sidecars and TYPE_HINTS improve."
        ),
    }

    # Write — single-endpoint runs go to separate files to preserve corpus output
    suffix = f"-{endpoint_filter}" if endpoint_filter else ""
    (OUT_DIR / f"merged-params{suffix}.json").write_text(json.dumps(merged_file_output, indent=2))
    (OUT_DIR / f"mismatch-report{suffix}.json").write_text(json.dumps(mismatch_file_output, indent=2))
    if not endpoint_filter:
        (OUT_DIR / "coverage-report.json").write_text(json.dumps(coverage_file_output, indent=2))

    print(
        f"[Stage 3] Merged {total} endpoints "
        f"(high={high}, low={low}) "
        f"Coverage: FULL={coverage_counts.get('FULL',0)} "
        f"PARTIAL={coverage_counts.get('PARTIAL',0)} "
        f"BLIND={coverage_counts.get('BLIND',0)} "
        f"ORPHAN={coverage_counts.get('ORPHAN',0)}"
    )
    if blind_endpoints:
        print(f"[Stage 3] ⚠ {len(blind_endpoints)} BLIND endpoints need sidecars: "
              f"{blind_endpoints[:5]}{'...' if len(blind_endpoints) > 5 else ''}")
    if _probable_404s:
        p404_names = [p["endpoint"] for p in _probable_404s if not p.get("deprecated")]
        if p404_names:
            print(f"[Stage 3] 🔴 {len(p404_names)} probable production 404s (active call sites, no PS1): "
                  f"{p404_names[:5]}{'...' if len(p404_names) > 5 else ''}")
    if _new_orphans:
        print(f"[Stage 3] 🆕 {len(_new_orphans)} NEW orphans since last run: {_new_orphans}")
    if sidecar_warnings:
        print(f"[Stage 3] ⚠ Sidecar warnings for: {sorted(sidecar_warnings)}")

    if endpoint_filter and endpoint_filter in all_mismatches:
        print(f"\n── Mismatch report for {endpoint_filter} ──")
        for m in all_mismatches[endpoint_filter]:
            print(f"  [{m['issue']}] {m['param']}: {m['detail']}")

    return merged_file_output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CIPP Param Merger — Stage 3")
    parser.add_argument("--endpoint", help="Merge only this endpoint (e.g. EditUser)")
    args = parser.parse_args()
    run(endpoint_filter=args.endpoint)
