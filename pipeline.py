"""
CIPP OAS Generator — Orchestrator
Runs stages 1–4 in sequence. Each stage is independently importable and testable.

Usage:
  python3 pipeline.py                        # full corpus run
  python3 pipeline.py --endpoint EditUser    # single endpoint, all stages
  python3 pipeline.py --stage 1             # only stage 1, full corpus
  python3 pipeline.py --validate-only        # generate + diff vs committed spec (CI mode)

Single-endpoint runs write to out/*-{EndpointName}.json so they never
overwrite corpus output. You can run both safely in the same session.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import stage1_api_scanner
import stage2_frontend_scanner
import stage3_merger
import stage4_emitter
from config import (
    OUT_DIR, API_REPO, FRONTEND_REPO, HTTP_FUNCTIONS_ROOT, FRONTEND_SRC_ROOT,
    TYPE_HINTS, SHARED_PARAM_REFS, FRONTEND_NOISE, PS_NOISE, PARAM_NOISE,
    ALWAYS_REQUIRED_BODY, ALWAYS_REQUIRED_QUERY, DYNAMIC_EXTENSION_PARENTS,
    KNOWN_FRONTEND_GAPS,
)
# Import compiled patterns from stage scripts so check_patterns validates the
# actual patterns in use — not a parallel re-implementation that can drift.
from stage1_api_scanner import (
    FUNCTIONALITY_RE   as _S1_FUNCTIONALITY_RE,
    QUERY_PARAM_RE     as _S1_QUERY_PARAM_RE,
    BODY_DIRECT_RE     as _S1_BODY_DIRECT_RE,
)
from stage2_frontend_scanner import (
    STATIC_URL_RE           as _S2_STATIC_URL_RE,
    POST_CONTEXT_RE         as _S2_POST_CONTEXT_RE,
    FORM_NAME_STATIC_RE     as _S2_FORM_NAME_STATIC_RE,
    SELECTOR_COMPONENT_RES  as _S2_SELECTOR_COMPONENT_RES,
    CIPP_FORM_PAGE_URL_RE   as _S2_CIPP_FORM_PAGE_URL_RE,
    MUTATE_URL_RE           as _S2_MUTATE_URL_RE,
)

COMMITTED_SPEC = API_REPO / "openapi.json"


# ── Pattern health check ──────────────────────────────────────────────────────

def check_patterns() -> int:
    """
    Validate that the generator's core assumptions still hold against the live repos.
    Run this after a CIPP release to detect pattern drift before it silently degrades output.

    Checks:
      1. HTTP Functions root exists and has Invoke-*.ps1 files
      2. .FUNCTIONALITY marker is present in a sample of entrypoint files
      3. $Request.Query / $Request.Body access pattern is present in API files
      4. Frontend src root exists and has .jsx files
      5. ApiGetCall / ApiPostCall wrappers are present in the frontend
      6. CippFormComponent name= pattern is present
      7. Selector components (Domain/License/User) are present in frontend
      8. Route registration pattern (CIPPEndpoint) still uses /api/ prefix routing

    Returns 0 if all checks pass, 1 if any fail.
    """
    ok = True
    issues: list[str] = []

    def check(label: str, result: bool, detail: str = "") -> None:
        nonlocal ok
        status = "✓" if result else "✗"
        print(f"  {status} {label}" + (f" — {detail}" if detail else ""))
        if not result:
            ok = False
            issues.append(label)

    print("\n── Pattern Health Check ──")
    print(f"API repo:      {API_REPO}")
    print(f"Frontend repo: {FRONTEND_REPO}")

    # 1. API functions root
    ps1_files = list(HTTP_FUNCTIONS_ROOT.rglob("Invoke-*.ps1"))
    check("HTTP Functions root exists", HTTP_FUNCTIONS_ROOT.exists())
    check("Invoke-*.ps1 files found", len(ps1_files) > 0, f"{len(ps1_files)} files")

    if ps1_files:
        # 2. .FUNCTIONALITY marker — validated against the compiled pattern from Stage 1
        sample = ps1_files[:20]
        func_count = sum(
            1 for f in sample
            if _S1_FUNCTIONALITY_RE.search(f.read_text(encoding="utf-8", errors="ignore"))
        )
        check(
            ".FUNCTIONALITY marker present in sample",
            func_count > 0,
            f"{func_count}/{len(sample)} sampled files"
        )

        # 3. $Request.Query / $Request.Body access — validated against Stage 1 patterns
        req_count = sum(
            1 for f in sample
            if (
                _S1_QUERY_PARAM_RE.search(f.read_text(encoding="utf-8", errors="ignore"))
                or _S1_BODY_DIRECT_RE.search(f.read_text(encoding="utf-8", errors="ignore"))
            )
        )
        check(
            "$Request.Query/Body access pattern present",
            req_count > 0,
            f"{req_count}/{len(sample)} sampled files"
        )

    # 4. Frontend src root
    jsx_files = []
    if FRONTEND_SRC_ROOT.exists():
        jsx_files = [f for f in FRONTEND_SRC_ROOT.rglob("*.jsx") if "node_modules" not in f.parts]
    check("Frontend src root exists", FRONTEND_SRC_ROOT.exists())
    check("JSX files found", len(jsx_files) > 0, f"{len(jsx_files)} files")

    if jsx_files:
        # Sample frontend files
        fe_sample_texts = [f.read_text(encoding="utf-8", errors="ignore") for f in jsx_files[:50]]
        combined = "\n".join(fe_sample_texts)

        # 5. ApiGetCall / ApiPostCall wrappers — validated via Stage 2's POST_CONTEXT_RE
        has_get  = bool(re.search(r'\bApiGetCall\b', combined))
        has_post = bool(_S2_POST_CONTEXT_RE.search(combined))
        check("ApiGetCall wrapper present in frontend",  has_get)
        check("ApiPostCall/mutate wrapper present in frontend", has_post)

        # 6. CippFormComponent name= pattern — validated via Stage 2's compiled pattern
        has_form = bool(_S2_FORM_NAME_STATIC_RE.search(combined))
        check("CippFormComponent name= pattern present", has_form)

        # 7. Selector components — validated via Stage 2's compiled patterns
        for pat in _S2_SELECTOR_COMPONENT_RES:
            comp_name = pat.pattern.split(r'\b')[0].lstrip('<')
            has_comp = bool(pat.search(combined))
            check(f"{comp_name} present in frontend", has_comp,
                  "may not appear in 50-file sample — not fatal if absent")

        # 8. Static URL pattern — validated via Stage 2's compiled pattern
        has_api_prefix = bool(_S2_STATIC_URL_RE.search(combined))
        check("/api/ url prefix pattern present in frontend (static)", has_api_prefix)

        # 9. urlFromData / .mutate pattern — validated via Stage 2's compiled pattern
        has_mutate = bool(_S2_MUTATE_URL_RE.search(combined))
        check(
            ".mutate({ url: ... }) pattern present",
            has_mutate,
            "absent = urlFromData pattern may have changed"
        )

        # 10. CippFormPage postUrl= pattern — validated via Stage 2's compiled pattern
        has_form_page = bool(_S2_CIPP_FORM_PAGE_URL_RE.search(combined))
        check(
            "CippFormPage postUrl= pattern present in frontend",
            has_form_page,
            "absent = CippFormPage submission pattern may have changed"
        )

    print()
    if ok:
        print("✓ All pattern checks passed — generator assumptions still hold.")
        return 0
    else:
        print(f"✗ {len(issues)} check(s) failed — review before running full corpus:")
        for i in issues:
            print(f"    • {i}")
        print("\nIf patterns changed, update the relevant regex in the stage scripts.")
        return 1


# ── Endpoint parameter trace ──────────────────────────────────────────────────

def validate_endpoint(endpoint: str, param_filter: str | None = None) -> int:
    """
    Full transparency trace for one endpoint, optionally filtered to one parameter.

    For each parameter:
      Stage 1 — was it found? Direct ($Request.Body.X) or blob alias ($Alias.X)?
                 Was it filtered by PS_NOISE or PARAM_NOISE? Confidence assigned?
      Stage 2 — was it found in any frontend file? Which call site? Static form field
                 or inline data object? Was it filtered by FRONTEND_NOISE?
      Stage 3 — how were the two sources merged? Location conflict? Sidecar override?
                 Was it removed by sidecar remove_params?
      Stage 4 — what TYPE_HINT applied? Shared $ref? Required? Final OAS schema?

    Also shows what was actively filtered/suppressed at each stage so you can see
    why a param is absent as clearly as why one is present.

    Usage:
      python3 pipeline.py --validate-endpoint AddUser
      python3 pipeline.py --validate-endpoint AddUser --param displayName
    """
    print(f"\n{'='*64}")
    print(f"  Endpoint trace: {endpoint}" + (f"  [param: {param_filter}]" if param_filter else ""))
    print(f"{'='*64}")

    # ── Run all 4 stages to populate per-endpoint output files ────────────────
    print("\n[Running pipeline for endpoint...]")
    stage1_api_scanner.run(endpoint_filter=endpoint)
    stage2_frontend_scanner.run(endpoint_filter=endpoint)
    stage3_merger.run(endpoint_filter=endpoint)
    # Stage 4 in snippet mode (just prints, doesn't write)
    print()

    # ── Load per-stage output files ───────────────────────────────────────────
    def _load_ep_file(base: str) -> dict | None:
        ep_path  = OUT_DIR / f"{base}-{endpoint}.json"
        full_path = OUT_DIR / f"{base}.json"
        path = ep_path if ep_path.exists() else (full_path if full_path.exists() else None)
        if not path:
            return None
        data = json.loads(path.read_text())
        return data.get("endpoints", {}).get(endpoint)

    s1 = _load_ep_file("endpoint-index")
    s2 = _load_ep_file("frontend-calls")
    s3 = _load_ep_file("merged-params")

    mismatch_path = OUT_DIR / f"mismatch-report-{endpoint}.json"
    if not mismatch_path.exists():
        mismatch_path = OUT_DIR / "mismatch-report.json"
    mismatches_by_ep: dict = {}
    if mismatch_path.exists():
        mismatches_by_ep = json.loads(mismatch_path.read_text()).get("mismatches", {})

    sidecar_path = Path(__file__).parent / "sidecars" / f"{endpoint}.json"
    sidecar = json.loads(sidecar_path.read_text()) if sidecar_path.exists() else {}

    # ── Endpoint-level summary ────────────────────────────────────────────────
    print(f"\n── Endpoint metadata ──")
    if s1:
        print(f"  PS1 file:         {s1.get('file', '(not found)')}")
        print(f"  Stage 1 coverage: {s1.get('coverage', '?')}")
        print(f"  HTTP methods:     {s1.get('http_methods', [])}")
        print(f"  Role:             {s1.get('role') or '(none)'}")
        print(f"  Blob aliases:     {s1.get('body_blob_aliases', []) or '(none)'}")
        print(f"  Downstream:       {s1.get('downstream', []) or '(none)'}")
        print(f"  Scheduled branch: {s1.get('has_scheduled_branch', False)}")
        print(f"  Passthrough:      {s1.get('is_passthrough', False)}")
    else:
        print("  Stage 1: ✗ No API implementation found (ORPHAN or unknown endpoint)")

    if s2:
        print(f"\n  Frontend call sites ({s2.get('call_count', 0)}):")
        for site in s2.get("call_sites", []):
            print(f"    • {site}")
        print(f"  Frontend methods: {s2.get('methods', [])}")
        print(f"  Has dynamic fields: {s2.get('has_dynamic_fields', False)}")
    else:
        print("\n  Stage 2: ✗ No frontend call sites found")

    if sidecar:
        print(f"\n  Sidecar: ✓ {sidecar_path.name}")
        if sidecar.get("notes"):
            print(f"    Note: {sidecar['notes']}")
        if sidecar.get("override_confidence"):
            print(f"    override_confidence: {sidecar['override_confidence']}")
        if sidecar.get("remove_params"):
            print(f"    remove_params: {sidecar['remove_params']}")
        if sidecar.get("override_methods"):
            print(f"    override_methods: {sidecar['override_methods']}")
    else:
        print(f"\n  Sidecar: ✗ None")

    if s3:
        print(f"\n  Final coverage tier: {s3.get('coverage_tier', '?')}")
        print(f"  Final confidence:    {s3.get('confidence', '?')}")

    # ── Active filter sets (what the tool suppresses) ─────────────────────────
    if not param_filter:
        print(f"\n── Active filter sets ──")
        print(f"  PS_NOISE      ({len(PS_NOISE)}):    {sorted(PS_NOISE)}")
        print(f"  PARAM_NOISE   ({len(PARAM_NOISE)}):    {sorted(PARAM_NOISE)}")
        print(f"  FRONTEND_NOISE({len(FRONTEND_NOISE)}):    {sorted(FRONTEND_NOISE)}")
        print(f"  SHARED_PARAM_REFS: {list(SHARED_PARAM_REFS.keys())}")
        print(f"  DYN_EXT_PARENTS:   {sorted(DYNAMIC_EXTENSION_PARENTS)}")
        print(f"  ALWAYS_REQUIRED_BODY:  {sorted(ALWAYS_REQUIRED_BODY)}")
        print(f"  ALWAYS_REQUIRED_QUERY: {sorted(ALWAYS_REQUIRED_QUERY)}")

    # ── Per-parameter trace ───────────────────────────────────────────────────
    if not s3:
        print("\n  No merged output found — cannot trace parameters.")
        return 1

    all_params = (
        [p for p in s3.get("query_params", []) + s3.get("body_params", [])
         if param_filter is None or p["name"].lower() == param_filter.lower()]
    )

    if param_filter and not all_params:
        # param wasn't in final output — trace why
        print(f"\n── Parameter '{param_filter}' not in final output ──")
        _trace_absent_param(param_filter, s1, s2, sidecar)
        return 0

    print(f"\n── Parameter traces ({len(all_params)} params) ──")
    ep_mismatches = mismatches_by_ep.get(endpoint, [])

    for p in all_params:
        name      = p["name"]
        name_l    = name.lower()
        in_loc    = p.get("in", "?")
        sources   = p.get("sources", [])
        conf      = p.get("confidence", "?")
        is_shared = p.get("is_shared_ref", False)
        is_sidecar_only = sources == ["sidecar"]

        print(f"\n  ┌─ {name}  [{in_loc}]  confidence={conf}  sources={sources}")

        # Stage 1 trace
        s1_params = (s1.get("query_params", []) + s1.get("body_params", [])) if s1 else []
        s1_match  = next((x for x in s1_params if x["name"].lower() == name_l), None)
        if s1_match:
            src_tag = s1_match.get("source", "?")
            blob_note = ""
            if src_tag == "ast_blob":
                aliases = s1.get("body_blob_aliases", [])
                blob_note = f" (blob alias: {aliases})"
            print(f"  │ Stage 1: ✓ found — source={src_tag}{blob_note}  confidence={s1_match.get('confidence','?')}")
        elif is_sidecar_only:
            print(f"  │ Stage 1: — not scanned (sidecar-only param)")
        else:
            print(f"  │ Stage 1: ✗ not found in API AST")
            # Check if it might have been filtered
            if name_l in PS_NOISE:
                print(f"  │          ↳ suppressed by PS_NOISE")
            if name_l in PARAM_NOISE:
                print(f"  │          ↳ suppressed by PARAM_NOISE")

        # Stage 2 trace
        s2_params = (s2.get("query_params", []) + s2.get("body_params", [])) if s2 else []
        s2_match  = next((x for x in s2_params if x["name"].lower() == name_l), None)
        if s2_match:
            src_tag = s2_match.get("source", "?")
            print(f"  │ Stage 2: ✓ found — source={src_tag}  confidence={s2_match.get('confidence','?')}")
        elif is_sidecar_only:
            print(f"  │ Stage 2: — not scanned (sidecar-only param)")
        else:
            print(f"  │ Stage 2: ✗ not found in frontend")
            if name_l in FRONTEND_NOISE:
                print(f"  │          ↳ suppressed by FRONTEND_NOISE (UI-only field)")

        # Stage 3 merge decision
        mismatch = next((m for m in ep_mismatches if m.get("param", "").lower() == name_l), None)
        if is_sidecar_only:
            print(f"  │ Stage 3: sidecar addition — bypasses merge logic")
        elif s1_match and s2_match:
            print(f"  │ Stage 3: ✓ both sources present → confidence upgraded to HIGH")
            if mismatch:
                print(f"  │          ↳ ⚠ {mismatch['issue']}: {mismatch['detail']}")
        elif s1_match:
            print(f"  │ Stage 3: one source (API only) → confidence={s1_match.get('confidence','?')} retained")
            if mismatch:
                print(f"  │          ↳ {mismatch['issue']}: {mismatch['detail']}")
        elif s2_match:
            print(f"  │ Stage 3: one source (frontend only) → confidence downgraded to MEDIUM")
            if mismatch:
                print(f"  │          ↳ {mismatch['issue']}: {mismatch['detail']}")

        # Sidecar check
        if sidecar:
            removed = {r.lower() for r in sidecar.get("remove_params", [])}
            added_names = {a["name"].lower() for a in sidecar.get("add_params", [])}
            if name_l in removed:
                print(f"  │ Sidecar: ↳ param was removed by remove_params (should not appear here)")
            elif name_l in added_names:
                sc_entry = next(a for a in sidecar["add_params"] if a["name"].lower() == name_l)
                print(f"  │ Sidecar: ✓ add_params entry: {sc_entry}")

        # Stage 4 schema decision
        if is_shared:
            ref = SHARED_PARAM_REFS.get(name_l, "?")
            print(f"  │ Stage 4: shared $ref → {ref}")
        else:
            hint = TYPE_HINTS.get(name_l)
            sidecar_type = next(
                (a.get("type") for a in sidecar.get("add_params", [])
                 if a["name"].lower() == name_l and a.get("type")),
                None
            )
            if sidecar_type:
                print(f"  │ Stage 4: schema from sidecar type='{sidecar_type}'")
            elif hint:
                print(f"  │ Stage 4: TYPE_HINT → {json.dumps(hint)}")
            else:
                print(f"  │ Stage 4: no hint → default schema: {{\"type\": \"string\"}}")

            if name_l in ALWAYS_REQUIRED_BODY and in_loc == "body":
                print(f"  │          required=true (ALWAYS_REQUIRED_BODY)")
            elif name_l in ALWAYS_REQUIRED_QUERY and in_loc == "query":
                print(f"  │          required=true (ALWAYS_REQUIRED_QUERY)")

        print(f"  └─ final: in={in_loc}  conf={conf}  sources={sources}")

    # ── Params that were suppressed (visible trace of what was dropped) ────────
    if not param_filter:
        _trace_suppressions(endpoint, s1, s2, sidecar)

    # ── Known gaps reminder ───────────────────────────────────────────────────
    if not param_filter:
        print(f"\n── Known static analysis gaps ──")
        for i, gap in enumerate(KNOWN_FRONTEND_GAPS, 1):
            print(f"  {i}. {gap}")

    return 0


def _trace_absent_param(name: str, s1: dict | None, s2: dict | None, sidecar: dict) -> None:
    """Explain why a specific param is absent from the final output."""
    name_l = name.lower()
    found_anywhere = False

    if s1:
        s1_all = s1.get("query_params", []) + s1.get("body_params", [])
        if any(p["name"].lower() == name_l for p in s1_all):
            print(f"  Stage 1: ✓ found in API AST")
            found_anywhere = True
        else:
            print(f"  Stage 1: ✗ not in API AST")
            if name_l in PS_NOISE:
                print(f"           ↳ suppressed by PS_NOISE (PS automatic member / LabelValue sub-field)")
            elif name_l in PARAM_NOISE:
                print(f"           ↳ suppressed by PARAM_NOISE (call option, not endpoint param)")

    if s2:
        s2_all = s2.get("query_params", []) + s2.get("body_params", [])
        if any(p["name"].lower() == name_l for p in s2_all):
            print(f"  Stage 2: ✓ found in frontend")
            found_anywhere = True
        else:
            print(f"  Stage 2: ✗ not in frontend")
            if name_l in FRONTEND_NOISE:
                print(f"           ↳ suppressed by FRONTEND_NOISE (UI-only field, never sent to API)")

    removed = {r.lower() for r in sidecar.get("remove_params", [])}
    if name_l in removed:
        print(f"  Sidecar: ↳ explicitly removed by remove_params — this is intentional")
        found_anywhere = True

    if not found_anywhere:
        print(f"\n  '{name}' was not found in any source and is not suppressed by any filter.")
        print(f"  Possible reasons:")
        print(f"    • The param name is different in the source (check case/spelling)")
        print(f"    • It's accessed via a downstream function that Stage 1 can't follow")
        print(f"    • It's set dynamically (JS computed name — see KNOWN_FRONTEND_GAPS)")
        print(f"    • It doesn't exist for this endpoint")
        print(f"\n  To add it explicitly: create/edit sidecars/{s1 and s1.get('endpoint', '?')}.json")


def _trace_suppressions(endpoint: str, s1: dict | None, s2: dict | None, sidecar: dict) -> None:
    """Show which params were actively filtered out at each stage."""
    suppressed: list[str] = []

    if s1:
        # Re-scan blob params for PS_NOISE hits — these were dropped before output
        aliases = s1.get("body_blob_aliases", [])
        file_path = s1.get("file")
        if file_path and aliases:
            ps1_path = API_REPO / file_path
            if ps1_path.exists():
                content = ps1_path.read_text(encoding="utf-8", errors="ignore")
                import re as _re
                for alias in aliases:
                    pat = _re.compile(rf'\${_re.escape(alias)}\.(\w+(?:\.\w+)*)\b', _re.IGNORECASE)
                    for m in pat.finditer(content):
                        top = m.group(1).split(".")[0].lower()
                        if top in PS_NOISE:
                            suppressed.append(f"  Stage 1 PS_NOISE:      ${alias}.{m.group(1)}")

    remove_set = {r.lower() for r in sidecar.get("remove_params", [])}
    for r in sidecar.get("remove_params", []):
        suppressed.append(f"  Sidecar remove_params: {r}")

    if suppressed:
        print(f"\n── Actively suppressed params ──")
        seen = set()
        for s in suppressed:
            if s not in seen:
                seen.add(s)
                print(s)
    else:
        print(f"\n── Actively suppressed params: (none detected) ──")


# ── Stage runner ──────────────────────────────────────────────────────────────

def run_pipeline(
    endpoint_filter: str | None = None,
    only_stage: int | None = None,
    verbose: bool = False,
) -> None:
    width = 60
    label = f"— {endpoint_filter}" if endpoint_filter else "(full corpus)"
    print("=" * width)
    print(f"CIPP OAS Generator {label}")
    print("=" * width)

    if only_stage is None or only_stage == 1:
        print("\n[Stage 1] API Scanner")
        stage1_api_scanner.run(endpoint_filter=endpoint_filter)

    if only_stage is None or only_stage == 2:
        print("\n[Stage 2] Frontend Scanner")
        stage2_frontend_scanner.run(endpoint_filter=endpoint_filter)

    if only_stage is None or only_stage == 3:
        print("\n[Stage 3] Merger + Coverage Classifier")
        stage3_merger.run(endpoint_filter=endpoint_filter)

    if only_stage is None or only_stage == 4:
        print("\n[Stage 4] OAS Emitter")
        stage4_emitter.run(endpoint_filter=endpoint_filter, verbose=verbose)

    print()
    if endpoint_filter:
        _print_endpoint_summary(endpoint_filter)
    else:
        _print_corpus_summary()


# ── Summary printers ──────────────────────────────────────────────────────────

def _print_endpoint_summary(endpoint: str) -> None:
    """Full diagnostic summary for a single endpoint run."""
    suffix = f"-{endpoint}"
    merged_path   = OUT_DIR / f"merged-params{suffix}.json"
    mismatch_path = OUT_DIR / f"mismatch-report{suffix}.json"

    if not merged_path.exists():
        merged_path = OUT_DIR / "merged-params.json"  # fall back to corpus
    if not mismatch_path.exists():
        mismatch_path = OUT_DIR / "mismatch-report.json"

    if not merged_path.exists():
        print("No merged data found.")
        return

    merged = json.loads(merged_path.read_text())
    ep = merged["endpoints"].get(endpoint)
    if not ep:
        print(f"Endpoint '{endpoint}' not found in merged output.")
        return

    q = ep.get("query_params", [])
    b = ep.get("body_params", [])

    print(f"── Summary: {endpoint} ──")
    print(f"  Coverage tier:   {ep.get('coverage_tier', '?')}")
    print(f"  Confidence:      {ep['confidence']}")
    print(f"  Role:            {ep.get('role') or '(none)'}")
    print(f"  HTTP methods:    {ep.get('http_methods', [])}")
    print(f"  Tags:            {ep.get('tags', [])}")
    print(f"  Downstream:      {ep.get('downstream', [])}")
    print(f"  Scheduled fork:  {ep.get('has_scheduled_branch', False)}")
    print(f"  Dynamic options: {ep.get('has_dynamic_options', False)}")
    print(f"  Sidecar applied: {ep.get('sidecar_applied', False)}")
    print(f"  Call sites:      {len(ep.get('call_sites', []))}")

    def _fmt(p: dict) -> str:
        shared = " [$ref]" if p.get("is_shared_ref") else ""
        sources = ",".join(p.get("sources", []))
        return f"    {p['name']} ({p.get('confidence','?')}) [{sources}]{shared}"

    print(f"\n  Query params ({len(q)}):")
    for p in q:
        print(_fmt(p))

    print(f"\n  Body params ({len(b)}):")
    for p in b:
        print(_fmt(p))

    if mismatch_path.exists():
        mismatches = json.loads(mismatch_path.read_text())
        ep_issues = mismatches.get("mismatches", {}).get(endpoint, [])
        real_issues = [m for m in ep_issues if m["issue"] != "sidecar_note"]
        notes       = [m for m in ep_issues if m["issue"] == "sidecar_note"]
        if real_issues:
            print(f"\n  ⚠ Mismatches ({len(real_issues)}):")
            for m in real_issues:
                print(f"    [{m['issue']}] {m['param']}: {m['detail']}")
        else:
            print("\n  ✓ No mismatches")
        for n in notes:
            print(f"\n  📝 {n['detail']}")


def _print_corpus_summary() -> None:
    """High-level summary for a full corpus run."""
    merged_path   = OUT_DIR / "merged-params.json"
    mismatch_path = OUT_DIR / "mismatch-report.json"
    coverage_path = OUT_DIR / "coverage-report.json"
    unified_path  = OUT_DIR / "openapi.json"

    print("── Corpus Summary ──")

    if merged_path.exists():
        merged = json.loads(merged_path.read_text())
        cb = merged.get("confidence_breakdown", {})
        cv = merged.get("coverage_breakdown", {})
        print(f"  Total endpoints:   {merged['total_endpoints']}")
        print(f"  Confidence  high:  {cb.get('high', 0)}")
        print(f"  Confidence  med:   {cb.get('medium', 0)}")
        print(f"  Confidence  low:   {cb.get('low', 0)}")
        print(f"  Coverage FULL:     {cv.get('FULL', 0)}")
        print(f"  Coverage PARTIAL:  {cv.get('PARTIAL', 0)}")
        print(f"  Coverage BLIND:    {cv.get('BLIND', 0)}")
        print(f"  Coverage ORPHAN:   {cv.get('ORPHAN', 0)}")
        print(f"  With sidecars:     {merged.get('with_sidecar_overrides', 0)}")

    if mismatch_path.exists():
        mm = json.loads(mismatch_path.read_text())
        print(f"  Endpoints w/ mismatches: {mm['total_endpoints_with_mismatches']}")

    if coverage_path.exists():
        cov = json.loads(coverage_path.read_text())
        blind = cov.get("blind_endpoints", [])
        orphan = cov.get("orphan_endpoints", [])
        if blind:
            print(f"\n  ⚠ BLIND (need sidecars): {blind[:10]}{'...' if len(blind)>10 else ''}")
        if orphan:
            print(f"  ⚠ ORPHAN (no PS1 found): {orphan[:10]}{'...' if len(orphan)>10 else ''}")

    if unified_path.exists():
        spec = json.loads(unified_path.read_text())
        print(f"\n  OAS paths: {len(spec.get('paths', {}))}")
        print(f"  Output:    {unified_path}")


# ── Sidecar coverage validation ──────────────────────────────────────────────

def validate_sidecar_coverage(merged_params_path: Path | None = None) -> int:
    """
    Validate that all wizard endpoints have sidecars.
    
    This validation runs after Stage 2 has completed and wizard components have been
    detected. Wizard endpoints require manual sidecars because their form fields
    are hidden from static analysis.
    
    Returns 0 if all wizard endpoints have sidecars, 1 if any are missing.
    """
    # Load merged params if available (preferred), otherwise load frontend-calls
    frontend_calls_path = OUT_DIR / "frontend-calls.json"
    merged_path = merged_params_path or (OUT_DIR / "merged-params.json")
    
    # Find existing sidecars
    sidecars_dir = Path(__file__).parent / "sidecars"
    existing_sidecars = {
        f.stem for f in sidecars_dir.glob('*.json')
        if f.name != '_template.json'
    }
    
    # Check wizard endpoints from merged params if available
    wizard_endpoints = {}
    if merged_path.exists():
        merged = json.loads(merged_path.read_text())
        # Look for wizard info in merged params (may have been propagated from stage2)
        for endpoint, data in merged.items():
            if not isinstance(data, dict):
                continue
            # Check if this endpoint has wizard components
            wizard_comps = data.get('wizard_components', [])
            if wizard_comps:
                wizard_endpoints[endpoint] = wizard_comps
    
    # Fall back to frontend-calls if merged params don't have wizard info
    if not wizard_endpoints and frontend_calls_path.exists():
        frontend = json.loads(frontend_calls_path.read_text())
        endpoints = frontend.get('endpoints', {})
        for endpoint, data in endpoints.items():
            wizard_comps = data.get('wizard_components', [])
            if wizard_comps:
                wizard_endpoints[endpoint] = wizard_comps
    
    if not wizard_endpoints:
        print("\n── Sidecar Coverage: No wizard endpoints detected ──")
        return 0
    
    print(f"\n── Sidecar Coverage: Checking {len(wizard_endpoints)} wizard endpoints ──")
    
    missing = []
    for endpoint in sorted(wizard_endpoints.keys()):
        has_sidecar = endpoint in existing_sidecars
        status = '✓' if has_sidecar else '✗'
        wizard_names = ', '.join(wizard_endpoints[endpoint])
        print(f"  {status} {endpoint:30} ({wizard_names})")
        if not has_sidecar:
            missing.append(endpoint)
    
    if missing:
        print(f"\n✗ {len(missing)} wizard endpoints need sidecars:")
        for ep in missing:
            print(f"    {ep}")
        print(f"\nCreate sidecars using: cp sidecars/_template.json sidecars/<EndpointName>.json")
        return 1
    else:
        print("\n✓ All wizard endpoints have sidecars")
        return 0


# ── Validate-only (CI mode) ───────────────────────────────────────────────────

def validate_only(verbose: bool = False) -> int:
    """
    Run full pipeline, validate spec correctness, then diff against committed openapi.json.
    Exits 0 if valid and current, 1 if validation fails or spec is stale.
    CI always runs clean mode (no verbose) so the committed spec stays clean.
    """
    run_pipeline(verbose=verbose)

    generated_path = OUT_DIR / "openapi.json"
    if not generated_path.exists():
        print("ERROR: No generated spec found after pipeline run.")
        return 1

    # Validate OpenAPI spec structure before checking diff
    print("\n── Validating OpenAPI spec ──")
    import subprocess
    validation_script = Path(__file__).parent / "validate_spec.py"
    result = subprocess.run(
        [sys.executable, str(validation_script), "--quiet", str(generated_path)],
        capture_output=True,
        text=True
    )
    
    if result.returncode != 0:
        print("✗ OpenAPI spec validation failed:")
        print(result.stderr, end="")
        print("\nFix validation errors before committing.")
        return 1
    
    print("✓ OpenAPI spec is structurally valid")
    
    # Check sidecar coverage for wizard endpoints
    sidecar_result = validate_sidecar_coverage()
    if sidecar_result != 0:
        print("\nFix missing sidecars before committing.")
        return 1

    if not COMMITTED_SPEC.exists():
        print(f"No committed spec at {COMMITTED_SPEC} — treating as first run.")
        print(f"Commit: cp {generated_path} {COMMITTED_SPEC}")
        return 1

    generated = json.loads(generated_path.read_text())
    committed  = json.loads(COMMITTED_SPEC.read_text())

    gen_paths = generated.get("paths", {})
    com_paths = committed.get("paths", {})

    added   = sorted(set(gen_paths) - set(com_paths))
    removed = sorted(set(com_paths) - set(gen_paths))
    changed = sorted(
        p for p in gen_paths
        if p in com_paths and gen_paths[p] != com_paths[p]
    )

    if not added and not removed and not changed:
        print("\n✓ Spec is current — committed openapi.json matches generated output.")
        return 0

    print("\n❌ Spec is STALE")
    if added:
        print(f"  New endpoints ({len(added)}):     {added[:5]}{'...' if len(added)>5 else ''}")
    if removed:
        print(f"  Removed endpoints ({len(removed)}): {removed[:5]}{'...' if len(removed)>5 else ''}")
    if changed:
        print(f"  Changed endpoints ({len(changed)}): {changed[:5]}{'...' if len(changed)>5 else ''}")
    print(f"\n  Update with: cp {generated_path} {COMMITTED_SPEC}")
    return 1


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="CIPP OAS Generator — Full Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 pipeline.py                                      # full corpus
  python3 pipeline.py --endpoint AddUser                   # single endpoint
  python3 pipeline.py --stage 1                            # API scan only
  python3 pipeline.py --validate-only                      # CI diff check
  python3 pipeline.py --check-patterns                     # post-release health check
  python3 pipeline.py --validate-endpoint AddUser          # full param trace
  python3 pipeline.py --validate-endpoint AddUser --param displayName  # single param trace
        """,
    )
    parser.add_argument("--endpoint",          help="Run for a single endpoint only")
    parser.add_argument("--stage",             type=int, choices=[1, 2, 3, 4],
                        help="Run only a specific stage")
    parser.add_argument("--validate-only",     action="store_true",
                        help="Generate and compare against committed spec (CI mode)")
    parser.add_argument("--check-patterns",    action="store_true",
                        help="Validate generator assumptions against live repos (post-release health check)")
    parser.add_argument("--validate-endpoint", metavar="ENDPOINT",
                        help="Full parameter trace for one endpoint — shows every stage decision and assumption")
    parser.add_argument("--param",             metavar="PARAM",
                        help="Filter --validate-endpoint trace to a single parameter name")
    parser.add_argument("--check-sidecars",    action="store_true",
                        help="Check for wizard endpoints that need sidecars")
    parser.add_argument(
        "--verbose", action="store_true",
        help=(
            "Include pipeline-introspection extensions in emitted OAS "
            "(x-cipp-confidence, x-cipp-coverage-tier, x-cipp-sources, etc.). "
            "Default: clean spec with consumer-useful extensions only."
        ),
    )
    args = parser.parse_args()

    if args.validate_only:
        sys.exit(validate_only(verbose=args.verbose))
    elif args.check_patterns:
        sys.exit(check_patterns())
    elif args.check_sidecars:
        # Full pipeline to stage 2 (to get wizard detection), then validate coverage
        run_pipeline(only_stage=2)
        sys.exit(validate_sidecar_coverage())
    elif args.validate_endpoint:
        sys.exit(validate_endpoint(args.validate_endpoint, param_filter=args.param))
    else:
        run_pipeline(endpoint_filter=args.endpoint, only_stage=args.stage, verbose=args.verbose)
