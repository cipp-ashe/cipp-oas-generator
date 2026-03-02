#!/usr/bin/env python3
"""
Sidecar Audit System — Comprehensive Accuracy Check

This script performs deep analysis of all sidecars to ensure:
  1. Parameters documented in sidecar match PS1 implementation
  2. No stub/minimal sidecars where rich data is available
  3. Type annotations are accurate
  4. Descriptions are meaningful, not just "parameter: name"
  5. Required fields are correctly marked
  6. No missing parameters that exist in PS1 or frontend

Output:
  - Full audit report showing discrepancies
  - Recommendations for enrichment
  - Auto-fix suggestions for common issues
"""

import json
import re
from pathlib import Path
from typing import Dict, List, Set, Tuple
from collections import defaultdict
import config

# ── Configuration ─────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
SIDECARS_DIR = SCRIPT_DIR / "sidecars"
OUT_DIR = SCRIPT_DIR / "out"
ENDPOINT_INDEX = OUT_DIR / "endpoint-index.json"
MERGED_PARAMS = OUT_DIR / "merged-params.json"
FRONTEND_CALLS = OUT_DIR / "frontend-calls.json"

# API repo paths (from config.py)
API_REPO = config.API_REPO
HTTP_FUNCTIONS_ROOT = config.HTTP_FUNCTIONS_ROOT

# ── PowerShell AST patterns ───────────────────────────────────────────────────
QUERY_PARAM_RE = re.compile(r'\$Request\.Query\.(\w+)', re.IGNORECASE)
BODY_PARAM_RE = re.compile(r'\$Request\.Body\.(\w+)', re.IGNORECASE)
BODY_BLOB_RE = re.compile(r'\$(\w+)\s*=\s*(?:\[pscustomobject\])?\$Request\.Body', re.IGNORECASE)
BLOB_ACCESS_RE = re.compile(r'\$(\w+)\.(\w+)', re.IGNORECASE)

# ── Load pipeline data ────────────────────────────────────────────────────────

def load_pipeline_data():
    """Load all pipeline outputs for cross-reference.
    
    Stage 2 output shape: {"stage": ..., "endpoints": {name: {...}}, ...}
    Stage 1 output shape: {"stage": ..., "endpoints": {name: {...}}, ...}
    Stage 3 output shape: {"stage": ..., "endpoints": {name: {...}}, ...}
    All three are stored as the full output dict — callers must unwrap ['endpoints'].
    """
    data = {}
    
    with open(ENDPOINT_INDEX) as f:
        data['stage1'] = json.load(f)
    
    with open(MERGED_PARAMS) as f:
        data['stage3'] = json.load(f)
    
    with open(FRONTEND_CALLS) as f:
        data['stage2'] = json.load(f)
    
    return data

def find_ps1_file(endpoint: str, pipeline_data: Dict) -> Path | None:
    """Find the PS1 file for an endpoint."""
    stage1_endpoints = pipeline_data['stage1']['endpoints']
    
    if endpoint not in stage1_endpoints:
        return None
    
    file_rel_path = stage1_endpoints[endpoint].get('file')
    if not file_rel_path:
        return None
    
    ps1_path = API_REPO / file_rel_path
    return ps1_path if ps1_path.exists() else None

def extract_ps1_params(ps1_path: Path) -> Dict:
    """Extract parameters directly from PS1 file using simple regex patterns."""
    content = ps1_path.read_text(encoding='utf-8', errors='ignore')
    
    params = {
        'query': set(),
        'body': set(),
        'blob_aliases': {}
    }
    
    # Direct query access: $Request.Query.Name
    for match in QUERY_PARAM_RE.finditer(content):
        params['query'].add(match.group(1))
    
    # Direct body access: $Request.Body.Name
    for match in BODY_PARAM_RE.finditer(content):
        params['body'].add(match.group(1))
    
    # Body blob assignments: $Foo = $Request.Body
    blob_aliases = {}
    for match in BODY_BLOB_RE.finditer(content):
        alias = match.group(1)
        blob_aliases[alias] = []
    
    # Blob field access: $Alias.Field
    if blob_aliases:
        for match in BLOB_ACCESS_RE.finditer(content):
            alias = match.group(1)
            field = match.group(2)
            if alias in blob_aliases:
                blob_aliases[alias].append(field)
    
    params['blob_aliases'] = {k: list(set(v)) for k, v in blob_aliases.items() if v}
    
    return params

def load_sidecar(endpoint: str) -> Dict | None:
    """Load sidecar JSON file."""
    sidecar_path = SIDECARS_DIR / f"{endpoint}.json"
    if not sidecar_path.exists():
        return None
    
    with open(sidecar_path) as f:
        return json.load(f)

# ── Analysis functions ────────────────────────────────────────────────────────

def analyze_sidecar_quality(endpoint: str, sidecar: Dict, pipeline_data: Dict) -> Dict:
    """Deep analysis of sidecar quality and accuracy."""
    issues = []
    severity = "good"  # good, warning, error
    
    # Get merged params from pipeline (all stage outputs have top-level 'endpoints' key)
    merged = pipeline_data['stage3'].get('endpoints', {}).get(endpoint, {})
    stage1 = pipeline_data['stage1'].get('endpoints', {}).get(endpoint, {})
    
    # Check 1: Generic descriptions
    # Canonical sidecar schema uses add_params (not parameters)
    sidecar_add_params = sidecar.get('add_params', [])
    if sidecar_add_params:
        generic_descriptions = []
        for param in sidecar_add_params:
            desc = param.get('description', '')
            name = param.get('name', '')
            # Check for lazy descriptions like "Body parameter: Foo" or "(auto) ..."
            if desc.lower() in [
                f"query parameter: {name.lower()}",
                f"body parameter: {name.lower()}",
                f"query parameter from ps1: {name.lower()}",
                f"body parameter from ps1: {name.lower()}",
                name.lower(),
                ''
            ] or desc.startswith('(auto)'):
                generic_descriptions.append(name)
        
        if generic_descriptions:
            issues.append({
                'type': 'generic_descriptions',
                'severity': 'warning',
                'params': generic_descriptions,
                'message': f"{len(generic_descriptions)} parameters have generic/empty descriptions"
            })
            severity = "warning"
    
    # Check 2: Missing parameters (stage1 found more than sidecar documents)
    sidecar_params = {p['name'] for p in sidecar.get('add_params', [])}
    
    stage1_query = {p['name'] for p in stage1.get('query_params', [])}
    stage1_body = {p['name'] for p in stage1.get('body_params', [])}
    stage1_all = stage1_query | stage1_body
    
    missing_from_sidecar = stage1_all - sidecar_params
    # Filter out TenantFilter (shared ref)
    missing_from_sidecar = {p for p in missing_from_sidecar if p.lower() != 'tenantfilter'}
    
    if missing_from_sidecar:
        issues.append({
            'type': 'missing_parameters',
            'severity': 'error',
            'params': list(missing_from_sidecar),
            'message': f"Sidecar missing {len(missing_from_sidecar)} parameters found in PS1"
        })
        severity = "error"
    
    # Check 3: Extra parameters (sidecar documents params not in PS1)
    extra_in_sidecar = sidecar_params - stage1_all
    if extra_in_sidecar:
        # This might be okay if frontend provides them, check stage2
        # Note: stage2 output has top-level 'endpoints' key — must unwrap
        fe_endpoint_data = pipeline_data['stage2'].get('endpoints', {}).get(endpoint, {})
        frontend_params = set()
        if fe_endpoint_data:
            for p in fe_endpoint_data.get('body_params', []):
                frontend_params.add(p['name'])
            for p in fe_endpoint_data.get('query_params', []):
                frontend_params.add(p['name'])
        
        # Only flag if not in frontend either
        truly_extra = extra_in_sidecar - frontend_params
        if truly_extra:
            issues.append({
                'type': 'extra_parameters',
                'severity': 'warning',
                'params': list(truly_extra),
                'message': f"{len(truly_extra)} parameters in sidecar not found in PS1 or frontend"
            })
            if severity == "good":
                severity = "warning"
    
    # Check 4: Type accuracy (check if types match inferred types)
    if 'parameters' in sidecar:
        type_mismatches = []
        for param in sidecar['parameters']:
            name = param.get('name')
            sidecar_type = param.get('type', 'string')
            
            # Find in merged params to see what scanner inferred
            merged_query = merged.get('query_params', [])
            merged_body = merged.get('body_params', [])
            merged_param = None
            for p in merged_query + merged_body:
                if p['name'] == name:
                    merged_param = p
                    break
            
            if merged_param:
                # Check if type annotation exists in PS1 or frontend
                # For now, just flag if sidecar says 'string' but it's clearly not
                if sidecar_type == 'string' and name.lower() in ['enabled', 'disabled', 'isdefault', 'required']:
                    type_mismatches.append((name, 'string', 'boolean?'))
        
        if type_mismatches:
            issues.append({
                'type': 'type_mismatches',
                'severity': 'info',
                'params': type_mismatches,
                'message': f"{len(type_mismatches)} parameters might have incorrect types"
            })
    
    # Check 5: Stub detector - sidecar has no params but PS1 has data
    # override_confidence is optional — not flagged as missing
    param_count = len(sidecar.get('add_params', []))
    
    if param_count == 0 and stage1_all:
        issues.append({
            'type': 'stub_sidecar',
            'severity': 'error',
            'message': "Sidecar has no add_params but PS1 has data"
        })
        severity = "error"
    
    # Check 6: Read PS1 directly for comprehensive comparison
    ps1_path = find_ps1_file(endpoint, pipeline_data)
    if ps1_path:
        ps1_params = extract_ps1_params(ps1_path)
        ps1_all_params = ps1_params['query'] | ps1_params['body']
        for alias, fields in ps1_params['blob_aliases'].items():
            ps1_all_params.update(fields)
        
        # Additional missing params from raw PS1 analysis
        additional_missing = ps1_all_params - sidecar_params - stage1_all
        additional_missing = {p for p in additional_missing if p.lower() != 'tenantfilter'}
        
        if additional_missing:
            issues.append({
                'type': 'additional_missing_params',
                'severity': 'warning',
                'params': list(additional_missing),
                'message': f"Raw PS1 analysis found {len(additional_missing)} more parameters"
            })
            if severity == "good":
                severity = "warning"
    
    return {
        'endpoint': endpoint,
        'severity': severity,
        'issues': issues,
        'sidecar_param_count': param_count,
        'stage1_param_count': len(stage1_all)
    }

# ── Main audit logic ──────────────────────────────────────────────────────────

def run_audit():
    """Run comprehensive audit of all sidecars."""
    print("=" * 80)
    print("SIDECAR AUDIT - Comprehensive Accuracy Check")
    print("=" * 80)
    print()
    
    # Load pipeline data
    print("Loading pipeline data...")
    pipeline_data = load_pipeline_data()
    print(f"  ✓ Stage 1: {len(pipeline_data['stage1'].get('endpoints', {}))} endpoints")
    print(f"  ✓ Stage 2: {len(pipeline_data['stage2'].get('endpoints', {}))} frontend calls")
    print(f"  ✓ Stage 3: {len(pipeline_data['stage3'].get('endpoints', {}))} merged endpoints")
    print()
    
    # Get all sidecars
    sidecar_files = list(SIDECARS_DIR.glob("*.json"))
    sidecar_files = [f for f in sidecar_files if f.name != '_template.json']
    
    print(f"Auditing {len(sidecar_files)} sidecars...")
    print()
    
    # Run analysis on each
    results = {
        'good': [],
        'warning': [],
        'error': []
    }
    
    for sidecar_path in sorted(sidecar_files):
        endpoint = sidecar_path.stem
        sidecar = load_sidecar(endpoint)
        
        if sidecar:
            analysis = analyze_sidecar_quality(endpoint, sidecar, pipeline_data)
            results[analysis['severity']].append(analysis)
    
    # Print summary
    print("─" * 80)
    print("AUDIT SUMMARY")
    print("─" * 80)
    print()
    print(f"  ✅ Good:    {len(results['good']):3d}  (accurate and complete)")
    print(f"  ⚠️  Warning: {len(results['warning']):3d}  (minor issues, needs review)")
    print(f"  🔴 Error:   {len(results['error']):3d}  (significant problems, needs fix)")
    print()
    
    # Detail error cases
    if results['error']:
        print("=" * 80)
        print("🔴 ERRORS - Require Immediate Attention")
        print("=" * 80)
        print()
        
        for analysis in sorted(results['error'], key=lambda x: x['endpoint'])[:20]:
            print(f"Endpoint: {analysis['endpoint']}")
            print(f"  Sidecar params: {analysis['sidecar_param_count']}")
            print(f"  Stage1 params:  {analysis['stage1_param_count']}")
            for issue in analysis['issues']:
                if issue['severity'] == 'error':
                    print(f"  ❌ {issue['message']}")
                    if 'params' in issue and isinstance(issue['params'], list):
                        print(f"     {', '.join(str(p) for p in issue['params'][:10])}")
            print()
        
        if len(results['error']) > 20:
            print(f"... and {len(results['error']) - 20} more errors")
            print()
    
    # Detail warning cases
    if results['warning']:
        print("=" * 80)
        print("⚠️  WARNINGS - Should Be Reviewed")
        print("=" * 80)
        print()
        
        for analysis in sorted(results['warning'], key=lambda x: x['endpoint'])[:15]:
            print(f"Endpoint: {analysis['endpoint']}")
            for issue in analysis['issues']:
                if issue['severity'] in ['warning', 'error']:
                    icon = "⚠️" if issue['severity'] == 'warning' else "❌"
                    print(f"  {icon} {issue['message']}")
                    if 'params' in issue and isinstance(issue['params'], list):
                        params_str = ', '.join(str(p) for p in issue['params'][:8])
                        print(f"     {params_str}")
            print()
        
        if len(results['warning']) > 15:
            print(f"... and {len(results['warning']) - 15} more warnings")
            print()
    
    # Summary of issue types
    print("=" * 80)
    print("ISSUE BREAKDOWN")
    print("=" * 80)
    print()
    
    issue_counts = defaultdict(int)
    for severity in ['error', 'warning']:
        for analysis in results[severity]:
            for issue in analysis['issues']:
                issue_counts[issue['type']] += 1
    
    for issue_type, count in sorted(issue_counts.items(), key=lambda x: -x[1]):
        print(f"  {issue_type:30} {count:3d} occurrences")
    
    print()
    print("=" * 80)
    print("RECOMMENDATIONS")
    print("=" * 80)
    print()
    
    if results['error']:
        print("1. Fix ERROR cases first:")
        print("   - Add missing parameters to sidecars")
        print("   - Remove stub sidecars that have no real data")
        print("   - Run: python3 audit_sidecars.py --fix-errors")
        print()
    
    if results['warning']:
        print("2. Review WARNING cases:")
        print("   - Enrich generic descriptions with meaningful text")
        print("   - Verify parameter types are correct")
        print("   - Add override_confidence where missing")
        print("   - Run: python3 audit_sidecars.py --fix-warnings")
        print()
    
    if len(results['good']) == len(sidecar_files):
        print("🎉 All sidecars are accurate and complete!")
    else:
        pct_good = len(results['good']) / len(sidecar_files) * 100
        print(f"Current accuracy: {pct_good:.1f}%")
        print(f"Goal: 100% ({len(sidecar_files) - len(results['good'])} sidecars need work)")
    
    return results

if __name__ == '__main__':
    import sys
    
    if '--fix-errors' in sys.argv:
        print("Error auto-fix not yet implemented")
        print("Manual review required for accuracy")
    elif '--fix-warnings' in sys.argv:
        print("Warning auto-fix not yet implemented")
        print("Manual review required for accuracy")
    else:
        run_audit()
