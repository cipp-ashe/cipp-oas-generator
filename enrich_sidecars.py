#!/usr/bin/env python3
"""
Sidecar Enrichment Tool - Intelligent Parameter Merging

This script fixes sidecar accuracy issues by:
  1. Reading PS1 files to extract ALL parameters
  2. Merging PS1 params into existing sidecars (preserving frontend/wizard fields)
  3. Adding override_confidence: high where missing
  4. Enriching generic descriptions with PS1 comments/context

Strategy:
  - PRESERVE all existing sidecar parameters (they may be from wizards)
  - ADD missing PS1 parameters that aren't documented
  - ENRICH descriptions where they're generic
  - ADD override_confidence if missing
"""

import json
import re
from pathlib import Path
from typing import Dict, List, Set
from datetime import datetime
import config

# ── Configuration ─────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
SIDECARS_DIR = SCRIPT_DIR / "sidecars"
OUT_DIR = SCRIPT_DIR / "out"
ENDPOINT_INDEX = OUT_DIR / "endpoint-index.json"

API_REPO = config.API_REPO

# ── PowerShell parameter extraction ───────────────────────────────────────────
QUERY_PARAM_RE = re.compile(r'\$Request\.Query\.(\w+)', re.IGNORECASE)
BODY_PARAM_RE = re.compile(r'\$Request\.Body\.(\w+)', re.IGNORECASE)
BODY_BLOB_RE = re.compile(r'\$(\w+)\s*=\s*(?:\[pscustomobject\])?\$Request\.Body', re.IGNORECASE)
BLOB_ACCESS_RE = re.compile(r'\$(\w+)\.(\w+)', re.IGNORECASE)
# Enhanced: nested property access like $UserObj.Scheduled.Enabled or $Body.groupId.addedFields.groupName
NESTED_PROP_RE = re.compile(r'\$(\w+)\.([\w.]+)', re.IGNORECASE)
PARAM_COMMENT_RE = re.compile(r'#\s*(\w+)\s*[-:]\s*(.+)', re.IGNORECASE)

def load_endpoint_index():
    """Load stage1 endpoint index."""
    with open(ENDPOINT_INDEX) as f:
        return json.load(f)

def find_ps1_file(endpoint: str, index: Dict) -> Path | None:
    """Find PS1 file for endpoint."""
    endpoints = index['endpoints']
    if endpoint not in endpoints:
        return None
    
    file_rel = endpoints[endpoint].get('file')
    if not file_rel:
        return None
    
    ps1_path = API_REPO / file_rel
    return ps1_path if ps1_path.exists() else None

def extract_ps1_params_rich(ps1_path: Path) -> Dict:
    """
    Extract parameters from PS1 with rich metadata.
    
    Returns dict with:
      - query: list of {name, description}
      - body: list of {name, description}
      - function_description: string from .SYNOPSIS or .DESCRIPTION
    """
    content = ps1_path.read_text(encoding='utf-8', errors='ignore')
    
    result = {
        'query': [],
        'body': [],
        'function_description': None
    }
    
    # Extract function description from help block
    synopsis_match = re.search(r'\.SYNOPSIS\s+(.+?)(?:\n\s*\.|\n\s*#>)', content, re.DOTALL | re.IGNORECASE)
    if synopsis_match:
        result['function_description'] = synopsis_match.group(1).strip()
    else:
        desc_match = re.search(r'\.DESCRIPTION\s+(.+?)(?:\n\s*\.|\n\s*#>)', content, re.DOTALL | re.IGNORECASE)
        if desc_match:
            result['function_description'] = desc_match.group(1).strip()
    
    # Extract query parameters
    query_params = {}
    for match in QUERY_PARAM_RE.finditer(content):
        param_name = match.group(1)
        query_params[param_name] = None
    
    # Extract body parameters (direct access)
    body_params = {}
    for match in BODY_PARAM_RE.finditer(content):
        param_name = match.group(1)
        body_params[param_name] = None
    
    # Extract blob-accessed parameters (including nested)
    blob_aliases = {}
    for match in BODY_BLOB_RE.finditer(content):
        alias = match.group(1)
        blob_aliases[alias] = set()
    
    if blob_aliases:
        # Use NESTED_PROP_RE to capture full property paths like Scheduled.Enabled
        for match in NESTED_PROP_RE.finditer(content):
            alias = match.group(1)
            property_path = match.group(2)
            
            if alias in blob_aliases:
                # Add the full property path as well as the root property
                blob_aliases[alias].add(property_path)
                
                # Also add just the first-level property if it's a nested path
                if '.' in property_path:
                    root_prop = property_path.split('.')[0]
                    blob_aliases[alias].add(root_prop)
        
        # Add all blob-accessed params to body_params
        for alias, fields in blob_aliases.items():
            for field in fields:
                if field not in body_params and field.lower() != 'tenantfilter':
                    body_params[field] = None
    
    # Try to find comments near parameters
    lines = content.split('\n')
    for i, line in enumerate(lines):
        comment_match = PARAM_COMMENT_RE.search(line)
        if comment_match:
            param_name = comment_match.group(1)
            param_desc = comment_match.group(2).strip()
            
            if param_name in query_params and not query_params[param_name]:
                query_params[param_name] = param_desc
            elif param_name in body_params and not body_params[param_name]:
                body_params[param_name] = param_desc
    
    # Convert to list format
    for name, desc in query_params.items():
        result['query'].append({
            'name': name,
            'description': desc or f"Query parameter from PS1: {name}"
        })
    
    for name, desc in body_params.items():
        # Skip TenantFilter (shared ref) - except for endpoints that ONLY have TenantFilter
        if name.lower() == 'tenantfilter' and len(body_params) > 1:
            continue
        result['body'].append({
            'name': name,
            'description': desc or f"Body parameter from PS1: {name}"
        })
    
    return result

def load_sidecar(endpoint: str) -> Dict | None:
    """Load existing sidecar."""
    sidecar_path = SIDECARS_DIR / f"{endpoint}.json"
    if not sidecar_path.exists():
        return None
    with open(sidecar_path) as f:
        return json.load(f)

def save_sidecar(endpoint: str, sidecar: Dict) -> None:
    """Save sidecar with proper formatting.
    
    Key ordering matches canonical sidecar schema:
      override_* fields first (intent/metadata), then add_params/remove_params
      (content), then trust/notes (provenance).
    'parameters' is NOT a valid sidecar key — only 'add_params' is used.
    """
    sidecar_path = SIDECARS_DIR / f"{endpoint}.json"
    
    # Canonical field ordering for readability
    ordered = {}
    for key in [
        'override_confidence', 'override_synopsis', 'override_description',
        'override_methods', 'override_role',
        '_comment', 'notes',
        'add_params', 'remove_params', 'add_responses',
        'raw_request_body', 'raw_response_body',
        'trust_level', 'tested_version',
        'deprecated', 'x_cipp_warnings',
    ]:
        if key in sidecar:
            ordered[key] = sidecar[key]
    
    # Add any remaining fields not in the canonical list
    for key, value in sidecar.items():
        if key not in ordered:
            ordered[key] = value
    
    with open(sidecar_path, 'w') as f:
        json.dump(ordered, f, indent=2)
        f.write('\n')

def merge_params(existing_params: List[Dict], ps1_params: List[Dict], param_in: str) -> List[Dict]:
    """
    Merge PS1 parameters into existing sidecar parameters.
    
    Strategy:
      - Keep ALL existing params (may be from frontend/wizards)
      - Add PS1 params that aren't already documented
      - Don't remove anything (preserves wizard fields)
    """
    existing_names = {p['name'] for p in existing_params}
    merged = list(existing_params)  # Start with all existing
    
    # Add PS1 params that are missing
    for ps1_param in ps1_params:
        if ps1_param['name'] not in existing_names:
            merged.append({
                'name': ps1_param['name'],
                'in': param_in,
                'required': False,
                'type': 'string',  # Default, can be refined later
                'description': ps1_param['description']
            })
    
    return merged

def enrich_sidecar(endpoint: str, sidecar: Dict, ps1_data: Dict, ps1_exists: bool) -> Dict:
    """
    Enrich sidecar with PS1 data.
    
    Adds:
      - Missing parameters from PS1
      - override_confidence if missing
      - Better description if current one is generic
      - Removes incorrect 'deprecated' flag if PS1 file exists
    """
    enriched = dict(sidecar)
    
    # If sidecar is marked deprecated but PS1 file exists, remove deprecation
    if ps1_exists and enriched.get('deprecated'):
        print(f"    WARNING: {endpoint} marked deprecated but PS1 exists - removing deprecation flag")
        del enriched['deprecated']
        if 'notes' in enriched and 'no PS1 exists' in enriched['notes']:
            del enriched['notes']
    
    # Add override_confidence if missing
    if 'override_confidence' not in enriched:
        enriched['override_confidence'] = 'high'
    
    # Enrich description if generic/missing
    current_desc = enriched.get('description', '')
    if not current_desc or current_desc.lower() == f"{endpoint.lower()} endpoint":
        if ps1_data.get('function_description'):
            enriched['description'] = ps1_data['function_description'][:200]  # Truncate if too long
    
    # Canonical sidecar schema uses 'add_params' exclusively.
    # All three branches write to 'add_params' — never 'parameters'.
    existing_add_params = enriched.get('add_params', [])
    existing_query = [p for p in existing_add_params if p.get('in') == 'query']
    existing_body  = [p for p in existing_add_params if p.get('in') == 'body']

    merged_query = merge_params(existing_query, ps1_data['query'], 'query')
    merged_body  = merge_params(existing_body,  ps1_data['body'],  'body')

    if merged_query or merged_body:
        enriched['add_params'] = merged_query + merged_body
    elif not existing_add_params and (ps1_data['query'] or ps1_data['body']):
        # No existing params and PS1 has data — build from scratch
        all_params = []
        for q in ps1_data['query']:
            all_params.append({
                'name': q['name'],
                'in': 'query',
                'required': False,
                'type': 'string',
                'description': q['description']
            })
        for b in ps1_data['body']:
            all_params.append({
                'name': b['name'],
                'in': 'body',
                'required': False,
                'type': 'string',
                'description': b['description']
            })
        enriched['add_params'] = all_params
    
    # Add enrichment comment
    if '_comment' not in enriched:
        enriched['_comment'] = f"Enriched with PS1 data on {datetime.now().strftime('%Y-%m-%d')}"
    else:
        enriched['_comment'] = f"{enriched['_comment']} | Enriched {datetime.now().strftime('%Y-%m-%d')}"
    
    return enriched

def run_enrichment(dry_run: bool = True):
    """Run enrichment process on all sidecars."""
    print("=" * 80)
    print("SIDECAR ENRICHMENT - Intelligent Parameter Merging")
    print("=" * 80)
    print()
    
    # Load endpoint index
    index = load_endpoint_index()
    print(f"Loaded {len(index['endpoints'])} endpoints from stage1")
    print()
    
    # Get all sidecars
    sidecars = list(SIDECARS_DIR.glob("*.json"))
    sidecars = [s for s in sidecars if s.name != '_template.json']
    
    print(f"Processing {len(sidecars)} sidecars...")
    print()
    
    stats = {
        'processed': 0,
        'enriched': 0,
        'params_added': 0,
        'no_ps1': 0,
        'no_changes': 0
    }
    
    for sidecar_path in sorted(sidecars):
        endpoint = sidecar_path.stem
        sidecar = load_sidecar(endpoint)
        
        if not sidecar:
            continue
        
        # Find PS1 file
        ps1_path = find_ps1_file(endpoint, index)
        if not ps1_path:
            stats['no_ps1'] += 1
            continue
        
        # Extract PS1 parameters
        ps1_data = extract_ps1_params_rich(ps1_path)
        
        # If sidecar is marked deprecated but PS1 file exists, always enrich (removes deprecation)
        needs_enrichment = False
        if sidecar.get('deprecated'):
            needs_enrichment = True
        
        # Check if enrichment needed based on params
        ps1_param_count = len(ps1_data['query']) + len(ps1_data['body'])
        if ps1_param_count == 0 and 'override_confidence' in sidecar and not needs_enrichment:
            stats['no_changes'] += 1
            continue
        
        # Enrich sidecar
        enriched = enrich_sidecar(endpoint, sidecar, ps1_data, ps1_exists=True)
        
        # Count changes — canonical schema uses add_params only
        original_param_count = len(sidecar.get('add_params', []))
        new_param_count = len(enriched.get('add_params', []))
        params_added = new_param_count - original_param_count
        
        if params_added > 0 or 'override_confidence' not in sidecar or sidecar.get('deprecated'):
            stats['enriched'] += 1
            stats['params_added'] += params_added
            print(f"  ✓ {endpoint:40} +{params_added} params")
            
            if not dry_run:
                save_sidecar(endpoint, enriched)
        else:
            stats['no_changes'] += 1
        
        stats['processed'] += 1
    
    print()
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print()
    print(f"  Processed:          {stats['processed']}")
    print(f"  Enriched:           {stats['enriched']}")
    print(f"  Parameters added:   {stats['params_added']}")
    print(f"  No PS1 file:        {stats['no_ps1']}")
    print(f"  No changes needed:  {stats['no_changes']}")
    print()
    
    if dry_run:
        print("🔍 DRY RUN - No changes made")
        print()
        print("To apply changes:")
        print("  python3 enrich_sidecars.py --apply")
        print()
        print("DRY RUN - No changes made")
        print()
        print("To apply changes:")
        print("  python3 enrich_sidecars.py --apply")
    else:
        print("Changes applied!")
        print()
        print("Next steps:")
        print("  1. Run: python3 audit_sidecars.py")
        print("  2. Run: python3 pipeline.py")
        print("  3. Review: git diff sidecars/")

if __name__ == '__main__':
    import sys
    dry_run = '--apply' not in sys.argv
    run_enrichment(dry_run=dry_run)
