"""
stage_passthru_builder.py — Resolve Graph passthru endpoints into oneOf variants.

Runs after Stage 2, before Stage 3.

Inputs:  out/endpoint-index.json, out/frontend-calls.json, schema-registry.json
Output:  out/passthru-resolved.json
"""
import json
from pathlib import Path

from config import PASSTHRU_ENDPOINTS, OUT_DIR, GRAPH_FIELD_TYPES

_SCRIPT_DIR = Path(__file__).parent
_REGISTRY_PATH = _SCRIPT_DIR / "schemas" / "schema-registry.json"


def _load_registry(path: Path | None = None) -> dict:
    """Load schema registry from disk."""
    p = path or _REGISTRY_PATH
    if not p.exists():
        return {"version": "1.0", "graph": {}, "exo": {}}
    return json.loads(p.read_text())


def _unwrap_graph_value_envelope(schema: dict) -> dict:
    """
    Unwrap Graph collection response envelope: {type: object, properties: {value: {type: array, items: ...}}}
    CIPP strips this wrapper — the actual response is the array contents.
    Returns the inner array schema, or the original schema if not wrapped.
    """
    if (
        schema.get("type") == "object"
        and "properties" in schema
        and "value" in schema["properties"]
    ):
        value_prop = schema["properties"]["value"]
        if value_prop.get("type") == "array":
            return value_prop
    return schema


def _resolve_variant(
    method: str,
    url_pattern: str,
    select: str | None,
    registry: dict,
) -> dict:
    """
    Resolve a single Graph URL against the registry.
    Returns a variant dict with request_schema, response_schema, and flags.
    """
    key = f"{method} {url_pattern}"
    graph_entry = registry.get("graph", {}).get(key)

    if graph_entry is None:
        return {
            "endpoint_value": url_pattern,
            "request_schema": None,
            "response_schema": None,
            "select_projected": False,
            "sidecar_candidate": True,
        }

    request_schema = graph_entry.get("request")
    response_schema = graph_entry.get("response")
    select_projected = False

    # Unwrap Graph {value: {type: array, items: ...}} envelope.
    # CIPP handles pagination and returns the array contents directly.
    if response_schema:
        response_schema = _unwrap_graph_value_envelope(response_schema)

    # Project response schema if $select is provided and select_fields exist
    if select and response_schema and graph_entry.get("select_fields"):
        selected = [f.strip() for f in select.split(",")]
        select_fields = graph_entry["select_fields"]
        projected_props = {}
        for field in selected:
            if field in select_fields:
                ft = select_fields[field]
                if isinstance(ft, str):
                    projected_props[field] = {"type": ft}
                else:
                    projected_props[field] = ft
            else:
                projected_props[field] = {"type": "string"}

        # Rebuild response schema with only projected fields
        projected_response = _project_response(response_schema, projected_props)
        response_schema = projected_response
        select_projected = True

    return {
        "endpoint_value": url_pattern,
        "request_schema": request_schema,
        "response_schema": response_schema,
        "select_projected": select_projected,
    }


def _project_response(original: dict, projected_props: dict) -> dict:
    """Replace properties in a response schema with projected subset."""
    if original.get("type") == "array" and "items" in original:
        return {
            "type": "array",
            "items": {
                "type": "object",
                "properties": projected_props,
            },
        }
    if original.get("type") == "object" and "properties" in original:
        return {
            "type": "object",
            "properties": projected_props,
        }
    # Fallback: wrap as array of objects (Graph collections)
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": projected_props,
        },
    }


def build_passthru_resolved(
    endpoint_index: dict,
    frontend_calls: dict,
    registry: dict,
) -> dict:
    """
    Build passthru-resolved data for all PASSTHRU_ENDPOINTS.

    Merges Stage 1 downstream_calls and Stage 2 passthru_calls,
    resolves each against the registry, assembles oneOf variants.
    """
    resolved: dict = {}

    for ep_name in PASSTHRU_ENDPOINTS:
        # Collect Graph URLs from Stage 1
        s1_data = endpoint_index.get(ep_name, {})
        s1_calls = s1_data.get("downstream_calls") or []

        # Collect Graph URLs from Stage 2
        s2_data = frontend_calls.get(ep_name, {})
        s2_calls = s2_data.get("passthru_calls") or []

        # Deduplicate by method|url, merging $select fields into the widest set.
        # e.g. /users with select "id,displayName" and /users with "id,mail"
        # becomes /users with select "id,displayName,mail" (union).
        url_map: dict[str, dict] = {}  # key: "METHOD|/url" -> {method, url, select_fields: set}
        unresolvable: list[dict] = []

        def _add_url(method: str, url: str, select: str | None) -> None:
            key = f"{method}|{url}"
            if key not in url_map:
                url_map[key] = {"method": method, "url": url, "select_fields": set()}
            if select:
                url_map[key]["select_fields"].update(
                    f.strip() for f in select.split(",") if f.strip()
                )

        for call in s1_calls:
            if call.get("service") != "graph":
                continue
            _add_url(call.get("method", "GET"), call.get("url_pattern"), call.get("select"))

        for call in s2_calls:
            if call.get("unresolvable"):
                unresolvable.append({
                    "graph_url": None,
                    "source_file": call.get("source_file"),
                    "sidecar_candidate": True,
                })
                continue
            url = call.get("graph_url")
            if url is None:
                continue
            _add_url("GET", url, call.get("select"))

        # Convert merged map to list, with select as comma-joined string or None
        graph_urls: list[dict] = []
        for entry in url_map.values():
            fields = entry["select_fields"]
            select = ",".join(sorted(fields)) if fields else None
            graph_urls.append({"method": entry["method"], "url": entry["url"], "select": select})

        # Resolve each URL against registry
        variants: list[dict] = []
        unresolved: list[dict] = list(unresolvable)

        for entry in graph_urls:
            variant = _resolve_variant(
                entry["method"], entry["url"], entry["select"], registry
            )
            if variant.get("sidecar_candidate"):
                unresolved.append({
                    "graph_url": entry["url"],
                    "sidecar_candidate": True,
                })
            else:
                # Add request schema with discriminator const
                req = variant.get("request_schema")
                variant["request_schema"] = {
                    "properties": {
                        "Endpoint": {"const": entry["url"]},
                        **(req.get("properties", {}) if req else {}),
                    }
                }
                if req and req.get("required"):
                    variant["request_schema"]["required"] = req["required"]
                variants.append(variant)

        resolved[ep_name] = {
            "coverage": "PASSTHRU",
            "discriminator_param": "Endpoint",
            "variants": variants,
            "unresolved": unresolved,
        }

    return resolved


def resolve_dual_mode_endpoints(
    endpoint_index: dict,
    registry: dict,
) -> dict:
    """Resolve dual-mode endpoints into collection + single-item variants."""
    resolved: dict = {}

    for ep_name, ep_data in endpoint_index.items():
        dm = ep_data.get("dual_mode")
        if not dm:
            continue

        disc_param = dm["discriminator_param"]
        coll_path = dm["collection_graph_path"]
        single_path = dm["single_item_graph_path"]

        # Get $select for collection mode from downstream_calls
        coll_select = None
        for call in (ep_data.get("downstream_calls") or []):
            if call.get("service") == "graph" and call.get("url_pattern") == coll_path:
                coll_select = call.get("select")
                break

        # Resolve collection variant (projected if select available)
        coll_variant = _resolve_variant("GET", coll_path, coll_select, registry)
        coll_response = coll_variant.get("response_schema") if not coll_variant.get("sidecar_candidate") else None

        # Resolve single-item variant (full entity, no projection)
        # Fallback to collection path if single-item has no response schema
        # (common for auto-discovered entries without $select data)
        single_variant = _resolve_variant("GET", single_path, None, registry)
        if single_variant.get("sidecar_candidate") or not single_variant.get("response_schema"):
            single_variant = _resolve_variant("GET", coll_path, None, registry)
        single_response = single_variant.get("response_schema") if not single_variant.get("sidecar_candidate") else None

        # Merge computed fields into both variants
        computed = ep_data.get("computed_fields") or []
        if computed:
            coll_response = _merge_computed_fields(coll_response, computed)
            single_response = _merge_computed_fields(single_response, computed)

        resolved[ep_name] = {
            "dual_mode": True,
            "discriminator_param": disc_param,
            "variants": [
                {
                    "mode": "collection",
                    "title": f"Collection (no {disc_param})",
                    "description": "Returns all items with projected fields",
                    "response_schema": coll_response,
                },
                {
                    "mode": "single_item",
                    "title": f"Single item ({disc_param} provided)",
                    "description": "Returns one item with full details",
                    "response_schema": single_response,
                },
            ],
        }

    return resolved


def enrich_registry(registry: dict, endpoint_index: dict) -> dict:
    """
    Auto-enrich the schema registry with Graph calls discovered by the scanner.

    Collects all Graph calls from endpoint_index (via graph_calls and
    downstream_calls), merges $select field unions per method+path, then:

    - Adds new entries with source="discovered" for calls not yet in the registry.
    - Updates existing source="discovered" entries with any newly seen select fields.
    - Never overwrites source="curated" entries.

    Returns a stats dict with:
        new_entries_added, entries_updated, curated_preserved,
        discovered_without_select
    """
    stats = {
        "new_entries_added": 0,
        "entries_updated": 0,
        "curated_preserved": 0,
        "discovered_without_select": 0,
    }

    graph_section = registry.setdefault("graph", {})

    # Phase 1: collect all (method, path) -> set of select field names
    # Sources: graph_calls list (path+method) and downstream_calls (service=graph)
    call_map: dict[str, set[str]] = {}  # key: "METHOD /path" -> set of field names

    for ep_name, ep_data in endpoint_index.items():
        # Gather keys from graph_calls (path+method, no select info here)
        for gc in (ep_data.get("graph_calls") or []):
            path = gc.get("path") or gc.get("url_pattern")
            method = (gc.get("method") or "GET").upper()
            if not path:
                continue
            key = f"{method} {path}"
            call_map.setdefault(key, set())

        # Gather select fields from downstream_calls where service=graph
        for dc in (ep_data.get("downstream_calls") or []):
            if dc.get("service") != "graph":
                continue
            path = dc.get("url_pattern") or dc.get("path")
            method = (dc.get("method") or "GET").upper()
            select_str = dc.get("select") or ""
            if not path:
                continue
            key = f"{method} {path}"
            call_map.setdefault(key, set())
            if select_str:
                for field in select_str.split(","):
                    field = field.strip()
                    if field:
                        call_map[key].add(field)

    # Phase 2: enrich registry
    for key, field_names in call_map.items():
        existing = graph_section.get(key)

        if existing is not None and existing.get("source") == "curated":
            stats["curated_preserved"] += 1
            continue

        # Build select_fields dict using GRAPH_FIELD_TYPES for known fields
        if field_names:
            select_fields: dict | None = {}
            for field in field_names:
                field_lower = field.lower()
                if field_lower in GRAPH_FIELD_TYPES:
                    select_fields[field] = GRAPH_FIELD_TYPES[field_lower]
                else:
                    select_fields[field] = "string"
        else:
            select_fields = None

        # Build response schema from select_fields (array of objects)
        if select_fields:
            props = {}
            for field, ft in select_fields.items():
                if isinstance(ft, str):
                    props[field] = {"type": ft}
                else:
                    props[field] = ft
            response_schema: dict | None = {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": props,
                },
            }
        else:
            response_schema = None

        if existing is None:
            # New entry
            graph_section[key] = {
                "source": "discovered",
                "request": None,
                "response": response_schema,
                "select_fields": select_fields,
            }
            stats["new_entries_added"] += 1
            if select_fields is None:
                stats["discovered_without_select"] += 1
        else:
            # Update existing discovered entry — merge select fields
            merged_fields = dict(existing.get("select_fields") or {})
            if select_fields:
                merged_fields.update(select_fields)

            # Rebuild response from merged fields
            if merged_fields:
                props = {}
                for field, ft in merged_fields.items():
                    if isinstance(ft, str):
                        props[field] = {"type": ft}
                    else:
                        props[field] = ft
                updated_response: dict | None = {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": props,
                    },
                }
            else:
                updated_response = existing.get("response")

            existing["select_fields"] = merged_fields if merged_fields else None
            existing["response"] = updated_response
            existing["source"] = "discovered"
            stats["entries_updated"] += 1

    return stats


def _merge_computed_fields(schema: dict | None, computed_fields: list[dict]) -> dict | None:
    """Merge PS1 computed fields into a response schema. Registry fields win on collision."""
    if not computed_fields:
        return schema

    import copy

    if schema is None:
        props = {}
        for f in computed_fields:
            props[f["name"]] = {"type": f["type"]}
        return {"type": "array", "items": {"type": "object", "properties": props}}

    schema = copy.deepcopy(schema)

    if schema.get("type") == "array" and "items" in schema:
        target = schema["items"]
    else:
        target = schema

    if "properties" not in target:
        target["properties"] = {}

    existing = target["properties"]
    for f in computed_fields:
        if f["name"] not in existing:
            existing[f["name"]] = {"type": f["type"]}

    return schema


def build_endpoint_responses(endpoint_index: dict, registry: dict) -> dict:
    """Build response schemas for standard endpoints (non-passthru, non-dual-mode).
    Resolves Graph calls against registry and merges in computed fields."""
    resolved: dict = {}

    for ep_name, ep_data in endpoint_index.items():
        if ep_data.get("dual_mode"):
            continue
        if ep_name in PASSTHRU_ENDPOINTS:
            continue

        computed = ep_data.get("computed_fields") or []
        graph_calls = ep_data.get("graph_calls", [])

        get_calls = [c for c in graph_calls if c["method"] == "GET"]
        schema = None

        if get_calls:
            call = get_calls[0]
            select = None
            for dc in (ep_data.get("downstream_calls") or []):
                if dc.get("service") == "graph" and dc.get("url_pattern") == call["path"]:
                    select = dc.get("select")
                    break
            variant = _resolve_variant("GET", call["path"], select, registry)
            if not variant.get("sidecar_candidate"):
                schema = variant.get("response_schema")

        if computed:
            schema = _merge_computed_fields(schema, computed)

        if schema:
            resolved[ep_name] = schema

    return resolved


def run(
    endpoint_filter: str | None = None,
    registry_path: Path | None = None,
) -> dict:
    """
    Main entry point. Reads stage outputs, resolves passthru variants, writes result.
    """
    # Load inputs
    suffix = f"-{endpoint_filter}" if endpoint_filter else ""
    idx_path = OUT_DIR / f"endpoint-index{suffix}.json"
    fe_path = OUT_DIR / f"frontend-calls{suffix}.json"

    idx_data = json.loads(idx_path.read_text()) if idx_path.exists() else {}
    fe_data = json.loads(fe_path.read_text()) if fe_path.exists() else {}

    endpoint_index = idx_data.get("endpoints", {})
    frontend_calls = fe_data.get("endpoints", {})

    registry = _load_registry(registry_path)

    result = build_passthru_resolved(endpoint_index, frontend_calls, registry)
    dual_mode_result = resolve_dual_mode_endpoints(endpoint_index, registry)
    standard_responses = build_endpoint_responses(endpoint_index, registry)

    # Write output
    out_path = OUT_DIR / f"passthru-resolved{suffix}.json"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_data = {
        "stage": "passthru_builder",
        "passthru_endpoints": result,
        "dual_mode_endpoints": dual_mode_result,
        "standard_responses": standard_responses,
    }
    out_path.write_text(json.dumps(out_data, indent=2) + "\n")
    dm_count = sum(len(v['variants']) for v in dual_mode_result.values())
    std_count = len(standard_responses)
    print(f"  passthru-resolved  ({sum(len(v['variants']) for v in result.values())} passthru + {dm_count} dual-mode variants + {std_count} standard responses)")

    return result
