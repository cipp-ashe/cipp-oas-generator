"""
Stage 2: Frontend Scanner
Scans the CIPP frontend (Next.js/React) for all API call sites.

Extracts per call site:
  - Endpoint name
  - HTTP method (GET/POST)
  - Query params (from inline data objects or URL template literals)
  - Form field names (from CippFormComponent, CippFormDomainSelector,
    CippFormLicenseSelector, CippFormUserSelector name= props)

Call patterns detected:
  ApiGetCall({ url: "/api/ListUsers", data: { tenantFilter, UserId } })
  ApiGetCallWithPagination({ url: "/api/...", data: { ... } })
  ApiPostCall({ url: "/api/AddUser", data: values })
  Template literal: url: `/api/Name?key=${val}&key2=${val2}`
  urlFromData pattern: ApiPostCall({ urlFromData: true }) + .mutate({ url: "/api/Name" })
  CippFormPage postUrl="/api/Name" — JSX prop pattern; inline fields captured,
    external child form components flagged (not followed — add sidecar).

Known gaps (static analysis limits — documented in output, never silent):
  See KNOWN_FRONTEND_GAPS in config.py.

Outputs: out/frontend-calls.json  (full corpus)
         out/frontend-calls-{Endpoint}.json  (single-endpoint run)
"""

import re
import json
import hashlib
import argparse
from pathlib import Path
from collections import defaultdict
from config import (
    FRONTEND_REPO, FRONTEND_SRC_ROOT, OUT_DIR,
    FRONTEND_NOISE, PARAM_NOISE,
    KNOWN_FRONTEND_GAPS,
    GRAPH_FIELD_TYPES,
    PASSTHRU_ENDPOINTS,
)

# ── Comment stripping ─────────────────────────────────────────────────────────

# Block comment pattern: /* ... */ (non-greedy, dotall so it spans newlines).
# Used to strip commented-out code before scanning for API calls so that
# endpoints inside /* ... */ blocks are not treated as active call sites.
_BLOCK_COMMENT_RE = re.compile(r'/\*.*?\*/', re.DOTALL)


def _strip_block_comments(content: str) -> str:
    """Remove /* ... */ block comments, preserving line count for error reporting."""
    # Replace each comment with the same number of newlines so line numbers stay valid.
    def _replace(m: re.Match) -> str:
        return '\n' * m.group(0).count('\n')
    return _BLOCK_COMMENT_RE.sub(_replace, content)


# ── Compiled patterns ─────────────────────────────────────────────────────────

# Static API URL in any property position: url: "/api/EndpointName"
# Accepts single, double, and backtick quotes.
STATIC_URL_RE = re.compile(
    r'url\s*:\s*(?P<q>["\' `])/api/(?P<ep>[A-Za-z][A-Za-z0-9_]*)(?P=q)'
)

# Template literal URL with inline query string:
#   url: `/api/EndpointName?key=${expr}&key2=${expr2}`
TEMPLATE_URL_RE = re.compile(
    r'url\s*:\s*`/api/(?P<ep>[A-Za-z][A-Za-z0-9_]*)\?(?P<qs>[^`\n]+)`'
)

# urlFromData pattern — URL passed at mutate() call time, not hook instantiation:
#   createSomething.mutate({ url: "/api/EndpointName", data: formData })
MUTATE_URL_RE = re.compile(
    r'\.mutate\s*\(\s*\{[^}]*?url\s*:\s*["\' `]\/api/(?P<ep>[A-Za-z][A-Za-z0-9_]*)["\' `]',
    re.DOTALL,
)

# CippFormPage postUrl="/api/EndpointName" — JSX attribute pattern.
# Matches only string literals (single or double quotes) — variable props are
# silently skipped, same as computed URL names in the other patterns.
CIPP_FORM_PAGE_URL_RE = re.compile(
    r'<CippFormPage\b[^>]*?\bpostUrl\s*=\s*["\'](?P<url>/api/(?P<ep>[A-Za-z][A-Za-z0-9_]*))["\']',
    re.DOTALL,
)

# Detect customDataformatter= prop on CippFormPage — signals schema transform.
# The value is a function; we don't parse it, just flag its presence.
CIPP_FORM_PAGE_TRANSFORM_RE = re.compile(
    r'\bcustomDataformatter\s*=\s*\{',
    re.DOTALL,
)

# Detect external form component children of CippFormPage.
# Matches JSX elements whose names look like form components: start with Cipp
# and contain "Form", "Add", or "Edit" — heuristic, informational only.
# False positives (e.g. CippFormCondition) are harmless — list is for sidecar authors.
EXTERNAL_FORM_COMPONENT_RE = re.compile(
    r'<(Cipp(?:Add|Edit|[A-Z]\w*Form)\w*)\b'
)

# Wizard components that encapsulate complex multi-step forms with hidden parameters.
# Static analysis cannot extract field names from these — manual sidecars required.
WIZARD_COMPONENTS: frozenset[str] = frozenset([
    'CippWizardOffboarding',
    'CippWizardBulkOptions',
    'CippWizardAutopilotOptions',
    'CippWizardAutopilotImport',
    'CippWizardGroupTemplates',
    'CippWizardAssignmentFilterTemplates',
    'CippWizardAppApproval',
    'CippIntunePolicy',
    'CippAlertsStep',
    'CippBaselinesStep',
    'CippNotificationsStep',
    'CippPSASyncOptions',
])

# Detect wizard component usage — matches import or JSX tag (with or without closing bracket).
# The scan_file function checks for any wizard component presence in the file content.
def detect_wizard_components(content: str) -> list[str]:
    """
    Detect wizard components in JSX file content.
    Returns list of wizard component names found.
    """
    found = []
    for wizard in WIZARD_COMPONENTS:
        if wizard in content:
            found.append(wizard)
    return sorted(found)

# Template literal query param extraction: key=${expr}
TEMPLATE_PARAM_RE = re.compile(r'([A-Za-z][A-Za-z0-9_]*)=\$\{[^}]+\}')

# Inline data object after a URL match: data: { key: val, key2: val2 }
# Window applied after match position — 2000 char cap prevents runaway matches.
INLINE_DATA_RE = re.compile(r'data\s*:\s*\{([^}]{1,2000})\}', re.DOTALL)
INLINE_KEY_RE  = re.compile(r'^(\w+)\s*:', re.MULTILINE)

# CippFormComponent name= prop
# Static string:  name="foo"  |  name='foo'
# Braced string:  name={"foo"}
# Dynamic expr:   name={someVar}  |  name={`field.${x}`}
FORM_NAME_STATIC_RE  = re.compile(
    r'<CippFormComponent\b[^>]*?\bname\s*=\s*["\']([A-Za-z][A-Za-z0-9_.[\]]*?)["\']',
    re.DOTALL,
)
FORM_NAME_BRACED_RE  = re.compile(
    r'<CippFormComponent\b[^>]*?\bname\s*=\s*\{([^}]+)\}',
    re.DOTALL,
)

# ── Tag-body scanner for full type inference ───────────────────────────────
# Captures the full attribute body of a <CippFormComponent ...> tag so we can
# extract name=, type=, multiple=, and options= from the same tag in one pass.
# Group 1: everything between <CippFormComponent and the closing > or />.
_CIPP_FC_TAG_RE = re.compile(
    r'<CippFormComponent\b((?:[^>]|(?<=>)(?!\s*[A-Z]))*?)(?:/>|>)',
    re.DOTALL,
)
# Fallback: simpler pattern that grabs up to 3000 chars — handles multi-line tags
# and avoids the look-behind complexity.
_CIPP_FC_TAG_BODY_RE = re.compile(
    r'<CippFormComponent\b([^>]{1,3000}?)(?:/>|>)',
    re.DOTALL,
)
_FC_ATTR_NAME_RE  = re.compile(r'\bname\s*=\s*["\']([A-Za-z][A-Za-z0-9_.[\]]*?)["\']')
_FC_ATTR_NAME_BRACED_RE = re.compile(r'\bname\s*=\s*\{([^}]+)\}')
_FC_ATTR_TYPE_RE  = re.compile(r'\btype\s*=\s*["\']([A-Za-z][A-Za-z0-9_]*)["\']')
_FC_ATTR_MULTI_RE = re.compile(r'\bmultiple\s*=\s*\{?\s*true\s*\}?', re.IGNORECASE)
_FC_OPTIONS_RE    = re.compile(r'\boptions\s*=\s*\{?\s*\[(.{1,1500}?)\]\s*\}?', re.DOTALL)
_FC_OPTION_VAL_RE = re.compile(r'\bvalue\s*:\s*["\']([^"\']+)["\']')

# CippFormComponent type= → OAS type descriptor.
# "string" means: use the OAS type string literally.
# dict means: use that dict as the schema (may contain enum, format, etc.).
# None means: default to string but note the type for description.
_FC_TYPE_TO_OAS: dict[str, str | dict] = {
    "textField":       "string",
    "textarea":        "string",
    "password":        "string",
    "switch":          "boolean",
    "checkbox":        "boolean",
    "number":          "number",
    "datePicker":      "string",    # format: date-time applied in make_static_field
    "dateTimePicker":  "string",    # format: date-time applied in make_static_field
    "select":          "string",    # overridden with enum if options= present
    "radio":           "string",    # overridden with enum if options= present
    "autoComplete":    "LabelValue",
    "richText":        "string",
    "hidden":          "string",
    "timePicker":      "string",
    "colorPicker":     "string",
}
_FC_DATE_TYPES: frozenset[str] = frozenset({"datePicker", "dateTimePicker", "timePicker"})

# Selector component tag body scanner — same multi-line approach.
# Group 1: component name, Group 2: tag body.
_SELECTOR_TAG_BODY_RE = re.compile(
    r'<(Cipp(?:FormDomainSelector|FormLicenseSelector|FormUserSelector))\b([^>]{1,1000}?)(?:/>|>)',
    re.DOTALL,
)
_SEL_ATTR_NAME_RE  = re.compile(r'\bname\s*=\s*["\' `]([A-Za-z][A-Za-z0-9_]*)["\' `]')
_SEL_ATTR_MULTI_RE = re.compile(r'\bmultiple\s*=\s*\{?\s*true\s*\}?', re.IGNORECASE)
# Selector component → base OAS type (before multiple wrapping)
_SELECTOR_BASE_TYPE: dict[str, str] = {
    "CippFormDomainSelector":  "LabelValue",
    "CippFormLicenseSelector": "array",    # already plural — array of SKU strings
    "CippFormUserSelector":    "LabelValue",
}

# Specialised selector components — each has a name= prop and produces a known
# field shape that TYPE_HINTS resolves (LabelValue, array, etc.).
# Pattern captures the name= value; shape is resolved downstream by TYPE_HINTS.
_SELECTOR_COMPONENT_NAMES = [
    "CippFormDomainSelector",   # → LabelValue (primDomain)
    "CippFormLicenseSelector",  # → array of SKU IDs (licenses)
    "CippFormUserSelector",     # → LabelValue (setManager, copyFrom, etc.)
]
SELECTOR_COMPONENT_RES: list[re.Pattern] = [
    re.compile(
        rf'<{component}\b[^>]*?\bname\s*=\s*["\' `]([A-Za-z][A-Za-z0-9_]*)["\' `]',
        re.DOTALL,
    )
    for component in _SELECTOR_COMPONENT_NAMES
]

# React Hook Form array index path normalisation: businessPhones[0] → businessPhones
ARRAY_INDEX_RE = re.compile(r'\[\d+\]')

# POST/mutation context detection — look within ±400 chars of the URL match.
# Covers ApiPostCall, ApiPatchCall, ApiDeleteCall, ApiPutCall (present or future),
# and direct .mutate() calls (used by urlFromData pattern).
# GET is the default — POST wins if any of these are visible in the window.
POST_CONTEXT_RE = re.compile(r'\b(?:ApiPostCall|ApiPatchCall|ApiDeleteCall|ApiPutCall|\.mutate)\b')

# Computed endpoint gate — skip, cannot resolve statically
COMPUTED_URL_RE = re.compile(r'url\s*:\s*`/api/\$\{')

# ── apiUrl prop / apiData patterns (CippDataTable, CippPageList, etc.) ────────

# apiUrl="/api/EndpointName" JSX prop — covers CippDataTable, CippPageList, and any component
# that accepts apiUrl as a prop.
APIURL_PROP_RE = re.compile(
    r'apiUrl\s*=\s*["\'](?P<url>/api/(?P<ep>[A-Za-z][A-Za-z0-9_]*))["\']'
)

# apiData={{ ... }} block — captures the inner content of the double-brace object.
# The 4000-char cap covers real-world apiData objects without runaway matching.
APIDATA_BLOCK_RE = re.compile(
    r'apiData\s*=\s*\{\{(?P<body>[^}]{1,4000}?)\}\}',
    re.DOTALL,
)

# Endpoint key inside apiData — the Graph resource path
ENDPOINT_KEY_RE = re.compile(r'\bEndpoint\s*:\s*["`\'](?P<val>[^"`\'\n]+)["`\']')

# $select key inside apiData — CSV list of field names
SELECT_KEY_RE = re.compile(r'\$select\s*:\s*["`\'](?P<val>[^"`\'\n]+)["`\']')

# simpleColumns={["field1","field2",...]} — displayed column names (subset of selected)
SIMPLE_COLUMNS_RE = re.compile(
    r'simpleColumns\s*=\s*\{?\s*\[(?P<cols>[^\]]{1,1000})\]',
    re.DOTALL,
)
SIMPLE_COL_VAL_RE = re.compile(r'["\']([A-Za-z][A-Za-z0-9_.]*)["\']')

# ── Passthru endpoint= extraction ────────────────────────────────────────────
# Matches Endpoint=/path in URL query string (static)
PASSTHRU_ENDPOINT_STATIC_RE = re.compile(
    r'Endpoint=(/[a-zA-Z0-9/_\-\.]+)',
    re.IGNORECASE,
)
# Matches $select=fields in URL query string
PASSTHRU_SELECT_RE = re.compile(
    r'\$select=([a-zA-Z0-9_,]+)',
    re.IGNORECASE,
)
# Matches Endpoint: "path" or Endpoint: "/path" in data/queryParams objects.
# The path may or may not start with / — real CIPP code uses both forms.
# Also matches backtick-quoted static strings like `users` (no interpolation).
PASSTHRU_ENDPOINT_DATA_RE = re.compile(
    r'''Endpoint\s*:\s*(?:["'](/?\s*[a-zA-Z][a-zA-Z0-9/_\-\.()' ]*?)["']|`([^`$]+)`)''',
    re.IGNORECASE,
)
# Matches $select: "fields" or $select=fields in data/queryParams
PASSTHRU_SELECT_QUERYPARAM_RE = re.compile(
    r'''\$select\s*[:=]\s*["']?([a-zA-Z0-9_,]+)["']?''',
    re.IGNORECASE,
)
# Detects computed/dynamic endpoint values (template literal with ${} interpolation)
PASSTHRU_ENDPOINT_DYNAMIC_RE = re.compile(
    r'Endpoint\s*:\s*`[^`]*\$\{',
    re.IGNORECASE,
)

# Inline data object key noise — these are call options, not endpoint params
_DATA_NOISE: set[str] = {
    "url", "data", "bulkrequest", "querykey", "relatedquerykeys",
    "waiting", "toast", "onresult", "staletime", "refetchonwindowfocus",
    "refetchonmount", "refetchinterval", "responsetype",
} | {p.lower() for p in PARAM_NOISE}

# Detect handler-constructed body: .mutate({ url: "...", data: varName })
# where varName is an identifier, not an inline object or `values`/`formValues`.
# Signals that the POST body was built in the handler, not submitted directly.
# This is a heuristic — false negatives are possible, false positives are harmless
# (they downgrade to flagged rather than generating wrong params).
_MUTATE_DATA_INLINE_RE = re.compile(
    r'\.mutate\s*\(\s*\{[^}]*?data\s*:\s*(?P<val>[^\s,}\n]+)',
    re.DOTALL,
)
# Tokens that indicate the data value IS the form values object (not constructed)
_FORM_VALUES_TOKENS: set[str] = {"values", "formvalues", "formdata", "data", "formcontrol.getvalues()"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def file_hash(path: Path) -> str:
    """Short SHA256 fingerprint for staleness detection."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def classify_call_type(content: str, url_pos: int) -> str:
    """
    Determine GET vs POST from a ±400-char window around the URL position.
    POST wins if ApiPostCall or .mutate is visible in the window; GET is default.
    """
    window = content[max(0, url_pos - 400): min(len(content), url_pos + 400)]
    return "POST" if POST_CONTEXT_RE.search(window) else "GET"


def extract_inline_query_params(content: str, url_pos: int) -> list[str]:
    """
    Find the data: { ... } block within 800 chars after the URL match.
    Return key names that aren't call-option noise.
    """
    segment = content[url_pos: url_pos + 800]
    m = INLINE_DATA_RE.search(segment)
    if not m:
        return []
    return [
        k for k in INLINE_KEY_RE.findall(m.group(1))
        if k.lower() not in _DATA_NOISE
    ]


def normalise_field_name(name: str) -> str:
    """Strip React Hook Form array index paths: businessPhones[0] → businessPhones."""
    return ARRAY_INDEX_RE.sub("", name)


def make_static_field(name: str, source: str, type_info: dict | None = None) -> dict:
    """
    Build a static form field record.

    type_info (optional): dict carrying any of:
      type    — OAS type string (e.g. "boolean", "string", "LabelValue", "array")
      enum    — list of allowed values (for radio/select fields)
      format  — OAS format string (e.g. "date-time")
      multiple — bool; if True and type is LabelValue, wraps as array-of-LabelValue
      items   — OAS items schema (for array types)
    """
    field: dict = {
        "name":       name,
        "in":         "body",
        "confidence": "high",
        "source":     source,
        "dynamic":    False,
    }
    if type_info:
        t = type_info.get("type")
        multiple = type_info.get("multiple", False)

        if t == "LabelValue":
            if multiple:
                field["type"] = "array"
                field["items"] = {"$ref": "#/components/schemas/LabelValue"}
            else:
                field["type"] = "LabelValue"
        elif t == "array":
            field["type"] = "array"
            if type_info.get("items"):
                field["items"] = type_info["items"]
        elif t:
            field["type"] = t
            if type_info.get("enum"):
                field["enum"] = type_info["enum"]
                if multiple:
                    # multi-select: emit as array of enum
                    field["type"] = "array"
                    field["items"] = {"type": "string", "enum": type_info["enum"]}
                    del field["enum"]
            if type_info.get("format"):
                field["format"] = type_info["format"]

    return field


def make_dynamic_field(expr: str) -> dict:
    return {
        "name":       expr,
        "in":         "body",
        "confidence": "low",
        "source":     "frontend_form_field_dynamic",
        "dynamic":    True,
    }


def _infer_fc_type(tag_body: str) -> dict | None:
    """
    Infer OAS type metadata from a CippFormComponent tag body.

    Returns a type_info dict suitable for make_static_field(), or None if the
    type prop is absent (caller falls back to string default).

    Handles:
      - type="switch" / type="checkbox"         → boolean
      - type="datePicker" / "dateTimePicker"     → string, format: date-time
      - type="autoComplete" + multiple={true}    → array of LabelValue
      - type="autoComplete" (single)             → LabelValue
      - type="radio" / "select" + options=[...]  → string enum (or array enum if multiple)
      - type="radio" / "select" no options       → string
      - type="number"                            → number
      - everything else                          → string
    """
    type_m = _FC_ATTR_TYPE_RE.search(tag_body)
    if not type_m:
        return None  # no type= prop — caller uses string default

    fc_type  = type_m.group(1)
    multiple = bool(_FC_ATTR_MULTI_RE.search(tag_body))
    oas_type = _FC_TYPE_TO_OAS.get(fc_type, "string")

    type_info: dict = {"type": oas_type, "multiple": multiple}

    # date types
    if fc_type in _FC_DATE_TYPES:
        type_info["format"] = "date-time"

    # radio / select — try to extract enum values from options=
    if fc_type in ("radio", "select"):
        opts_m = _FC_OPTIONS_RE.search(tag_body)
        if opts_m:
            values = _FC_OPTION_VAL_RE.findall(opts_m.group(1))
            if values:
                type_info["enum"] = values

    return type_info


def extract_form_fields(content: str) -> tuple[list[dict], list[dict]]:
    """
    Extract all form field names from a JSX file with full type inference.

    Returns (static_fields, dynamic_fields).
      static_fields  — names known at scan time; high confidence; type inferred where possible
      dynamic_fields — computed/template names; low confidence; flagged but kept

    Type inference (per field, not per file):
      CippFormComponent type="switch"/"checkbox"     → type: boolean
      CippFormComponent type="autoComplete"          → type: LabelValue
      CippFormComponent type="autoComplete" + multi  → type: array, items: LabelValue
      CippFormComponent type="radio"/"select"        → type: string, enum: [...]  (if options= found)
      CippFormComponent type="number"                → type: number
      CippFormComponent type="datePicker"            → type: string, format: date-time
      CippFormUserSelector multiple={true}           → type: array, items: LabelValue
      CippFormUserSelector (single)                  → type: LabelValue
      CippFormDomainSelector                         → type: LabelValue
      CippFormLicenseSelector                        → type: array (of SKU strings)
      All other / no type=                           → type: string (default — may be overridden by sidecar)
    """
    static:  list[dict] = []
    dynamic: list[dict] = []
    seen_static: set[str] = set()

    def add_static(name: str, source: str, type_info: dict | None = None) -> None:
        norm = normalise_field_name(name)
        if norm.lower() in FRONTEND_NOISE:
            return
        key = norm.lower()
        if key not in seen_static:
            seen_static.add(key)
            static.append(make_static_field(norm, source, type_info=type_info))

    # ── CippFormComponent — full tag-body scan (name + type + multiple + options) ──
    for tag_m in _CIPP_FC_TAG_BODY_RE.finditer(content):
        tag_body = tag_m.group(1)
        # Try static name first
        name_m = _FC_ATTR_NAME_RE.search(tag_body)
        if name_m:
            type_info = _infer_fc_type(tag_body)
            add_static(name_m.group(1), "frontend_form_field", type_info=type_info)
        else:
            # Try braced expression
            braced_m = _FC_ATTR_NAME_BRACED_RE.search(tag_body)
            if braced_m:
                expr = braced_m.group(1).strip()
                lit = re.match(r'''^["']([A-Za-z][A-Za-z0-9_.[\]]*)["']$''', expr)
                if lit:
                    type_info = _infer_fc_type(tag_body)
                    add_static(lit.group(1), "frontend_form_field", type_info=type_info)
                else:
                    dynamic.append(make_dynamic_field(expr))

    # ── Specialised selector components — tag body scan (name + multiple) ────────
    for tag_m in _SELECTOR_TAG_BODY_RE.finditer(content):
        comp_name = tag_m.group(1)
        tag_body  = tag_m.group(2)
        name_m    = _SEL_ATTR_NAME_RE.search(tag_body)
        if not name_m:
            continue
        name     = name_m.group(1)
        multiple = bool(_SEL_ATTR_MULTI_RE.search(tag_body))
        base_t   = _SELECTOR_BASE_TYPE.get(comp_name, "LabelValue")

        if base_t == "array":
            # CippFormLicenseSelector — always an array of SKU strings
            type_info = {"type": "array", "items": {"type": "string"}}
        elif multiple:
            # Multi-select user/domain selector → array of LabelValue
            type_info = {"type": "LabelValue", "multiple": True}
        else:
            type_info = {"type": "LabelValue", "multiple": False}

        add_static(name, "frontend_selector_component", type_info=type_info)

    # ── Legacy pass: catch any CippFormComponent tags the tag-body scanner missed ─
    # The tag-body RE uses [^>]{1,3000} which can fail on deeply nested JSX tags
    # (very uncommon but not impossible). Fall back to the original simple patterns
    # for any names not yet seen — these get no type info (string default).
    for m in FORM_NAME_STATIC_RE.finditer(content):
        add_static(m.group(1), "frontend_form_field")  # no-op if already seen

    for m in FORM_NAME_BRACED_RE.finditer(content):
        expr = m.group(1).strip()
        lit = re.match(r'''^["']([A-Za-z][A-Za-z0-9_.[\]]*)["']$''', expr)
        if lit:
            add_static(lit.group(1), "frontend_form_field")
        else:
            # Only add to dynamic if not already seen as static
            if expr not in {d["name"] for d in dynamic}:
                dynamic.append(make_dynamic_field(expr))

    # Legacy selector fallback (for any names not yet captured by tag-body scanner)
    for pat in SELECTOR_COMPONENT_RES:
        for m in pat.finditer(content):
            add_static(m.group(1), "frontend_selector_component")  # no-op if already seen

    return static, dynamic


def select_fields_to_schema(fields: list[str]) -> dict:
    """
    Convert a list of Graph $select field names to an OAS object schema.
    Uses GRAPH_FIELD_TYPES for known fields; defaults to string for unknowns.
    """
    properties = {}
    for field in fields:
        hint = GRAPH_FIELD_TYPES.get(field.lower())
        if hint is None:
            # Check for DateTime suffix pattern
            if field.lower().endswith("datetime"):
                schema = {"type": "string", "format": "date-time"}
            else:
                schema = {"type": "string"}
        elif isinstance(hint, str):
            schema = {"type": hint}
        else:
            schema = dict(hint)
        properties[field] = schema
    return {
        "type": "object",
        "properties": properties,
        "additionalProperties": True,
    }


def extract_api_data_calls(content: str, rel_path: str, fhash: str) -> list[dict]:
    """
    Extract API call records from apiUrl="/api/EndpointName" JSX prop patterns.

    For each apiUrl match, looks in the next 2000 chars for an apiData={{ ... }} block
    and extracts Endpoint and $select values. Also looks for simpleColumns within
    500 chars of the apiUrl match.

    Returns a list of call record dicts with all standard keys populated.
    """
    results = []
    for m in APIURL_PROP_RE.finditer(content):
        endpoint = m.group("ep")
        pos = m.start()

        # Look ahead 2000 chars for apiData block
        lookahead = content[pos: pos + 2000]
        endpoint_resource = None
        select_fields: list[str] = []

        apidata_m = APIDATA_BLOCK_RE.search(lookahead)
        if apidata_m:
            body = apidata_m.group("body")
            ep_m = ENDPOINT_KEY_RE.search(body)
            if ep_m:
                endpoint_resource = ep_m.group("val").strip()
            sel_m = SELECT_KEY_RE.search(body)
            if sel_m:
                raw = sel_m.group("val")
                select_fields = [f.strip() for f in raw.split(",") if f.strip()]

        # Look for simpleColumns within 500 chars of the apiUrl match
        simple_window = content[pos: pos + 500]
        simple_columns: list[str] = []
        sc_m = SIMPLE_COLUMNS_RE.search(simple_window)
        if sc_m:
            simple_columns = SIMPLE_COL_VAL_RE.findall(sc_m.group("cols"))

        results.append({
            "endpoint":                    endpoint,
            "method":                      "GET",
            "url_pattern":                 "apiurl_prop",
            "endpoint_resource":           endpoint_resource,
            "select_fields":               select_fields,
            "simple_columns":              simple_columns,
            "file":                        rel_path,
            "file_hash":                   fhash,
            "query_params":                [],
            "form_fields":                 [],
            "has_dynamic_fields":          False,
            "has_data_transform":          False,
            "has_constructed_body":        False,
            "has_external_form_component": False,
            "external_form_components":    [],
            "has_wizard_component":        False,
            "wizard_components":           [],
        })

    return results


def detect_constructed_body(content: str, mutate_match_start: int) -> bool:
    """
    Return True if the .mutate() call at mutate_match_start passes a locally
    constructed object as its data value (rather than form values directly).

    Looks for data: <identifier> where the identifier is NOT a known form-values
    token. This signals the handler built the POST body itself — field names in
    the form do not reliably reflect what the API receives.
    """
    segment = content[mutate_match_start: mutate_match_start + 400]
    m = _MUTATE_DATA_INLINE_RE.search(segment)
    if not m:
        return False
    val = m.group("val").strip().rstrip(",}").strip()
    # Inline object literal — not constructed
    if val.startswith("{"):
        return False
    # Known direct form value tokens — not constructed
    if val.lower() in _FORM_VALUES_TOKENS:
        return False
    # Anything else is treated as a constructed variable
    return True


def _extract_passthru_calls(
    content: str,
    passthru_endpoint: str,
    source_file: str,
) -> list[dict]:
    """
    Extract Graph endpoint= values from calls to a passthru endpoint.
    Returns list of {graph_url, select, source_file} dicts.

    Handles three real patterns from CIPP frontend:
    1. Endpoint=/path in URL query string
    2. Endpoint: "path" in data: {} or queryParams: {} blocks
    3. Endpoint: `template` with ${} interpolation (flagged unresolvable)
    """
    calls: list[dict] = []
    seen: set[str] = set()

    # ── Pattern 1: Endpoint= in URL query string ─────────────────────────
    url_pattern = re.compile(
        rf'/api/{re.escape(passthru_endpoint)}[^"\'`]*',
        re.IGNORECASE,
    )
    # Also detect ${} interpolation in URL query string (dynamic Endpoint)
    url_dynamic_re = re.compile(
        rf'/api/{re.escape(passthru_endpoint)}[^"\'`]*\$\{{',
        re.IGNORECASE,
    )
    for m in url_pattern.finditer(content):
        url_fragment = m.group(0)
        ep_match = PASSTHRU_ENDPOINT_STATIC_RE.search(url_fragment)
        if ep_match:
            graph_url = _normalize_endpoint_value(ep_match.group(1))
            select_match = PASSTHRU_SELECT_RE.search(url_fragment)
            select = select_match.group(1) if select_match else None
            key = f"{graph_url}|{select}"
            if key not in seen:
                seen.add(key)
                calls.append({
                    "graph_url": graph_url,
                    "select": select,
                    "source_file": source_file,
                })
        elif url_dynamic_re.match(content, m.start()):
            dyn_key = f"_url_dynamic_{m.start()}"
            if dyn_key not in seen:
                seen.add(dyn_key)
                calls.append({
                    "graph_url": None,
                    "select": None,
                    "source_file": source_file,
                    "unresolvable": True,
                })

    # ── Pattern 2 & 3: Endpoint in data/queryParams blocks ───────────────
    # Find each call-site URL reference, then scan FORWARD only for the
    # nearest Endpoint: value in the data/queryParams block.
    block_pattern = re.compile(
        rf'/api/{re.escape(passthru_endpoint)}',
        re.IGNORECASE,
    )
    for m in block_pattern.finditer(content):
        # Scan forward from the URL match to find the data block
        end = min(len(content), m.end() + 500)
        window = content[m.start():end]

        # Check for dynamic endpoint first (backtick with ${})
        if PASSTHRU_ENDPOINT_DYNAMIC_RE.search(window):
            dyn_key = f"_dynamic_{m.start()}"
            if dyn_key not in seen:
                seen.add(dyn_key)
                calls.append({
                    "graph_url": None,
                    "select": None,
                    "source_file": source_file,
                    "unresolvable": True,
                })
            continue

        # Check for static Endpoint in data/queryParams block
        ep_match = PASSTHRU_ENDPOINT_DATA_RE.search(window)
        if ep_match:
            raw_value = ep_match.group(1) or ep_match.group(2)
            if raw_value:
                graph_url = _normalize_endpoint_value(raw_value.strip())
                select_match = PASSTHRU_SELECT_QUERYPARAM_RE.search(window)
                select = select_match.group(1) if select_match else None
                key = f"{graph_url}|{select}"
                if key not in seen:
                    seen.add(key)
                    calls.append({
                        "graph_url": graph_url,
                        "select": select,
                        "source_file": source_file,
                    })

    return calls


def _normalize_endpoint_value(value: str) -> str:
    """Normalize an Endpoint value to a /path form for registry lookup."""
    value = value.strip()
    if not value.startswith("/"):
        value = "/" + value
    return value


def scan_file(jsx_path: Path) -> list[dict]:
    """
    Scan one JSX/JS file. Returns one call record per detected API call site.

    Form field extraction is intentionally file-scoped for inline-URL patterns
    (static, mutate, template) because these patterns co-locate fields and call
    site in the same file. For CippFormPage with external child components the
    fields cannot be extracted — the call record carries has_external_form_component
    instead, and Stage 3 emits an explicit mismatch note directing to a sidecar.
    
    Wizard component detection is also file-scoped — if any wizard component is
    detected in the file, the endpoint likely requires a manual sidecar to document
    the wizard's hidden parameters.
    """
    content  = _strip_block_comments(jsx_path.read_text(encoding="utf-8", errors="ignore"))
    rel_path = str(jsx_path.relative_to(FRONTEND_REPO))
    fhash    = file_hash(jsx_path)
    calls:   list[dict] = []

    static_fields, dynamic_fields = extract_form_fields(content)
    has_dynamic = bool(dynamic_fields)
    
    # Detect wizard components in this file
    wizard_components = detect_wizard_components(content)
    has_wizard = bool(wizard_components)

    # ── Static URL ────────────────────────────────────────────────────────
    for m in STATIC_URL_RE.finditer(content):
        endpoint = m.group("ep")
        pos      = m.start()
        if COMPUTED_URL_RE.search(content[max(0, pos - 10): pos + 60]):
            continue
        method = classify_call_type(content, pos)
        calls.append({
            "endpoint":              endpoint,
            "method":                method,
            "url_pattern":           "static",
            "query_params":          [
                {"name": k, "in": "query", "confidence": "high",
                 "source": "frontend_data_obj"}
                for k in extract_inline_query_params(content, pos)
            ] if method == "GET" else [],
            "form_fields":           static_fields if method == "POST" else [],
            "has_dynamic_fields":    has_dynamic,
            "has_data_transform":    False,
            "has_constructed_body":  False,
            "has_external_form_component": False,
            "external_form_components":    [],
            "has_wizard_component":  has_wizard,
            "wizard_components":     wizard_components,
            "file":                  rel_path,
            "file_hash":             fhash,
        })

    # ── urlFromData / .mutate() ───────────────────────────────────────────
    for m in MUTATE_URL_RE.finditer(content):
        constructed = detect_constructed_body(content, m.start())
        calls.append({
            "endpoint":              m.group("ep"),
            "method":                "POST",
            "url_pattern":           "mutate_urlfromdata",
            "query_params":          [],
            # If the body is handler-constructed, form fields are unreliable.
            # Still include them — they may partially overlap — but the flag
            # tells Stage 3 to emit a mismatch note and not trust completeness.
            "form_fields":           static_fields,
            "has_dynamic_fields":    has_dynamic,
            "has_data_transform":    False,
            "has_constructed_body":  constructed,
            "has_external_form_component": False,
            "external_form_components":    [],
            "has_wizard_component":  has_wizard,
            "wizard_components":     wizard_components,
            "file":                  rel_path,
            "file_hash":             fhash,
        })

    # ── Template literal URL with query string ────────────────────────────
    for m in TEMPLATE_URL_RE.finditer(content):
        calls.append({
            "endpoint":              m.group("ep"),
            "method":                "GET",
            "url_pattern":           "template_literal",
            "query_params":          [
                {"name": p, "in": "query", "confidence": "high",
                 "source": "frontend_url_template"}
                for p in TEMPLATE_PARAM_RE.findall(m.group("qs"))
            ],
            "form_fields":           [],
            "has_dynamic_fields":    False,
            "has_data_transform":    False,
            "has_constructed_body":  False,
            "has_external_form_component": False,
            "external_form_components":    [],
            "has_wizard_component":  has_wizard,
            "wizard_components":     wizard_components,
            "file":                  rel_path,
            "file_hash":             fhash,
        })

    # ── CippFormPage postUrl= ─────────────────────────────────────────────
    # Detect child form components — informational, for sidecar authors.
    # Exclude known non-field components that match the heuristic pattern.
    _NON_FORM_COMPONENTS: set[str] = {
        "CippFormCondition", "CippFormPage", "CippFormTenantSelector",
    }
    child_components = sorted({
        cm.group(1) for cm in EXTERNAL_FORM_COMPONENT_RE.finditer(content)
        if cm.group(1) not in _NON_FORM_COMPONENTS
    })

    for m in CIPP_FORM_PAGE_URL_RE.finditer(content):
        endpoint = m.group("ep")
        # Check for customDataformatter within 3000 chars of the match
        # (covers most JSX prop lists without risking cross-component false positives)
        vicinity = content[m.start(): min(len(content), m.start() + 3000)]
        has_transform = bool(CIPP_FORM_PAGE_TRANSFORM_RE.search(vicinity))
        has_external  = bool(child_components)
        calls.append({
            "endpoint":              endpoint,
            "method":                "POST",
            "url_pattern":           "cipp_form_page",
            "query_params":          [],
            # Include inline form fields from this file. If the form fields live
            # in an external component, static_fields will be empty and
            # has_external_form_component will be True — Stage 3 uses this to
            # emit the right mismatch note instead of silently leaving params empty.
            "form_fields":           static_fields,
            "has_dynamic_fields":    has_dynamic,
            "has_data_transform":    has_transform,
            "has_constructed_body":  False,
            "has_external_form_component": has_external,
            "external_form_components":    child_components,
            "has_wizard_component":  has_wizard,
            "wizard_components":     wizard_components,
            "file":                  rel_path,
            "file_hash":             fhash,
        })

    # ── apiUrl prop (CippDataTable, CippPageList, etc.) ───────────────────
    calls.extend(extract_api_data_calls(content, rel_path, fhash))

    # ── Passthru endpoint= extraction ─────────────────────────────────────
    # For any call record whose endpoint is a known passthru endpoint, run
    # _extract_passthru_calls on the file content and attach the results.
    # This runs once per passthru endpoint per file (not once per call record).
    passthru_endpoints_seen: set[str] = set()
    for call in calls:
        ep = call["endpoint"]
        if ep in PASSTHRU_ENDPOINTS and ep not in passthru_endpoints_seen:
            passthru_endpoints_seen.add(ep)
            call["passthru_calls"] = _extract_passthru_calls(content, ep, rel_path)

    return calls


def _field_richness(ff: dict) -> int:
    """
    Return a richness score for a form field record.
    Higher = more type information available.
    Used during consolidation to keep the richer record when the same field
    appears in multiple call sites with different type metadata.

    Scoring:
      +2  has a non-string type (boolean, number, LabelValue, array, etc.)
      +1  has enum values
      +1  has format
      +1  has items (array element schema)
      +1  confidence == "high"
    """
    score = 0
    t = ff.get("type", "string")
    if t and t != "string":
        score += 2
    if ff.get("enum"):
        score += 1
    if ff.get("format"):
        score += 1
    if ff.get("items"):
        score += 1
    if ff.get("confidence") == "high":
        score += 1
    return score


def consolidate(calls_by_endpoint: dict[str, list[dict]]) -> dict[str, dict]:
    """
    Merge all per-file call records for each endpoint into one consolidated record.

    Merging rules:
      Query params   — union by lowercase name; confidence upgraded to high if any site is high
      Form fields    — union by lowercase name (static only); richer type wins; confidence
                       upgraded to high if any site is high
      Methods        — union of all seen methods
      Call sites     — sorted unique file list
      url_patterns   — union of all detected patterns (preserved for Stage 3 tier decisions)
      Flags          — OR across all call records (any True → True in consolidated output)
      external_form_components — union of all detected component names

    Type richness during merge:
      When the same field appears in multiple call sites, the record with richer type
      metadata wins (non-string type > enum > format > items). This ensures that if
      one call site has type="boolean" for a switch field and another doesn't, the
      boolean wins over the string default. Confidence is always the max observed.
    """
    result: dict[str, dict] = {}

    for endpoint, calls in calls_by_endpoint.items():
        query:      dict[str, dict] = {}
        fields:     dict[str, dict] = {}
        methods:    set[str]        = set()
        sites:      list[str]       = []
        url_patterns: set[str]      = set()
        has_dynamic          = False
        has_data_transform   = False
        has_constructed_body = False
        has_external_form    = False
        has_wizard           = False
        external_components: set[str] = set()
        wizard_components:   set[str] = set()
        # Passthru Graph endpoint= calls — merged across all call sites.
        # De-duplicated by (graph_url, select) key; unresolvable entries kept.
        _passthru_calls: list[dict] = []
        _passthru_seen: set[str] = set()
        # $select field hints from apiurl_prop calls — keyed by endpoint_resource.
        # Track select_fields (richest list per resource) AND simple_columns as fallback.
        # Resources without $select are still tracked so stage4 can look them up
        # in the graph-responses cache.
        _select_by_resource:  dict[str, list[str]] = {}
        _columns_by_resource: dict[str, list[str]] = {}

        for call in calls:
            methods.add(call["method"])
            sites.append(call["file"])
            url_patterns.add(call.get("url_pattern", "unknown"))
            has_dynamic          = has_dynamic          or call.get("has_dynamic_fields",    False)
            has_data_transform   = has_data_transform   or call.get("has_data_transform",    False)
            has_constructed_body = has_constructed_body or call.get("has_constructed_body",  False)
            has_external_form    = has_external_form    or call.get("has_external_form_component", False)
            has_wizard           = has_wizard           or call.get("has_wizard_component",  False)
            external_components.update(call.get("external_form_components", []))
            wizard_components.update(call.get("wizard_components", []))

            # Collect passthru_calls from call records (set during scan_file)
            for pc in call.get("passthru_calls", []):
                if pc.get("unresolvable"):
                    _passthru_calls.append(pc)
                else:
                    key = f"{pc.get('graph_url')}|{pc.get('select')}"
                    if key not in _passthru_seen:
                        _passthru_seen.add(key)
                        _passthru_calls.append(pc)

            # Collect hints from apiurl_prop call records
            if call.get("url_pattern") == "apiurl_prop":
                resource = call.get("endpoint_resource")
                sel  = call.get("select_fields",  [])
                cols = call.get("simple_columns", [])
                if resource:
                    # Track richest $select list per resource
                    if sel:
                        existing = _select_by_resource.get(resource, [])
                        if len(sel) > len(existing):
                            _select_by_resource[resource] = sel
                    else:
                        # Register the resource even without $select so stage4
                        # can fall back to graph-responses.json for that path
                        _select_by_resource.setdefault(resource, [])
                    # Track simpleColumns as a secondary signal
                    if cols:
                        existing_cols = _columns_by_resource.get(resource, [])
                        if len(cols) > len(existing_cols):
                            _columns_by_resource[resource] = cols

            for qp in call.get("query_params", []):
                key = qp["name"].lower()
                if key not in query:
                    query[key] = qp
                elif qp["confidence"] == "high":
                    query[key] = {**query[key], "confidence": "high"}

            for ff in call.get("form_fields", []):
                if ff.get("dynamic"):
                    continue
                key = ff["name"].lower()
                if key not in fields:
                    fields[key] = ff
                else:
                    existing   = fields[key]
                    incoming   = ff
                    # Richness wins — keep the record with more type information.
                    # Always upgrade confidence if the incoming is high.
                    if _field_richness(incoming) > _field_richness(existing):
                        merged = dict(incoming)
                        # Preserve high confidence if existing already had it
                        if existing.get("confidence") == "high":
                            merged["confidence"] = "high"
                        fields[key] = merged
                    elif incoming.get("confidence") == "high":
                        fields[key] = {**existing, "confidence": "high"}

        # Build response schema variants — one per discovered endpoint_resource.
        # Each variant carries the $select fields (if any) and simpleColumns fallback.
        # Stage4 uses these to generate typed schemas; for resources with no $select,
        # it falls back to graph-responses.json for the full Graph object shape.
        _response_field_hints = None
        if _select_by_resource:
            variants = [
                {
                    "endpoint_resource": resource,
                    "select_fields":     _select_by_resource[resource],
                    "simple_columns":    _columns_by_resource.get(resource, []),
                }
                for resource in sorted(_select_by_resource)
            ]
            _response_field_hints = {"variants": variants}

        result[endpoint] = {
            "endpoint":                    endpoint,
            "methods":                     sorted(methods),
            "call_sites":                  sorted(set(sites)),
            "call_count":                  len(calls),
            "url_patterns":                sorted(url_patterns),
            "query_params":                list(query.values()),
            "body_params":                 list(fields.values()),
            "has_dynamic_fields":          has_dynamic,
            "has_data_transform":          has_data_transform,
            "has_constructed_body":        has_constructed_body,
            "has_external_form_component": has_external_form,
            "external_form_components":    sorted(external_components),
            "has_wizard_component":        has_wizard,
            "wizard_components":           sorted(wizard_components),
            "response_field_hints":        _response_field_hints,
            "passthru_calls":              _passthru_calls if _passthru_calls else None,
            "_source":                     "stage2_frontend_scanner",
        }

    return result


def run(endpoint_filter: str | None = None) -> dict:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if not FRONTEND_SRC_ROOT.exists():
        raise RuntimeError(
            f"Frontend src not found at {FRONTEND_SRC_ROOT}.\n"
            f"Set CIPP_FRONTEND_REPO env var (currently resolves to: {FRONTEND_REPO})"
        )

    jsx_files = sorted(
        f for f in (
            list(FRONTEND_SRC_ROOT.rglob("*.jsx")) +
            list(FRONTEND_SRC_ROOT.rglob("*.js"))
        )
        if "node_modules" not in f.parts
    )

    by_endpoint: dict[str, list[dict]] = defaultdict(list)
    scan_errors: list[dict] = []

    for jsx in jsx_files:
        try:
            for call in scan_file(jsx):
                ep = call["endpoint"]
                if endpoint_filter and ep.lower() != endpoint_filter.lower():
                    continue
                by_endpoint[ep].append(call)
        except Exception as e:
            scan_errors.append({
                "file":  str(jsx.relative_to(FRONTEND_REPO)),
                "error": str(e),
            })

    consolidated = consolidate(by_endpoint)

    output = {
        "stage":                 "2_frontend_scanner",
        "frontend_repo":         str(FRONTEND_REPO),
        "total_files_scanned":   len(jsx_files),
        "total_endpoints_found": len(consolidated),
        "known_gaps":            KNOWN_FRONTEND_GAPS,
        "scan_errors":           scan_errors,
        "endpoints":             consolidated,
    }

    out_path = (
        OUT_DIR / f"frontend-calls-{endpoint_filter}.json"
        if endpoint_filter
        else OUT_DIR / "frontend-calls.json"
    )
    out_path.write_text(json.dumps(output, indent=2))

    print(
        f"[Stage 2] Scanned {len(jsx_files)} files -> "
        f"{len(consolidated)} endpoints -> {out_path}"
    )
    if len(consolidated) == 0:
        print(
            f"[Stage 2] ⚠ Zero endpoints found. Either CIPP_FRONTEND_REPO is wrong "
            f"({FRONTEND_REPO}) or the ApiGetCall/ApiPostCall call patterns have changed."
        )
    if scan_errors:
        print(f"[Stage 2] ⚠ {len(scan_errors)} scan errors")

    # Summary of new flag counts — useful for auditing sidecar backlog
    n_transform   = sum(1 for v in consolidated.values() if v.get("has_data_transform"))
    n_external    = sum(1 for v in consolidated.values() if v.get("has_external_form_component"))
    n_constructed = sum(1 for v in consolidated.values() if v.get("has_constructed_body"))
    n_wizard      = sum(1 for v in consolidated.values() if v.get("has_wizard_component"))
    if n_external or n_transform or n_constructed or n_wizard:
        print(
            f"[Stage 2]   Sidecar candidates: "
            f"{n_wizard} wizard-component, "
            f"{n_external} external-form-component, "
            f"{n_transform} data-transform, "
            f"{n_constructed} constructed-body"
        )

    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CIPP Frontend Scanner — Stage 2")
    parser.add_argument("--endpoint", help="Scan only this endpoint (e.g. EditUser)")
    args = parser.parse_args()
    run(endpoint_filter=args.endpoint)
