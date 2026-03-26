"""
Stage 4: OAS 3.1 Emitter
Reads merged-params.json → emits valid OAS 3.1 JSON.

Outputs:
  out/openapi.json           — unified spec (all endpoints)
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

import re
import json
import argparse
from pathlib import Path
from collections import defaultdict
from config import (
    OUT_DIR, API_REPO, SHARED_PARAM_REFS,
    TYPE_HINTS, EXAMPLE_HINTS, ALWAYS_REQUIRED_BODY, ALWAYS_REQUIRED_QUERY,
    DYNAMIC_EXTENSION_PARENTS, TAG_DISPLAY_NAMES, TAG_ORDER,
)

_SCHEMAS_DIR = Path(__file__).parent / "schemas"

def _normalize_graph_schema(schema: dict) -> dict:
    """
    Normalize a Graph API response schema to match what CIPP actually returns.

    New-GraphGetRequest handles OData pagination internally — it follows @odata.nextLink
    and returns the fully-aggregated items list directly, stripping the collection envelope.
    So CIPP endpoints that proxy Graph collections return a plain JSON array, not
    {value: [...], @odata.nextLink: ...}.

    Rules:
      - Collection envelope ({type: object, properties: {value: {type: array, items: X}}})
        → unwrap to {type: array, items: X}
      - Single-item object schemas → keep as-is
      - Remove @odata.nextLink from any schema (never present in CIPP responses)
    """
    if schema.get("type") == "object":
        props = schema.get("properties", {})
        value_prop = props.get("value", {})
        if value_prop.get("type") == "array" and "items" in value_prop:
            # Collection envelope — return just the array
            return {"type": "array", "items": value_prop["items"]}
        # Single-item object — strip @odata.nextLink if present
        cleaned_props = {k: v for k, v in props.items() if k != "@odata.nextLink"}
        if cleaned_props != props:
            return {**schema, "properties": cleaned_props}
    return schema


def _load_graph_responses() -> dict:
    p = _SCHEMAS_DIR / "graph-responses.json"
    if not p.exists():
        return {}
    raw = json.loads(p.read_text(encoding="utf-8"))
    return {key: _normalize_graph_schema(schema) for key, schema in raw.items()}

_GRAPH_RESPONSES: dict = _load_graph_responses()

# Build a mapping: 'GET /path' → component name.
# Both _resolve_response_schema and build_spec use this so names stay consistent.
# Rebuilt by _refresh_graph_responses() at the start of run() so Stage 0 schema
# cache updates (written after this module is imported) are always picked up.


def _refresh_graph_responses() -> None:
    """Reload graph-responses.json and component names into module globals.

    Called at the top of run() so that Stage 0 (fetch_schemas) schema updates
    — which happen after this module is imported — are reflected in Stage 4
    output rather than requiring a second pipeline invocation.
    """
    global _GRAPH_RESPONSES, _GRAPH_RESPONSE_COMPONENT_NAMES
    _GRAPH_RESPONSES = _load_graph_responses()
    _GRAPH_RESPONSE_COMPONENT_NAMES = {
        key: _graph_path_to_component_name(key)
        for key in _GRAPH_RESPONSES
    }


def _graph_path_to_component_name(graph_key: str) -> str:
    """
    Derive a PascalCase OAS component name from a 'GET /some/path/{id}' key.

    Examples:
      'GET /users'                                        → 'GraphUsers'
      'GET /users/{id}'                                   → 'GraphUser'
      'GET /deviceManagement/managedDevices'              → 'GraphManagedDevices'
      'GET /deviceManagement/managedDevices/{id}'         → 'GraphManagedDevice'
      'GET /identity/conditionalAccess/namedLocations'    → 'GraphNamedLocations'
    """
    path = graph_key[len("GET "):].lstrip("/")
    # Drop {id} segments — they're structural, not semantic
    segments = [s for s in path.split("/") if not s.startswith("{")]
    # Use the last two meaningful segments to keep names distinct
    meaningful = segments[-2:] if len(segments) >= 2 else segments
    # CamelCase each segment (already camelCase from Graph — just uppercase the first letter)
    name = "".join(s[0].upper() + s[1:] for s in meaningful if s)
    # Strip trailing 's' only when it's a single item (path ends with {id})
    is_single = path.endswith("{id}") or path.endswith("{id})")
    if is_single and name.endswith("s"):
        name = name[:-1]
    return f"Graph{name}"


# Populated by _refresh_graph_responses() — see below.
_GRAPH_RESPONSE_COMPONENT_NAMES: dict[str, str] = {
    key: _graph_path_to_component_name(key)
    for key in _GRAPH_RESPONSES
}


def _resolve_response_schema(graph_calls: list[dict], http_methods: list[str]) -> dict | None:
    """
    Resolve a Graph response schema for GET endpoints with a single unambiguous Graph GET call.
    Normalizes {varname} → {id} before lookup so stage1 path tokens match schema cache keys.
    Returns a $ref to the named component (registered in components/schemas by build_spec).
    """
    if not any(m.upper() == "GET" for m in http_methods):
        return None
    get_calls = [c for c in graph_calls if c.get("method") == "GET"]
    if len(get_calls) != 1:
        return None
    norm_path = re.sub(r'\{[^}]+\}', '{id}', get_calls[0]['path'])
    key = f"GET {norm_path}"
    if key not in _GRAPH_RESPONSES:
        return None
    component_name = _GRAPH_RESPONSE_COMPONENT_NAMES[key]
    return {"$ref": f"#/components/schemas/{component_name}"}

_SCRIPT_DIR = Path(__file__).parent


# ── Tag display helpers ───────────────────────────────────────────────────────

def _display_tag(tags: list[str]) -> str:
    """
    Convert a raw folder-path tag list to its UI-aligned display string.
    Renames the first segment via TAG_DISPLAY_NAMES; sub-path parts pass through.
    e.g. ["Endpoint", "Autopilot"] → "Intune > Autopilot"
    e.g. [] → "Uncategorized"
    """
    if not tags:
        return "Uncategorized"
    first = TAG_DISPLAY_NAMES.get(tags[0], tags[0])
    return " > ".join([first] + list(tags[1:]))


def _tag_sort_key(tag: str) -> tuple[int, str]:
    """Sort key: position in TAG_ORDER for the section, then alpha within section."""
    section = tag.split(" > ")[0]
    idx = TAG_ORDER.index(section) if section in TAG_ORDER else len(TAG_ORDER)
    return (idx, tag)

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
            "schema": {"type": "string", "example": "example.onmicrosoft.com"},
        },
        "selectedTenants": {
            "name": "selectedTenants",
            "in": "query",
            "description": "Comma-separated list of tenant domains for bulk operations.",
            "required": False,
            "schema": {"type": "string", "example": "example.onmicrosoft.com"},
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
        return _inject_example(schema, name_lower)

    # TYPE_HINTS lookup (name-based global overrides in config)
    hint = TYPE_HINTS.get(name_lower)
    if hint:
        return _inject_example(dict(hint), name_lower)  # copy to avoid mutating the config

    # Param carries inline enum/format without an explicit type — apply to string default
    if param.get("enum") or param.get("format"):
        schema = {"type": "string"}
        if param.get("enum"):
            schema["enum"] = param["enum"]
        if param.get("format"):
            schema["format"] = param["format"]
        return _inject_example(schema, name_lower)

    # Default
    desc = param.get("description")
    schema = {"type": "string"}
    if desc:
        schema["description"] = desc
    return _inject_example(schema, name_lower)


def _inject_example(schema: dict, name_lower: str) -> dict:
    """Add example value to schema if available in EXAMPLE_HINTS. Skips $ref schemas."""
    if "$ref" in schema:
        return schema
    example = EXAMPLE_HINTS.get(name_lower)
    if example is not None and "example" not in schema:
        schema["example"] = example
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
        "required": name_lower in required_set or param.get("required", False),
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

        if name_lower in ALWAYS_REQUIRED_BODY or param.get("required", False):
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


def _emit_passthru_request_body(passthru_resolved: dict) -> dict:
    """Build oneOf requestBody for a passthru endpoint."""
    variants = passthru_resolved.get("variants", [])
    discriminator_param = passthru_resolved.get("discriminator_param", "Endpoint")

    one_of = []
    for v in variants:
        schema = dict(v.get("request_schema", {}))
        schema["title"] = f"GET {v['endpoint_value']}"
        one_of.append(schema)

    return {
        "content": {
            "application/json": {
                "schema": {
                    "oneOf": one_of,
                    "discriminator": {"propertyName": discriminator_param},
                }
            }
        }
    }


def _emit_passthru_response(passthru_resolved: dict) -> dict:
    """Build oneOf 200 response for a passthru endpoint."""
    variants = passthru_resolved.get("variants", [])

    one_of = []
    for v in variants:
        resp = v.get("response_schema")
        if resp is None:
            continue
        schema = dict(resp)
        schema["title"] = f"GET {v['endpoint_value']}"
        one_of.append(schema)

    return {
        "description": "Success",
        "content": {
            "application/json": {
                "schema": {
                    "oneOf": one_of,
                }
            }
        }
    }


def _emit_response_schema_from_inference(inferred: dict) -> dict:
    """Wrap an inferred response schema in OAS response format."""
    return {
        "description": "Success",
        "content": {
            "application/json": {
                "schema": inferred,
            }
        }
    }


def _emit_fallback_response(entity_name: str) -> dict:
    """Emit honest fallback response with additionalProperties + extension."""
    return {
        "description": "Success",
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "additionalProperties": True,
                    "x-cipp-response-source": entity_name,
                }
            }
        }
    }


def _emit_dual_mode_response(dual_mode_resolved: dict) -> dict:
    """Build oneOf 200 response for a dual-mode endpoint."""
    variants = dual_mode_resolved.get("variants", [])

    one_of = []
    for v in variants:
        schema = v.get("response_schema")
        if schema is None:
            continue
        variant_schema = dict(schema)
        variant_schema["title"] = v.get("title", v.get("mode", ""))
        variant_schema["description"] = v.get("description", "")
        one_of.append(variant_schema)

    if not one_of:
        return {
            "description": "Success",
            "content": {
                "application/json": {
                    "schema": {"type": "object", "additionalProperties": True}
                }
            }
        }

    return {
        "description": "Success",
        "content": {
            "application/json": {
                "schema": {
                    "oneOf": one_of,
                }
            }
        }
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
    raw_request_body       = ep.get("raw_request_body")        # sidecar escape hatch
    raw_response_body      = ep.get("raw_response_body")       # sidecar escape hatch
    # Resolve response schema via the Graph schema cache ($ref-based, not inline).
    inferred_response = _resolve_response_schema(ep.get("graph_calls", []), methods)

    # Fallback: use frontend $select / Graph-resource hints when no Graph $ref resolved.
    if not inferred_response and not ep.get("raw_response_body"):
        hints = ep.get("response_field_hints")
        if hints and hints.get("variants"):
            from stage2_frontend_scanner import select_fields_to_schema
            variant_schemas = []
            for v in hints["variants"]:
                resource   = v.get("endpoint_resource", "")
                sel_fields = v.get("select_fields", [])
                simple_cols = v.get("simple_columns", [])

                if sel_fields:
                    # $select was explicit — generate a precisely typed schema
                    item_schema = select_fields_to_schema(sel_fields)
                else:
                    # No $select — look up the full Graph schema for this resource path
                    norm_path = "/" + resource.rstrip("/")
                    norm_path = re.sub(r'\{[^}]+\}', '{id}', norm_path)
                    graph_key = f"GET {norm_path}"
                    cached = _GRAPH_RESPONSES.get(graph_key)
                    if cached:
                        # The cached schema is the collection envelope {value:[...]}
                        # Extract the items schema if present
                        value_prop = cached.get("properties", {}).get("value", {})
                        item_schema = value_prop.get("items") or cached
                    elif simple_cols:
                        # Fall back to simpleColumns as a minimal field list
                        item_schema = select_fields_to_schema(simple_cols)
                    else:
                        item_schema = {"type": "object", "additionalProperties": True}

                desc = f"Graph `{resource}` object"
                if sel_fields:
                    desc += f" — {len(sel_fields)} fields from $select"
                variant_schemas.append({
                    "type": "object",
                    "description": desc,
                    **(item_schema if item_schema.get("type") == "object"
                       else {"properties": {"value": {"type": "object"}}}),
                })

            if len(variant_schemas) == 1:
                items = variant_schemas[0]
            else:
                items = {"oneOf": variant_schemas}

            # CIPP's ListGraphRequest wraps results in {Results: [...]}
            inferred_response = {
                "type": "object",
                "properties": {
                    "Results": {
                        "type": "array",
                        "items": items,
                    },
                },
            }
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

    # ── Passthru endpoint emitter ─────────────────────────────────────────
    passthru = ep.get("passthru_resolved")
    if passthru and passthru.get("variants"):
        request_body = _emit_passthru_request_body(passthru)

    # ── Responses ─────────────────────────────────────────────────────────
    # Priority: sidecar raw_response_body > passthru oneOf > inferred Graph schema > StandardResults fallback.
    if raw_response_body:
        # Sidecar escape hatch — highest authority, replaces everything.
        responses = {}
        for code, resp_obj in raw_response_body.items():
            if isinstance(resp_obj, dict):
                responses[str(code)] = resp_obj
            else:
                responses[str(code)] = {"description": str(resp_obj)}
        # Always keep 401/403 — auth failures apply to everything
        if "401" not in responses:
            responses["401"] = _STANDARD_RESPONSES["401"]
        if "403" not in responses:
            responses["403"] = _STANDARD_RESPONSES["403"]
    elif passthru and passthru.get("variants"):
        # Passthru endpoint — emit oneOf response covering all known Graph variants.
        responses = {
            "200": _emit_passthru_response(passthru),
            "400": _STANDARD_RESPONSES["400"],
            "401": _STANDARD_RESPONSES["401"],
            "403": _STANDARD_RESPONSES["403"],
            "500": _STANDARD_RESPONSES["500"],
        }
        for code, desc in extra_responses.items():
            responses[str(code)] = desc if isinstance(desc, dict) else {"description": desc}
    elif (dual_mode := ep.get("dual_mode_resolved")) and dual_mode.get("variants"):
        # Dual-mode endpoint — emit oneOf response covering collection and single-item variants.
        has_variants = any(v.get("response_schema") for v in dual_mode["variants"])
        if has_variants:
            responses = {
                "200": _emit_dual_mode_response(dual_mode),
                "400": _STANDARD_RESPONSES["400"],
                "401": _STANDARD_RESPONSES["401"],
                "403": _STANDARD_RESPONSES["403"],
                "500": _STANDARD_RESPONSES["500"],
            }
            for code, desc in extra_responses.items():
                responses[str(code)] = desc if isinstance(desc, dict) else {"description": desc}
        else:
            responses = dict(_STANDARD_RESPONSES)
    elif (assembled := ep.get("assembled_response_schema")):
        # Standard response schema pre-assembled by passthru builder from computed fields.
        responses = {
            "200": _emit_response_schema_from_inference(assembled),
            "400": _STANDARD_RESPONSES["400"],
            "401": _STANDARD_RESPONSES["401"],
            "403": _STANDARD_RESPONSES["403"],
            "500": _STANDARD_RESPONSES["500"],
        }
        for code, desc in extra_responses.items():
            responses[str(code)] = desc if isinstance(desc, dict) else {"description": desc}
    elif inferred_response:
        # Graph response schema inferred from downstream URI + schema cache.
        responses = {
            "200": {
                "description": "Success",
                "content": {
                    "application/json": {"schema": inferred_response}
                },
            },
            "400": _STANDARD_RESPONSES["400"],
            "401": _STANDARD_RESPONSES["401"],
            "403": _STANDARD_RESPONSES["403"],
            "500": _STANDARD_RESPONSES["500"],
        }
        for code, desc in extra_responses.items():
            responses[str(code)] = desc if isinstance(desc, dict) else {"description": desc}
    else:
        # Standard fallback — StandardResults envelope.
        responses = dict(_STANDARD_RESPONSES)
        for code, desc in extra_responses.items():
            if isinstance(desc, dict):
                responses[str(code)] = desc
            else:
                responses[str(code)] = {"description": desc}

    # ── Operation object ──────────────────────────────────────────────────
    operation: dict = {
        "summary":  synopsis,
        "tags":     [_display_tag(tags)],
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
    all_tag_strings: set[str] = set()
    for ep_name, ep_data in endpoints.items():
        paths[f"/api/{ep_name}"] = build_path_entry(ep_data, verbose=verbose)
        all_tag_strings.add(_display_tag(ep_data.get("tags", [])))

    ordered_tags = sorted(all_tag_strings, key=_tag_sort_key)
    tags_array = [{"name": t} for t in ordered_tags]

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

    # Read API version from CIPP-API repo's version_latest.txt
    version = "unknown"
    version_file = API_REPO / "version_latest.txt"
    if version_file.exists():
        version = version_file.read_text(encoding="utf-8").strip()

    info: dict = {
        "title":       title,
        "version":     version,
        "description": description,
        "x-cipp-docs": "https://docs.cipp.app",
    }
    if verbose:
        info["x-generated-by"] = "cipp-oas-generator/stage4_emitter.py"

    # Merge Graph response schemas into components/schemas so they appear in the
    # spec's schema registry and tooling can render them by name.
    components = {
        **SHARED_COMPONENTS,
        "schemas": {
            **SHARED_COMPONENTS["schemas"],
            **{
                _GRAPH_RESPONSE_COMPONENT_NAMES[key]: schema
                for key, schema in _GRAPH_RESPONSES.items()
            },
        },
    }

    return {
        "openapi":    "3.1.0",
        "info":       info,
        "tags":       tags_array,
        "servers":    [{"url": "/api", "description": "CIPP API"}],
        "security":   [{"bearerAuth": []}],
        "components": components,
        "paths":      paths,
    }


def run(endpoint_filter: str | None = None, verbose: bool = False) -> None:
    # Reload schema cache so Stage 0 updates (written after import) are visible.
    _refresh_graph_responses()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

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
    _suppressed_api_only: list[str] = []
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
        elif not ep_data.get("call_sites"):
            # API-only endpoint — no frontend call sites detected.
            # The CIPP API for the frontend IS the API; endpoints not called
            # from the frontend are internal/unused and excluded from the spec.
            _suppressed_api_only.append(ep_name)
        else:
            filtered_endpoints[ep_name] = ep_data
    endpoints = filtered_endpoints

    if _suppressed_orphans:
        print(f"[Stage 4] WARN Suppressed {len(_suppressed_orphans)} ORPHAN endpoints from OAS "
              f"(no PS1 backing - would 404): {sorted(_suppressed_orphans)[:5]}"
              f"{'...' if len(_suppressed_orphans) > 5 else ''}")
    if _suppressed_api_only:
        print(f"[Stage 4] Suppressed {len(_suppressed_api_only)} API-only endpoints "
              f"(no frontend calls): {sorted(_suppressed_api_only)[:5]}"
              f"{'...' if len(_suppressed_api_only) > 5 else ''}")
    if _deprecated_orphans:
        print(f"[Stage 4]   {len(_deprecated_orphans)} deprecated ORPHANs included as deprecated: "
              f"{sorted(_deprecated_orphans)}")

    if endpoint_filter:
        ep = endpoints.get(endpoint_filter)
        if not ep:
            # Check if it was suppressed
            if endpoint_filter in _suppressed_orphans:
                print(f"[Stage 4] Endpoint '{endpoint_filter}' suppressed from OAS (ORPHAN - no PS1).")
            elif endpoint_filter in _suppressed_api_only:
                print(f"[Stage 4] Endpoint '{endpoint_filter}' suppressed from OAS (API-only - no frontend calls).")
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
    print(f"[Stage 4] Unified spec -> {unified_path} ({len(endpoints)} endpoints){mode_label}")



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
