"""
generate_sidecars.py — Auto-generate sidecar drafts for all flagged endpoints.

Reads pipeline output (must run pipeline.py first) and generates sidecars for
every endpoint in coverage-report.json sidecar_candidates, broken out by reason:

  blind               — API found, zero params. Derives body from frontend-only params
                        if available; otherwise emits a minimal stub with a note.
  external_form_component — Locates the component file, re-runs Stage 2 field
                        extraction on it, derives body from those fields.
  data_transform      — API body known; emits it with a warning that transform
                        is present and fields/types may differ in transit.
  naming_divergence   — Detects api_only + frontend_only pairs with similar names
                        (edit distance / common suffix patterns); emits corrections.

All generated sidecars are marked:
  trust_level: "inferred"
  _comment: "AUTO-GENERATED — review before publishing"

Existing sidecars are NEVER overwritten. Run with --dry-run to preview.

Usage:
  python3 generate_sidecars.py                # generate all missing
  python3 generate_sidecars.py --dry-run       # print what would be written
  python3 generate_sidecars.py --endpoint AddGroup   # single endpoint
  python3 generate_sidecars.py --reason blind  # one category only
  python3 generate_sidecars.py --list          # list all candidates with reasons
"""

import re
import json
import argparse
from pathlib import Path
from difflib import SequenceMatcher

from config import (
    OUT_DIR, SIDECARS_DIR, FRONTEND_SRC_ROOT, API_REPO,
    KNOWN_FRONTEND_GAPS,
)

# ── Re-use Stage 2 field extraction patterns (don't duplicate) ────────────────
from stage2_frontend_scanner import (
    FORM_NAME_STATIC_RE,
    FORM_NAME_BRACED_RE,
    SELECTOR_COMPONENT_RES,
    INLINE_DATA_RE,
    INLINE_KEY_RE,
    TEMPLATE_PARAM_RE,
    ARRAY_INDEX_RE,
    _DATA_NOISE,
    normalise_field_name,
    make_static_field,
)

_AUTO_COMMENT = "AUTO-GENERATED — review before publishing. trust_level=inferred."

# ── CippFormComponent type= → OAS type mapping ────────────────────────────────
# Maps the JSX `type=` prop value to an OAS type descriptor.
# Descriptor is a dict that gets merged into the param entry.
_FORM_TYPE_MAP: dict[str, dict] = {
    "textField":    {"type": "string"},
    "textarea":     {"type": "string"},
    "password":     {"type": "string", "format": "password"},
    "switch":       {"type": "boolean"},
    "checkbox":     {"type": "boolean"},
    "number":       {"type": "number"},
    "datePicker":   {"type": "string", "format": "date-time"},
    "dateTimePicker": {"type": "string", "format": "date-time"},
    # select/radio/autoComplete — type depends on whether multiple and options are present
    # handled separately below
    "select":       {"type": "string"},
    "radio":        {"type": "string"},
    "autoComplete": {"type": "object", "_ref": "LabelValue"},  # sentinel — replaced below
    "richText":     {"type": "string"},
    "hidden":       {"type": "string"},
}

# Selector component name → OAS type descriptor
_SELECTOR_TYPE_MAP: dict[str, dict] = {
    "CippFormDomainSelector": {"type": "object", "_ref": "LabelValue"},
    "CippFormLicenseSelector": {"type": "array",  "items": {"type": "string"},
                                "description": "Array of license SKU IDs"},
    "CippFormUserSelector":   {"type": "object", "_ref": "LabelValue"},
}

# Regex: capture name= AND type= from the same <CippFormComponent ...> tag.
# We scan the full tag body (up to 2000 chars) so attribute order doesn't matter.
# Group 1: tag body between <CippFormComponent and the closing > or />
_CIPP_FORM_TAG_RE = re.compile(
    r'<CippFormComponent\b([^>]{1,3000}?)(?:/>|>)',
    re.DOTALL,
)
_ATTR_NAME_RE  = re.compile(r'\bname\s*=\s*["\']([A-Za-z][A-Za-z0-9_.[\]]*?)["\']')
_ATTR_TYPE_RE  = re.compile(r'\btype\s*=\s*["\']([A-Za-z][A-Za-z0-9_]*)["\']')
_ATTR_MULTI_RE = re.compile(r'\bmultiple\s*=\s*\{?\s*true\s*\}?', re.IGNORECASE)

# Regex: extract inline options array values from radio/select fields.
# Matches options={[{...value: "x"...}, ...]} within 1200 chars after the tag open.
# Captures the value string from each {label: "...", value: "..."} entry.
_OPTIONS_ARRAY_RE = re.compile(
    r'\boptions\s*=\s*\{?\s*\[(.{1,1200}?)\]\s*\}?',
    re.DOTALL,
)
_OPTION_VALUE_RE = re.compile(r'\bvalue\s*:\s*["\']([^"\']+)["\']')

# Exchange ↔ Graph field name mapping — known corpus-wide divergences.
# Key: API field name (Exchange/CIPP convention), Value: frontend field name (Graph/display).
# These should be treated as confirmed matches regardless of similarity score.
_KNOWN_FIELD_ALIASES: dict[str, str] = {
    "Company":          "companyName",
    "Title":            "jobTitle",
    "MobilePhone":      "mobilePhone",
    "BusinessPhone":    "businessPhone",
    "StreetAddress":    "streetAddress",
    "PostalCode":       "postalCode",
    "CountryOrRegion":  "country",
    "OfficeLocation":   "officeLocation",
    "tenantID":         "tenantFilter",   # documented transform — keep for reference
}
_KNOWN_ALIASES_LOWER: dict[str, str] = {
    k.lower(): v.lower() for k, v in _KNOWN_FIELD_ALIASES.items()
}


# ── Similarity helpers ────────────────────────────────────────────────────────

def _similarity(a: str, b: str) -> float:
    """SequenceMatcher ratio on lowercase strings."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _likely_same_field(api_name: str, fe_name: str) -> bool:
    """
    Heuristic: are these two param names probably the same underlying field?

    Hard exclusions first:
      - Dotted paths: 'foo' vs 'foo.bar' is parent/child, NOT a name divergence.
        The frontend sends a nested object; the API reads the parent. Emitting
        remove_params for 'foo.bar' would delete a valid sub-field.
      - Known transform pairs (tenantID/tenantFilter) should not be auto-corrected —
        they're intentional API design, not a naming mistake.

    Confirmed matches:
      - _KNOWN_FIELD_ALIASES: corpus-wide Exchange↔Graph name mappings.

    Heuristic matches (require higher bar than prefix/containment):
      - High similarity (≥0.75) with minimum length guard
      - Common suffix strip (Name, Id, Filter, s) after length guard
    """
    a, b = api_name.lower(), fe_name.lower()
    if a == b:
        return False  # already matched — not a divergence

    # Hard exclusion: one is a dotted-path extension of the other
    # e.g. 'Compliance' vs 'Compliance.AppSync' — parent/child, not divergence
    if b.startswith(a + ".") or a.startswith(b + "."):
        return False

    # Hard exclusion: either name contains a dot — nested path, not a flat field
    # A flat API field name should never match a dotted frontend path
    if "." in a or "." in b:
        return False

    # Known alias — confirmed divergence
    if _KNOWN_ALIASES_LOWER.get(a) == b or _KNOWN_ALIASES_LOWER.get(b) == a:
        return True

    # One is contained in the other (e.g. phone / businessPhone)
    # But require min length 4 to avoid false matches on short names
    if len(a) >= 4 and len(b) >= 4:
        if a in b or b in a:
            return True

    # High similarity with length guard
    if len(a) >= 5 and len(b) >= 5 and _similarity(a, b) >= 0.75:
        return True

    return False


# ── Component file locator ────────────────────────────────────────────────────

def _find_component_file(component_name: str) -> Path | None:
    """
    Find the JSX file that exports a named component.
    Search order:
      1. src/components/CippComponents/<name>.jsx  (most common)
      2. src/components/CippFormPages/<name>.jsx   (form page components)
      3. src/components/CippWizard/<name>.jsx      (wizard components)
      4. Full rglob fallback across src/
    """
    search_dirs = [
        FRONTEND_SRC_ROOT / "components" / "CippComponents",
        FRONTEND_SRC_ROOT / "components" / "CippFormPages",
        FRONTEND_SRC_ROOT / "components" / "CippWizard",
    ]
    for d in search_dirs:
        p = d / f"{component_name}.jsx"
        if p.exists():
            return p
    # Rglob fallback
    hits = [
        p for p in FRONTEND_SRC_ROOT.rglob(f"{component_name}.jsx")
        if "node_modules" not in p.parts
    ]
    return hits[0] if len(hits) == 1 else None


def _infer_param_type(
    tag_body: str,
    form_type: str | None,
    is_selector: bool = False,
    selector_name: str = "",
    multiple: bool = False,
) -> dict:
    """
    Infer the OAS type descriptor for a single form field.

    Priority:
      1. Selector components — type determined by component name + multiple flag
      2. CippFormComponent type= prop — mapped via _FORM_TYPE_MAP
      3. Default: string

    For radio/select with inline options= — extract enum values.
    For autoComplete — LabelValue unless multiple=True (then array of LabelValue).
    For UserSelector/DomainSelector with multiple=True — array of LabelValue.
    """
    if is_selector:
        base = dict(_SELECTOR_TYPE_MAP.get(selector_name, {"type": "string"}))
        if multiple:
            # Wrap in array
            item_schema: dict = {"type": "object"}
            if base.get("_ref"):
                item_schema = {"$ref": f"#/components/schemas/{base['_ref']}"}
            return {"type": "array", "items": item_schema}
        # Single select
        if base.get("_ref"):
            ref = base.pop("_ref")
            return {"$ref": f"#/components/schemas/{ref}"}
        return base

    if not form_type:
        return {"type": "string"}

    base = dict(_FORM_TYPE_MAP.get(form_type, {"type": "string"}))

    # autoComplete — LabelValue shape
    if form_type == "autoComplete":
        if multiple:
            return {"type": "array", "items": {"$ref": "#/components/schemas/LabelValue"}}
        return {"$ref": "#/components/schemas/LabelValue"}

    # radio/select — try to extract enum from options=
    if form_type in ("radio", "select"):
        options_m = _OPTIONS_ARRAY_RE.search(tag_body)
        if options_m:
            values = _OPTION_VALUE_RE.findall(options_m.group(1))
            if values:
                if multiple:
                    return {"type": "array", "items": {"type": "string", "enum": values}}
                return {"type": "string", "enum": values}
        if multiple:
            return {"type": "array", "items": {"type": "string"}}
        return {"type": "string"}

    # _ref sentinel (shouldn't appear for non-selector paths but guard anyway)
    if base.get("_ref"):
        ref = base.pop("_ref")
        return {"$ref": f"#/components/schemas/{ref}"}

    if multiple:
        item = dict(base)
        return {"type": "array", "items": item}

    return base


def _extract_fields_from_component(component_path: Path) -> list[dict]:
    """
    Extract form field params from a JSX component file with full type inference.

    For each field extracted:
      - name: from name= prop
      - type: inferred from type= prop (CippFormComponent) or component type (selectors)
      - enum: extracted from inline options= array when type=radio/select
      - array: detected from multiple={true} on selector components

    Dot-notation handling:
      Field names like "Compliance.AppSync" and "Compliance.EDR" are grouped by their
      parent prefix ("Compliance") and emitted as a single parent field with type=object.
      The sub-properties are noted in the description. This prevents emitting
      "Compliance" as a flat string field AND prevents false naming-divergence matches
      against API params that only expose the parent object.
      Exception: if a field name IS the parent (i.e. "Compliance" appears without any
      dotted children), it is emitted as-is.

    Returns list of param dicts ready for sidecar add_params (type already set).
    """
    content = component_path.read_text(encoding="utf-8", errors="ignore")
    seen: set[str] = set()
    # Separate collection for dotted fields — grouped by parent before emitting
    dotted_by_parent: dict[str, list[str]] = {}  # parent_lower → [full_names...]
    flat_fields: list[dict] = []

    def _add(name: str, type_desc: dict, source: str) -> None:
        n  = normalise_field_name(name)
        nl = n.lower()
        if nl in seen or nl in _DATA_NOISE:
            return

        # Dotted-path field: group by parent, defer emission
        if "." in n:
            parent = n.split(".", 1)[0]
            pl = parent.lower()
            if pl not in dotted_by_parent:
                dotted_by_parent[pl] = []
            dotted_by_parent[pl].append(n)
            seen.add(nl)  # prevent duplicate dotted name
            return

        seen.add(nl)
        entry: dict = {
            "name":       n,
            "in":         "body",
            "required":   False,
            "confidence": "medium",
            "description": f"(auto) from {component_path.name}",
        }
        # Merge type descriptor
        if "$ref" in type_desc:
            entry["type"] = type_desc["$ref"].split("/")[-1]  # e.g. "LabelValue"
        elif type_desc.get("type") == "array":
            entry["type"] = "array"
            items = type_desc.get("items", {})
            if items.get("$ref"):
                entry["description"] += f" (array of {items['$ref'].split('/')[-1]})"
            elif items.get("enum"):
                entry["enum"] = items["enum"]
        else:
            entry["type"] = type_desc.get("type", "string")
            if type_desc.get("enum"):
                entry["enum"] = type_desc["enum"]
            if type_desc.get("format"):
                entry["format"] = type_desc["format"]
        flat_fields.append(entry)

    # ── CippFormComponent tags — capture name= AND type= from same tag ───────
    for tag_m in _CIPP_FORM_TAG_RE.finditer(content):
        tag_body = tag_m.group(1)
        name_m = _ATTR_NAME_RE.search(tag_body)
        if not name_m:
            continue
        name = name_m.group(1)
        type_m = _ATTR_TYPE_RE.search(tag_body)
        form_type = type_m.group(1) if type_m else None
        multiple = bool(_ATTR_MULTI_RE.search(tag_body))
        type_desc = _infer_param_type(tag_body, form_type, multiple=multiple)
        _add(name, type_desc, "component_form_field")

    # ── Selector components — capture name= AND multiple from same tag ────────
    _SELECTOR_TAG_RE = re.compile(
        r'<(Cipp(?:FormDomainSelector|FormLicenseSelector|FormUserSelector))\b([^>]{1,1000}?)(?:/>|>)',
        re.DOTALL,
    )
    for tag_m in _SELECTOR_TAG_RE.finditer(content):
        comp_name = tag_m.group(1)
        tag_body  = tag_m.group(2)
        name_m = re.search(r'\bname\s*=\s*["\' `]([A-Za-z][A-Za-z0-9_]*)["\' `]', tag_body)
        if not name_m:
            continue
        name = name_m.group(1)
        multiple = bool(_ATTR_MULTI_RE.search(tag_body))
        type_desc = _infer_param_type(tag_body, None, is_selector=True,
                                       selector_name=comp_name, multiple=multiple)
        _add(name, type_desc, f"component_selector:{comp_name}")

    # ── Inline data objects (ApiPostCall / mutate) ────────────────────────────
    for m in INLINE_DATA_RE.finditer(content):
        for key in INLINE_KEY_RE.findall(m.group(1)):
            if key.lower() not in _DATA_NOISE:
                _add(key, {"type": "string"}, "component_inline_data")

    # ── Emit dotted-path groups as nested object fields ───────────────────────
    # For each parent prefix that has one or more dotted children, emit the parent
    # as type: object. The sub-property names are captured in the description so
    # sidecar authors know what's inside.
    #
    # Example: Compliance.AppSync + Compliance.EDR → one "Compliance" field:
    #   {name: "Compliance", type: "object",
    #    description: "(auto) object with sub-properties: AppSync, EDR"}
    #
    # If the parent name was ALSO seen as a flat (non-dotted) field, the flat field
    # wins and we annotate its description with the sub-property names instead.
    for parent_lower, dotted_names in dotted_by_parent.items():
        # Recover the display-case parent name from the first dotted entry
        parent_display = dotted_names[0].split(".", 1)[0]
        sub_props = sorted({n.split(".", 1)[1] for n in dotted_names})
        sub_desc  = ", ".join(sub_props)

        if parent_lower in seen:
            # Parent was already emitted as a flat field — find it and annotate
            for f in flat_fields:
                if f["name"].lower() == parent_lower:
                    f["description"] = (
                        f.get("description", "")
                        + f" (object with sub-properties: {sub_desc})"
                    ).strip()
                    # Upgrade type to object if it was string
                    if f.get("type", "string") == "string":
                        f["type"] = "object"
                    break
        else:
            # Parent not emitted yet — create it now
            seen.add(parent_lower)
            flat_fields.append({
                "name":        parent_display,
                "in":          "body",
                "required":    False,
                "type":        "object",
                "confidence":  "medium",
                "description": (
                    f"(auto) from {component_path.name} — "
                    f"object with sub-properties: {sub_desc}"
                ),
            })

    return flat_fields


# ── Sidecar builders per category ─────────────────────────────────────────────

# ── PS1 string manipulation pattern detection ─────────────────────────────────
# Detects patterns in PowerShell that tell us something concrete about a param's type.
# These are all deterministic reads from the actual source file.

# $alias.field -split 'char' → field is a string, description notes split behavior
_PS_SPLIT_RE = re.compile(r'\$\w+\.(\w+)\s+-split\s+["\']([^"\']+)["\']', re.IGNORECASE)
# [bool]$alias.field / $alias.field -eq 'true' / -eq $true → field is boolean
_PS_BOOL_RE   = re.compile(
    r'(?:\[bool\]\s*\$\w+\.(\w+)'
    r'|\$\w+\.(\w+)\s*-eq\s*["\']true["\']'
    r'|\$\w+\.(\w+)\s*-eq\s*\$true)',
    re.IGNORECASE,
)
# [int]$alias.field / [datetime]$alias.field → cast gives us the type
_PS_CAST_RE   = re.compile(r'\[(\w+)\]\s*\$\w+\.(\w+)\b', re.IGNORECASE)
_PS_CAST_MAP  = {"int": "integer", "int32": "integer", "int64": "integer",
                 "datetime": "string",  # format: date-time added separately
                 "bool": "boolean", "double": "number", "float": "number"}


def _scan_ps1_type_hints(ps1_content: str) -> dict[str, dict]:
    """
    Scan a PS1 file for type signals on body params.
    Returns {field_name_lower: {type, ...}} for any field where type is deterministic.
    """
    hints: dict[str, dict] = {}

    # -split → string with description
    for m in _PS_SPLIT_RE.finditer(ps1_content):
        field = m.group(1).lower()
        sep   = m.group(2)
        hints[field] = {"type": "string",
                        "_split_note": f"split on '{sep}' server-side"}

    # boolean signals
    for m in _PS_BOOL_RE.finditer(ps1_content):
        field = (m.group(1) or m.group(2) or m.group(3) or "").lower()
        if field:
            hints[field] = {"type": "boolean"}

    # explicit casts
    for m in _PS_CAST_RE.finditer(ps1_content):
        cast_type = m.group(1).lower()
        field     = m.group(2).lower()
        oas_type  = _PS_CAST_MAP.get(cast_type)
        if oas_type:
            entry: dict = {"type": oas_type}
            if cast_type == "datetime":
                entry["format"] = "date-time"
            hints[field] = entry

    return hints


def _apply_ps1_hints(params: list[dict], ps1_hints: dict[str, dict]) -> list[dict]:
    """
    Apply PS1 type hints to a param list. Mutates in place, returns list.
    Only upgrades from the default 'string' — never downgrades a known type.
    """
    for p in params:
        nl = p["name"].lower()
        hint = ps1_hints.get(nl)
        if hint and p.get("type", "string") == "string":
            p["type"] = hint["type"]
            if hint.get("format"):
                p["format"] = hint["format"]
            if hint.get("_split_note"):
                p["description"] = (
                    p.get("description", "")
                    + f" [{hint['_split_note']}]"
                ).strip()
    return params


def _build_blind_sidecar(
    endpoint: str,
    api_data: dict,
    fe_data: dict,
    mm_issues: list[dict],
) -> dict:
    """
    BLIND: API found, zero params extracted from API AST.
    Strategy: use frontend-only params as the body contract — they're what the
    frontend actually sends, and the API blob-passes them downstream.
    Types are inferred from the frontend param metadata where available.
    PS1 type hints applied for any deterministic signals in the source file.
    """
    fe_body  = fe_data.get("body_params", [])
    fe_query = fe_data.get("query_params", [])

    # Load PS1 for type hint scanning
    ps1_hints: dict = {}
    ps1_file = api_data.get("file")
    if ps1_file:
        ps1_path = API_REPO / ps1_file
        if ps1_path.exists():
            ps1_hints = _scan_ps1_type_hints(
                ps1_path.read_text(encoding="utf-8", errors="ignore")
            )

    add_params: list[dict] = []

    for p in fe_query:
        entry: dict = {
            "name": p["name"], "in": "query",
            "required": False, "type": p.get("type", "string"),
            "confidence": "medium",
            "description": "(auto) detected in frontend call",
        }
        add_params.append(entry)

    for p in fe_body:
        # Preserve type if frontend scanner already inferred it (e.g. LabelValue)
        inferred_type = p.get("type", "string")
        entry = {
            "name": p["name"], "in": "body",
            "required": False, "type": inferred_type,
            "confidence": "medium",
            "description": "(auto) detected in frontend form/call",
        }
        add_params.append(entry)

    # Apply PS1 type hints — upgrades string defaults where deterministic
    add_params = _apply_ps1_hints(add_params, ps1_hints)

    downstream = api_data.get("downstream", [])
    passthrough = api_data.get("is_passthrough", False)

    notes_parts = [
        "BLIND endpoint — API reads $Request.Body as a blob with no direct field access.",
    ]
    if downstream:
        notes_parts.append(f"Downstream services: {', '.join(downstream)}.")
    if passthrough:
        notes_parts.append("Passthrough detected — body forwarded to downstream function.")
    if add_params:
        untyped = sum(1 for p in add_params if p.get("type") == "string")
        notes_parts.append(
            f"{len(add_params)} param(s) inferred from frontend."
            + (f" {untyped} defaulting to string — verify types." if untyped else "")
        )
    else:
        notes_parts.append(
            "No frontend params found. "
            "Endpoint may be backend-only or frontend call not detected by scanner."
        )

    sidecar: dict = {
        "_comment": _AUTO_COMMENT,
        "trust_level": "inferred",
        "notes": " ".join(notes_parts),
    }
    if add_params:
        sidecar["add_params"] = add_params

    return sidecar


def _build_external_form_sidecar(
    endpoint: str,
    api_data: dict,
    fe_data: dict,
    mm_issues: list[dict],
) -> dict:
    """
    External form component: CippFormPage postUrl= pattern where form fields live
    in a child component. Locate the component, extract its fields with full type
    inference (type= prop, selector types, enum from options=, multiple flag).
    PS1 type hints applied over the top for any deterministic server-side signals.
    """
    component_names = fe_data.get("external_form_components", [])
    add_params: list[dict] = []
    resolved_from: list[str] = []
    unresolved: list[str] = []

    for comp_name in component_names:
        comp_path = _find_component_file(comp_name)
        if comp_path:
            fields = _extract_fields_from_component(comp_path)
            for f in fields:
                if not any(p["name"].lower() == f["name"].lower() for p in add_params):
                    # Fields already have type/enum/format from _extract_fields_from_component
                    add_params.append(f)
            resolved_from.append(f"{comp_name} ({comp_path.name})")
        else:
            unresolved.append(comp_name)

    # Also include any params the API scanner found via blob alias access
    api_body = api_data.get("body_params", [])
    for p in api_body:
        if not any(x["name"].lower() == p["name"].lower() for x in add_params):
            add_params.append({
                "name": p["name"], "in": "body",
                "required": False, "type": "string",
                "confidence": "medium",
                "description": "(auto) from API scan",
            })

    # Apply PS1 type hints — upgrades string defaults where deterministic
    ps1_hints: dict = {}
    ps1_file = api_data.get("file")
    if ps1_file:
        ps1_path = API_REPO / ps1_file
        if ps1_path.exists():
            ps1_hints = _scan_ps1_type_hints(
                ps1_path.read_text(encoding="utf-8", errors="ignore")
            )
    add_params = _apply_ps1_hints(add_params, ps1_hints)

    # Count how many are still string (unresolved types)
    untyped = sum(1 for p in add_params if p.get("type") == "string"
                  and not p.get("enum"))

    notes_parts = ["External form component — fields from child component(s), not inline."]
    if resolved_from:
        notes_parts.append(f"Resolved: {', '.join(resolved_from)}.")
    if unresolved:
        notes_parts.append(
            f"Could not locate: {', '.join(unresolved)}. Add params manually."
        )
    if untyped:
        notes_parts.append(
            f"{untyped} param(s) still defaulting to string — verify types."
        )

    sidecar: dict = {
        "_comment": _AUTO_COMMENT,
        "trust_level": "inferred",
        "notes": " ".join(notes_parts),
    }
    if add_params:
        sidecar["add_params"] = add_params

    return sidecar


def _build_data_transform_sidecar(
    endpoint: str,
    api_data: dict,
    fe_data: dict,
    mm_issues: list[dict],
) -> dict:
    """
    Data transform: customDataformatter= is present on the CippFormPage.
    The frontend reshapes form fields before POST — param names/types in transit
    may differ from what the form shows. We know the API side; we don't know
    the exact transform output.

    Strategy:
    - API params are the authoritative post-transform names (what the server reads)
    - PS1 type hints applied for any deterministic type signals
    - Known Exchange↔Graph alias pairs flagged explicitly
    - Frontend-only params added with note that they may be renamed by transform
    """
    api_body  = api_data.get("body_params", [])
    api_query = api_data.get("query_params", [])
    fe_body   = fe_data.get("body_params", [])

    # Load PS1 for type hint scanning
    ps1_hints: dict = {}
    ps1_file = api_data.get("file")
    if ps1_file:
        ps1_path = API_REPO / ps1_file
        if ps1_path.exists():
            ps1_hints = _scan_ps1_type_hints(
                ps1_path.read_text(encoding="utf-8", errors="ignore")
            )

    seen: set[str] = {p["name"].lower() for p in api_body + api_query}
    add_params: list[dict] = []

    for p in api_body:
        entry: dict = {
            "name": p["name"], "in": "body",
            "required": False, "type": "string",
            "confidence": "medium",
            "description": "(auto) from API scan",
        }
        # Flag known Exchange↔Graph alias pairs
        alias_fe = _KNOWN_FIELD_ALIASES.get(p["name"])
        if alias_fe:
            entry["description"] += f" (frontend may send as '{alias_fe}' — transform renames)"
        add_params.append(entry)

    for p in api_query:
        add_params.append({
            "name": p["name"], "in": "query",
            "required": False, "type": "string",
            "confidence": "medium",
            "description": "(auto) from API scan",
        })

    for p in fe_body:
        if p["name"].lower() not in seen:
            seen.add(p["name"].lower())
            add_params.append({
                "name": p["name"], "in": "body",
                "required": False, "type": p.get("type", "string"),
                "confidence": "medium",
                "description": (
                    "(auto) frontend form field — "
                    "may be renamed/reshaped by customDataformatter before POST"
                ),
            })

    # Apply PS1 type hints — upgrades string defaults where deterministic
    add_params = _apply_ps1_hints(add_params, ps1_hints)

    sidecar: dict = {
        "_comment": _AUTO_COMMENT,
        "trust_level": "inferred",
        "x_cipp_warnings": [
            "customDataformatter is present on the CippFormPage for this endpoint. "
            "Form field names and/or types may be renamed or restructured before POST. "
            "Verify the transformer function in the frontend page file and correct "
            "param names/types accordingly."
        ],
        "notes": (
            "Data transform detected — CippFormPage has customDataformatter= prop. "
            "API params reflect post-transform field names (what the server reads). "
            "Frontend params are pre-transform and may differ. "
            "Verify the customDataformatter function and update this sidecar."
        ),
    }
    if add_params:
        sidecar["add_params"] = add_params

    return sidecar


def _build_naming_divergence_sidecar(
    endpoint: str,
    api_data: dict,
    fe_data: dict,
    mm_issues: list[dict],
) -> dict | None:
    """
    Naming divergence: api_only and frontend_only params that are probably the
    same field with different names. Emit remove_params for the wrong names and
    add_params with the correct merged name (API name wins for casing, frontend
    name noted in description).
    Returns None if no confident divergences found.
    """
    api_only  = [i["param"] for i in mm_issues if i["issue"] == "api_only"]
    fe_only   = [i["param"] for i in mm_issues if i["issue"] == "frontend_only"]

    if not api_only or not fe_only:
        return None

    pairs: list[tuple[str, str, float]] = []
    for a_name in api_only:
        for f_name in fe_only:
            if _likely_same_field(a_name, f_name):
                score = _similarity(a_name, f_name)
                pairs.append((a_name, f_name, score))

    if not pairs:
        return None

    # Deduplicate: each api_name and fe_name used at most once
    used_api: set[str] = set()
    used_fe:  set[str] = set()
    remove_params: list[str] = []
    add_params:    list[dict] = []

    for a_name, f_name, score in sorted(pairs, key=lambda x: -x[2]):
        if a_name in used_api or f_name in used_fe:
            continue
        used_api.add(a_name)
        used_fe.add(f_name)
        # API name wins — remove the frontend name, add API name with note
        remove_params.append(f_name)
        add_params.append({
            "name": a_name, "in": "body",
            "required": False, "type": "string",
            "confidence": "medium",
            "description": (
                f"(auto) API uses '{a_name}', frontend sends '{f_name}' — "
                f"likely the same field (similarity={score:.2f}). Verify and correct type."
            ),
        })

    if not add_params:
        return None

    return {
        "_comment": _AUTO_COMMENT,
        "trust_level": "inferred",
        "remove_params": remove_params,
        "add_params": add_params,
        "notes": (
            f"Naming divergence: {len(add_params)} param(s) where API and frontend "
            "use different names for the same field. Auto-matched by name similarity. "
            "Verify each pair before publishing."
        ),
    }


# ── Main orchestrator ─────────────────────────────────────────────────────────

def load_pipeline_outputs() -> tuple[dict, dict, dict, dict]:
    """Load all four pipeline output files. Raises if any are missing."""
    for name in ("endpoint-index.json", "frontend-calls.json",
                 "mismatch-report.json", "coverage-report.json"):
        if not (OUT_DIR / name).exists():
            raise FileNotFoundError(
                f"Missing pipeline output: {OUT_DIR / name}\n"
                "Run pipeline.py first."
            )
    return (
        json.loads((OUT_DIR / "endpoint-index.json").read_text())["endpoints"],
        json.loads((OUT_DIR / "frontend-calls.json").read_text())["endpoints"],
        json.loads((OUT_DIR / "mismatch-report.json").read_text())["mismatches"],
        json.loads((OUT_DIR / "coverage-report.json").read_text()),
    )


def generate_sidecar(
    endpoint: str,
    reason: str,
    api_data: dict,
    fe_data: dict,
    mm_issues: list[dict],
    dry_run: bool = False,
) -> dict | None:
    """
    Generate a sidecar for one endpoint. Returns the sidecar dict, or None if
    nothing useful could be generated. Writes to sidecars/ unless dry_run=True.
    """
    out_path = SIDECARS_DIR / f"{endpoint}.json"
    if out_path.exists():
        return None  # never overwrite

    sidecar: dict | None = None

    if reason == "blind":
        sidecar = _build_blind_sidecar(endpoint, api_data, fe_data, mm_issues)
    elif reason == "external_form_component":
        sidecar = _build_external_form_sidecar(endpoint, api_data, fe_data, mm_issues)
    elif reason == "data_transform":
        sidecar = _build_data_transform_sidecar(endpoint, api_data, fe_data, mm_issues)
    elif reason == "naming_divergence":
        sidecar = _build_naming_divergence_sidecar(endpoint, api_data, fe_data, mm_issues)
    else:
        return None

    if sidecar is None:
        return None

    if dry_run:
        print(f"\n── {endpoint} [{reason}] ──")
        print(json.dumps(sidecar, indent=2))
    else:
        SIDECARS_DIR.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(sidecar, indent=2))
        print(f"  ✓ {endpoint}.json [{reason}]")

    return sidecar


def run(
    endpoint_filter: str | None = None,
    reason_filter:   str | None = None,
    dry_run: bool = False,
    list_only: bool = False,
) -> None:
    api_eps, fe_eps, mismatches, cov = load_pipeline_outputs()

    by_reason: dict[str, list[str]] = cov["sidecar_candidates_by_reason"]

    # Build a naming_divergence bucket from mismatch report — not in coverage-report
    naming_divergence_eps: list[str] = []
    for ep_name, issues in mismatches.items():
        api_only = [i for i in issues if i["issue"] == "api_only"]
        fe_only  = [i for i in issues if i["issue"] == "frontend_only"]
        if api_only and fe_only:
            # Check if any pair passes the similarity test
            if any(
                _likely_same_field(a["param"], f["param"])
                for a in api_only for f in fe_only
            ):
                # Only flag if no sidecar already exists and not in another bucket
                if not (SIDECARS_DIR / f"{ep_name}.json").exists():
                    naming_divergence_eps.append(ep_name)

    all_by_reason: dict[str, list[str]] = {
        **by_reason,
        "naming_divergence": naming_divergence_eps,
    }

    if list_only:
        total = 0
        for reason, eps in all_by_reason.items():
            if reason_filter and reason != reason_filter:
                continue
            filtered = [e for e in eps if not endpoint_filter or e == endpoint_filter]
            if filtered:
                print(f"\n{reason} ({len(filtered)}):")
                for ep in sorted(filtered):
                    exists = "✓" if (SIDECARS_DIR / f"{ep}.json").exists() else "·"
                    print(f"  {exists} {ep}")
                total += len(filtered)
        print(f"\nTotal: {total} candidates")
        return

    generated = 0
    skipped_existing = 0
    skipped_empty = 0

    # Priority order: external_form_component first (highest signal),
    # then data_transform, then naming_divergence, then blind.
    priority = ["external_form_component", "data_transform", "naming_divergence", "blind"]
    processed: set[str] = set()

    for reason in priority:
        if reason_filter and reason != reason_filter:
            continue
        eps = all_by_reason.get(reason, [])
        for ep_name in sorted(eps):
            if endpoint_filter and ep_name != endpoint_filter:
                continue
            if ep_name in processed:
                continue
            processed.add(ep_name)

            if (SIDECARS_DIR / f"{ep_name}.json").exists():
                skipped_existing += 1
                continue

            api_data = api_eps.get(ep_name, {})
            fe_data  = fe_eps.get(ep_name, {})
            mm       = mismatches.get(ep_name, [])

            result = generate_sidecar(ep_name, reason, api_data, fe_data, mm, dry_run=dry_run)
            if result is not None:
                generated += 1
            else:
                skipped_empty += 1

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Generated: {generated}  "
          f"Skipped (existing): {skipped_existing}  "
          f"Skipped (no content): {skipped_empty}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="CIPP OAS Sidecar Generator — auto-drafts sidecars from pipeline output",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 generate_sidecars.py --list                     # show all candidates
  python3 generate_sidecars.py --dry-run                  # preview all, write nothing
  python3 generate_sidecars.py --reason external_form_component  # one category
  python3 generate_sidecars.py --endpoint AddGroup        # single endpoint
  python3 generate_sidecars.py                            # generate all missing
        """,
    )
    parser.add_argument("--dry-run",   action="store_true", help="Preview only, write nothing")
    parser.add_argument("--list",      action="store_true", help="List candidates and exit")
    parser.add_argument("--endpoint",  help="Process a single endpoint only")
    parser.add_argument("--reason",    help="Process one category only (blind|external_form_component|data_transform|naming_divergence)")
    args = parser.parse_args()
    run(
        endpoint_filter=args.endpoint,
        reason_filter=args.reason,
        dry_run=args.dry_run,
        list_only=args.list,
    )
