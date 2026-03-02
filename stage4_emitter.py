"""
Stage 4: OAS 3.1 Emitter
Reads merged-params.json → emits valid OAS 3.1 JSON.

Outputs:
  out/openapi.json           — unified spec (all endpoints)
  out/domain/<name>.json     — per-domain spec
  (single-endpoint mode: prints snippet only, does not write files)

OAS 3.1 correctness rules enforced here:
  - $ref must be the sole key in its containing object (siblings are illegal)
  - x-cipp-* extensions are valid in OAS 3.1 (extension keys are always allowed)
  - additionalProperties: true is legal for open schemas
  - required[] contains only the names of properties defined in properties{}

Verbosity modes:
  Default (clean):  Only consumer-useful extensions are emitted:
                    x-cipp-role, x-cipp-dynamic, x-cipp-scheduled-branch,
                    x-cipp-dynamic-options, x-cipp-warnings.
                    Pipeline-introspection metadata (confidence, coverage tier,
                    sources, data-transform flags, etc.) is stripped entirely.
  --verbose:        All x-cipp-* extensions are emitted, including pipeline
                    metadata. Use for internal auditing and debugging only.
"""

import json
import argparse
from pathlib import Path
from collections import defaultdict
from config import (
    OUT_DIR, DOMAIN_DIR, SHARED_PARAM_REFS,
    TYPE_HINTS, ALWAYS_REQUIRED_BODY, ALWAYS_REQUIRED_QUERY,
    DYNAMIC_EXTENSION_PARENTS,
)

_SCRIPT_DIR = Path(__file__).parent
# DOMAIN_DIR imported from config

# ── Shared OAS components ─────────────────────────────────────────────────────

SHARED_COMPONENTS: dict = {
    "schemas": {
        "LabelValue": {
            "type": "object",
            "description": (
                "Autocomplete/select field. Most dropdowns in CIPP use this shape. "
                "Backend typically unwraps via: $x.value ?? $x"
            ),
            "properties": {
                "label": {"type": "string"},
                "value": {"type": "string"},
            },
        },
        "LabelValueNumber": {
            "type": "object",
            "properties": {
                "label": {"type": "string"},
                "value": {"type": "number"},
            },
        },
        "GroupRef": {
            "type": "object",
            "description": "Reference to a group, including type metadata for routing (Graph vs EXO).",
            "properties": {
                "label": {"type": "string"},
                "value": {"type": "string", "description": "Group object ID"},
                "addedFields": {
                    "type": "object",
                    "properties": {
                        "groupType": {
                            "type": "string",
                            "enum": [
                                "Security",
                                "Distribution list",
                                "Mail-Enabled Security",
                                "Microsoft 365",
                            ],
                        }
                    },
                },
            },
        },
        "PostExecution": {
            "type": "object",
            "description": "Notification channels triggered after task completion.",
            "properties": {
                "webhook": {"type": "boolean"},
                "email":   {"type": "boolean"},
                "psa":     {"type": "boolean"},
            },
        },
        "ScheduledTask": {
            "type": "object",
            "description": "Controls deferred execution. If Enabled is true, task is queued rather than run immediately.",
            "properties": {
                "Enabled": {"type": "boolean"},
                "date":    {"type": "string", "format": "date-time"},
            },
        },
        "StandardResults": {
            "type": "object",
            "description": "Standard CIPP API response envelope.",
            "properties": {
                "Results": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Result messages, one per operation. Mix of success and error strings.",
                }
            },
        },
        "DynamicExtensionFields": {
            "type": "object",
            "description": (
                "Per-tenant custom directory extension attributes. "
                "Schema varies by tenant configuration and cannot be statically defined."
            ),
            "additionalProperties": True,
            "x-cipp-dynamic": True,
        },
    },
    "parameters": {
        "tenantFilter": {
            "name": "tenantFilter",
            "in": "query",
            "description": "Target tenant domain or 'AllTenants' for multi-tenant operations.",
            "required": True,
            "schema": {"type": "string"},
        },
        "selectedTenants": {
            "name": "selectedTenants",
            "in": "query",
            "description": "Comma-separated list of tenant domains for bulk operations.",
            "required": False,
            "schema": {"type": "string"},
        },
    },
    "securitySchemes": {
        "bearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": (
                "Azure AD bearer token. "
                "Obtained via MSAL against the CIPP API app registration."
            ),
        }
    },
}

_STANDARD_RESPONSES: dict = {
    "200": {
        "description": "Success",
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/StandardResults"}
            }
        },
    },
    "400": {"description": "Bad request — missing required field or invalid input"},
    "401": {"description": "Unauthorized — invalid or missing bearer token"},
    "403": {"description": "Forbidden — caller lacks the required RBAC role"},
    "500": {"description": "Internal server error"},
}


# ── Schema building ───────────────────────────────────────────────────────────

def make_schema(param: dict) -> dict:
    """
    Build OAS schema for a single param.

    Priority order (highest to lowest):
      1. param's own 'type' field — set by sidecars OR by Stage 2 type inference.
         Stage 2 now infers types from CippFormComponent type= props, selector
         components, and multiple= flags, so many frontend-sourced params now
         carry real type information rather than defaulting to string.
      2. TYPE_HINTS config lookup (name-based global overrides)
      3. Inline metadata from the param itself: enum, format, items
      4. String default

    OAS 3.1 compliance: $ref must be the sole key in its containing object.
    Never puts a $ref alongside type, description, or any other key.

    Handles the following param-level fields:
      type    — string, boolean, number, integer, array, object, or a schema $ref name
      enum    — list of string values (for string type)
      format  — OAS format string (e.g. "date-time", "password")
      items   — OAS items schema for array types (e.g. {"$ref": "..."} or {"type": "string"})
    """
    name_lower = param["name"].split(".")[0].lower()  # top-level name only

    # Known schema $ref names — return a clean $ref with no siblings
    _REF_TYPES = {
        "LabelValue", "LabelValueNumber", "GroupRef",
        "PostExecution", "ScheduledTask", "DynamicExtensionFields",
    }

    # Param-level type (from sidecar or Stage 2 type inference)
    param_type = param.get("type")
    if param_type:
        # Schema $ref names
        if param_type in _REF_TYPES:
            return {"$ref": f"#/components/schemas/{param_type}"}

        # Array — preserve items schema if provided
        if param_type == "array":
            items_schema = param.get("items")
            if not items_schema:
                items_schema = {"type": "string"}
            # If items is itself a $ref shape, keep it clean
            return {"type": "array", "items": items_schema}

        # Scalar types
        schema: dict = {"type": param_type}
        if param.get("enum"):
            schema["enum"] = param["enum"]
        if param.get("format"):
            schema["format"] = param["format"]
        return schema

    # TYPE_HINTS lookup (name-based global overrides in config)
    hint = TYPE_HINTS.get(name_lower)
    if hint:
        return dict(hint)  # copy to avoid mutating the config

    # Param carries inline enum/format without an explicit type — apply to string default
    if param.get("enum") or param.get("format"):
        schema = {"type": "string"}
        if param.get("enum"):
            schema["enum"] = param["enum"]
        if param.get("format"):
            schema["format"] = param["format"]
        return schema

    # Default
    desc = param.get("description")
    schema = {"type": "string"}
    if desc:
        schema["description"] = desc
    return schema


def make_param_object(param: dict, required_set: set[str], verbose: bool = False) -> dict:
    """
    Build an OAS parameter object (for query params in the parameters[] list).
    $ref params are returned as {"$ref": "..."} directly.
    Pipeline-introspection extensions (confidence, sources) are only emitted in verbose mode.
    """
    name_lower = param["name"].lower()

    if name_lower in SHARED_PARAM_REFS:
        return {"$ref": SHARED_PARAM_REFS[name_lower]}

    schema = make_schema(param)
    obj: dict = {
        "name":     param["name"],
        "in":       "query",
        "required": name_lower in required_set,
        "schema":   schema,
    }
    if verbose:
        if param.get("confidence") and param["confidence"] != "high":
            obj["x-cipp-confidence"] = param["confidence"]
        if param.get("sources"):
            obj["x-cipp-sources"] = param["sources"]
    return obj


def build_request_body(body_params: list[dict], verbose: bool = False) -> dict | None:
    """
    Build OAS requestBody from merged body params.
    Handles nested params (foo.bar → nested object).
    OAS 3.1 compliance: $ref as sole key in schema.
    """
    if not body_params:
        return None

    properties: dict  = {}
    required:   list  = []

    for param in body_params:
        name       = param["name"]
        name_lower = name.lower()
        schema     = make_schema(param)

        if name_lower in ALWAYS_REQUIRED_BODY:
            required.append(name)

        if "." in name:
            # Nested path: e.g. "Scheduled.Enabled" → Scheduled.properties.Enabled
            # Also handles deep paths: "defaultAttributes.SomeLabel.Value"
            parts  = name.split(".", 1)
            parent = parts[0]
            child  = parts[1]
            # Dynamic extension field parents collapse to $ref — don't build nested properties
            if parent.lower() in DYNAMIC_EXTENSION_PARENTS:
                if parent not in properties:
                    properties[parent] = {"$ref": "#/components/schemas/DynamicExtensionFields"}
            else:
                if parent not in properties:
                    properties[parent] = {"type": "object", "properties": {}}
                elif "properties" not in properties[parent]:
                    # Parent was added as a flat param first (e.g. "ASR" blob access from API).
                    # Now a dotted child is arriving — promote the parent to an object.
                    properties[parent]["type"] = "object"
                    properties[parent]["properties"] = {}
                properties[parent]["properties"][child] = schema
        else:
            # Annotate schema with cipp extensions (only if schema is not a bare $ref)
            if "$ref" in schema and len(schema) == 1:
                if verbose:
                    # Wrap in allOf to allow extensions alongside $ref (OAS 3.1 compliant)
                    annotated: dict = {"allOf": [schema]}
                    if param.get("confidence") and param["confidence"] != "high":
                        annotated["x-cipp-confidence"] = param["confidence"]
                    if param.get("sources"):
                        annotated["x-cipp-sources"] = param["sources"]
                    properties[name] = annotated
                else:
                    properties[name] = schema
            else:
                if verbose:
                    if param.get("confidence") and param["confidence"] != "high":
                        schema["x-cipp-confidence"] = param["confidence"]
                    if param.get("sources"):
                        schema["x-cipp-sources"] = param["sources"]
                properties[name] = schema

    # Ensure required only lists names that are in properties
    required = [r for r in required if r in properties]

    body_schema: dict = {"type": "object", "properties": properties}
    if required:
        body_schema["required"] = required

    return {
        "required": True,
        "content":  {
            "application/json": {"schema": body_schema}
        },
    }


def build_path_entry(ep: dict, verbose: bool = False) -> dict:
    """Build OAS paths entry (method operations) for one endpoint."""
    endpoint    = ep["endpoint"]
    methods     = ep.get("http_methods", ["GET"])
    tags        = ep.get("tags", [])
    role        = ep.get("role") or ""
    synopsis    = ep.get("synopsis") or endpoint
    description = ep.get("description") or ""
    confidence  = ep.get("confidence", "medium")
    coverage    = ep.get("coverage_tier", "PARTIAL")
    downstream  = ep.get("downstream", [])
    has_scheduled      = ep.get("has_scheduled_branch", False)
    has_dynamic        = ep.get("has_dynamic_options", False)
    has_data_transform = ep.get("has_data_transform", False)
    has_constructed    = ep.get("has_constructed_body", False)
    has_external_form  = ep.get("has_external_form_component", False)
    external_components = ep.get("external_form_components", [])
    extra_responses = ep.get("extra_responses", {})
    raw_request_body  = ep.get("raw_request_body")   # sidecar escape hatch
    raw_response_body = ep.get("raw_response_body")  # sidecar escape hatch
    trust_level    = ep.get("trust_level")
    tested_version = ep.get("tested_version")
    deprecated     = ep.get("deprecated", False)
    warnings       = ep.get("x_cipp_warnings", [])

    query_params = ep.get("query_params", [])
    body_params  = ep.get("body_params",  [])

    # ── Parameters (query) ────────────────────────────────────────────────
    parameters: list = []
    seen_refs: set = set()
    for qp in query_params:
        pobj = make_param_object(qp, ALWAYS_REQUIRED_QUERY, verbose=verbose)
        # Deduplicate $ref parameters
        if "$ref" in pobj:
            if pobj["$ref"] in seen_refs:
                continue
            seen_refs.add(pobj["$ref"])
        parameters.append(pobj)

    # ── Request body ──────────────────────────────────────────────────────
    # raw_request_body from sidecar takes priority — it's a hand-written OAS
    # requestBody object and replaces generated output entirely.
    if raw_request_body:
        request_body = raw_request_body
    elif coverage == "ARRAY_BODY":
        # Body is an array of objects. Required element fields (id, tenantFilter, etc.)
        # are emitted as properties with required:true. Everything else is open schema.
        item_properties: dict = {}
        item_required: list = []
        for param in body_params:
            item_properties[param["name"]] = make_schema(param)
            if param.get("name", "").lower() in ALWAYS_REQUIRED_BODY:
                item_required.append(param["name"])
        item_schema: dict = {"type": "object", "additionalProperties": True}
        if item_properties:
            item_schema["properties"] = item_properties
        if item_required:
            item_schema["required"] = item_required
        request_body = {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "oneOf": [
                            {"type": "array", "items": item_schema},
                            item_schema,
                        ],
                        "description": (
                            "Accepts a single object or an array of objects. "
                            "Each object must include the required fields."
                        ),
                    }
                }
            },
        }
    else:
        request_body = build_request_body(body_params, verbose=verbose)

    # ── Responses ─────────────────────────────────────────────────────────
    # raw_response_body from sidecar replaces standard responses entirely when present.
    if raw_response_body:
        responses = {}
        for code, resp_obj in raw_response_body.items():
            if isinstance(resp_obj, dict):
                responses[str(code)] = resp_obj
            else:
                # Fallback: treat as description string (graceful degradation)
                responses[str(code)] = {"description": str(resp_obj)}
        # Always keep 401/403 — auth failures apply to everything
        if "401" not in responses:
            responses["401"] = _STANDARD_RESPONSES["401"]
        if "403" not in responses:
            responses["403"] = _STANDARD_RESPONSES["403"]
    else:
        responses = dict(_STANDARD_RESPONSES)
        for code, desc in extra_responses.items():
            if isinstance(desc, dict):
                responses[str(code)] = desc
            else:
                responses[str(code)] = {"description": desc}

    # ── Operation object ──────────────────────────────────────────────────
    operation: dict = {
        "summary":  synopsis,
        "tags":     [" > ".join(tags)] if tags else ["Uncategorized"],
        "security": [{"bearerAuth": []}],
        "responses": responses,
    }
    if description:
        operation["description"] = description
    if deprecated:
        operation["deprecated"] = True

    # Extensions — consumer-useful: always emitted (role, behavior flags, warnings)
    if role:
        operation["x-cipp-role"] = role
    if has_scheduled:
        operation["x-cipp-scheduled-branch"] = True
    if has_dynamic:
        operation["x-cipp-dynamic-options"] = True
    if warnings:
        operation["x-cipp-warnings"] = warnings

    # Extensions — pipeline-introspection: only in verbose mode
    if verbose:
        if confidence != "high":
            operation["x-cipp-confidence"] = confidence
        if coverage != "FULL":
            operation["x-cipp-coverage-tier"] = coverage
        if downstream:
            operation["x-cipp-downstream"] = downstream
        if trust_level:
            operation["x-cipp-trust-level"] = trust_level
        if tested_version:
            operation["x-cipp-tested-version"] = tested_version
        if raw_request_body:
            operation["x-cipp-raw-body"] = True
        if has_data_transform:
            operation["x-cipp-data-transform"] = True
        if has_constructed:
            operation["x-cipp-constructed-body"] = True
        if has_external_form and external_components:
            operation["x-cipp-external-form-components"] = external_components

    if parameters:
        operation["parameters"] = parameters
    if request_body:
        operation["requestBody"] = request_body

    return {m.lower(): operation for m in methods}


def build_spec(endpoints: dict, title: str = "CIPP API", verbose: bool = False) -> dict:
    paths: dict = {}
    for ep_name, ep_data in endpoints.items():
        paths[f"/api/{ep_name}"] = build_path_entry(ep_data, verbose=verbose)

    description = (
        "CIPP-API is an Azure Function App providing the logic layer for the CIPP platform. "
        "This spec is auto-generated via static analysis of both the API (PowerShell) and "
        "frontend (React/Next.js) repositories."
    )
    if verbose:
        description += (
            " x-cipp-confidence: high=verified from both repos; medium=one source; low=speculative."
            " x-cipp-coverage-tier: FULL=complete; PARTIAL=one source or mixed;"
            " BLIND=pure blob passthrough (needs sidecar); ORPHAN=frontend-only."
        )

    info: dict = {
        "title":       title,
        "version":     "auto",
        "description": description,
        "x-cipp-docs": "https://docs.cipp.app",
    }
    if verbose:
        info["x-generated-by"] = "cipp-oas-generator/stage4_emitter.py"

    return {
        "openapi":    "3.1.0",
        "info":       info,
        "servers":    [{"url": "/api", "description": "CIPP API"}],
        "security":   [{"bearerAuth": []}],
        "components": SHARED_COMPONENTS,
        "paths":      paths,
    }


def group_by_domain(endpoints: dict) -> dict[str, dict]:
    domains: dict[str, dict] = defaultdict(dict)
    for ep_name, ep_data in endpoints.items():
        tags   = ep_data.get("tags", [])
        domain = tags[0] if tags else "Uncategorized"
        domains[domain][ep_name] = ep_data
    return dict(domains)


def run(endpoint_filter: str | None = None, verbose: bool = False) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    DOMAIN_DIR.mkdir(parents=True, exist_ok=True)

    # Load merged params — prefer endpoint-specific file if filtering
    if endpoint_filter:
        ep_file = OUT_DIR / f"merged-params-{endpoint_filter}.json"
        merged_path = ep_file if ep_file.exists() else OUT_DIR / "merged-params.json"
    else:
        merged_path = OUT_DIR / "merged-params.json"

    if not merged_path.exists():
        raise FileNotFoundError(
            f"merged-params.json not found at {merged_path}. Run stage3_merger.py first."
        )

    merged    = json.loads(merged_path.read_text())
    endpoints = merged["endpoints"]

    # Exclude ORPHAN endpoints from OAS output.
    # ORPHAN = frontend calls this URL but no Invoke-*.ps1 exists. In CIPP's single-
    # entrypoint router, these return 404 at runtime. Emitting them as valid OAS paths
    # would mislead SDK generators and API consumers into calling URLs that don't work.
    #
    # Exception: deprecated ORPHANs ARE included — callers need to know the endpoint
    # existed and is now gone, so they can clean up their integrations.
    #
    # The coverage-report.json probable_404s field is the right place for these bugs.
    _suppressed_orphans: list[str] = []
    _deprecated_orphans: list[str] = []
    filtered_endpoints: dict = {}
    for ep_name, ep_data in endpoints.items():
        if ep_data.get("coverage_tier") == "ORPHAN":
            if ep_data.get("deprecated", False):
                _deprecated_orphans.append(ep_name)
                filtered_endpoints[ep_name] = ep_data  # include deprecated ORPHANs
            else:
                _suppressed_orphans.append(ep_name)
                # excluded — do not add to filtered_endpoints
        else:
            filtered_endpoints[ep_name] = ep_data
    endpoints = filtered_endpoints

    if _suppressed_orphans:
        print(f"[Stage 4] ⚠ Suppressed {len(_suppressed_orphans)} ORPHAN endpoints from OAS "
              f"(no PS1 backing — would 404): {sorted(_suppressed_orphans)[:5]}"
              f"{'...' if len(_suppressed_orphans) > 5 else ''}")
    if _deprecated_orphans:
        print(f"[Stage 4]   {len(_deprecated_orphans)} deprecated ORPHANs included as deprecated: "
              f"{sorted(_deprecated_orphans)}")

    if endpoint_filter:
        ep = endpoints.get(endpoint_filter)
        if not ep:
            # Check if it was suppressed
            if endpoint_filter in _suppressed_orphans:
                print(f"[Stage 4] Endpoint '{endpoint_filter}' suppressed from OAS (ORPHAN — no PS1).")
            else:
                print(f"[Stage 4] Endpoint '{endpoint_filter}' not found in merged data.")
            return
        snippet = {f"/api/{endpoint_filter}": build_path_entry(ep, verbose=verbose)}
        print(f"\n── OAS 3.1 snippet for /api/{endpoint_filter} ──")
        print(json.dumps(snippet, indent=2))
        return

    # Full run
    full_spec    = build_spec(endpoints, verbose=verbose)
    unified_path = OUT_DIR / "openapi.json"
    unified_path.write_text(json.dumps(full_spec, indent=2))
    mode_label = " [verbose]" if verbose else " [clean]"
    print(f"[Stage 4] Unified spec → {unified_path} ({len(endpoints)} endpoints){mode_label}")

    domains = group_by_domain(endpoints)
    for domain, domain_eps in domains.items():
        safe = domain.lower().replace(" ", "-")
        domain_spec = build_spec(domain_eps, title=f"CIPP {domain} API", verbose=verbose)
        (DOMAIN_DIR / f"{safe}-openapi.json").write_text(json.dumps(domain_spec, indent=2))
    print(f"[Stage 4] Per-domain specs → {DOMAIN_DIR}/ ({len(domains)} domains)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CIPP OAS Emitter — Stage 4")
    parser.add_argument("--endpoint", help="Print OAS snippet for one endpoint only")
    parser.add_argument(
        "--verbose", action="store_true",
        help=(
            "Include pipeline-introspection extensions (x-cipp-confidence, "
            "x-cipp-coverage-tier, x-cipp-sources, x-cipp-data-transform, etc.). "
            "Default: clean spec with consumer-useful extensions only."
        ),
    )
    args = parser.parse_args()
    run(endpoint_filter=args.endpoint, verbose=args.verbose)
