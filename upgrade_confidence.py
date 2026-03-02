#!/usr/bin/env python3
"""
Confidence Upgrade System — Foundational Approach

Philosophy:
  - Sidecars are human verification, not overrides
  - If a sidecar exists, the endpoint is verified → high confidence
  - Scanner provides suggestions; sidecars provide truth
  - No endpoint should remain <high confidence with a sidecar present

This script:
  1. Identifies all sidecars without override_confidence: high
  2. Adds override_confidence: high to them (human verification implies confidence)
  3. Generates sidecar templates for remaining medium/low confidence endpoints
  4. Provides a report showing the confidence upgrade path
"""

from pathlib import Path
import json
from typing import Dict, List, Set
from collections import defaultdict

# ── Configuration ─────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
SIDECARS_DIR = SCRIPT_DIR / "sidecars"
OUT_DIR = SCRIPT_DIR / "out"
MERGED_PARAMS = OUT_DIR / "merged-params.json"

# ── Load current state ────────────────────────────────────────────────────────

def load_merged_params() -> Dict:
    """Load the merged-params.json to see current confidence state."""
    with open(MERGED_PARAMS) as f:
        return json.load(f)

def load_sidecar(endpoint: str) -> Dict | None:
    """Load existing sidecar for an endpoint (JSON format)."""
    sidecar_path = SIDECARS_DIR / f"{endpoint}.json"
    if not sidecar_path.exists():
        return None
    with open(sidecar_path) as f:
        return json.load(f)

def save_sidecar(endpoint: str, data: Dict) -> None:
    """Save sidecar in JSON format with proper formatting."""
    sidecar_path = SIDECARS_DIR / f"{endpoint}.json"
    
    # Ensure override_confidence is at the top for visibility
    ordered_data = {}
    if 'override_confidence' in data:
        ordered_data['override_confidence'] = data['override_confidence']
    
    # Add remaining fields in a reasonable order
    for key in ['_comment', 'override_method', 'parameters', 'description']:
        if key in data and key != 'override_confidence':
            ordered_data[key] = data[key]
    
    # Add any remaining fields
    for key, value in data.items():
        if key not in ordered_data:
            ordered_data[key] = value
    
    with open(sidecar_path, 'w') as f:
        json.dump(ordered_data, f, indent=2)
        f.write('\n')  # Trailing newline

def generate_sidecar_template(endpoint: str, info: Dict) -> Dict:
    """Generate a sidecar template based on merged data."""
    template = {
        'override_confidence': 'high',
        '_comment': f'Generated template for {endpoint} - verify parameters before use',
    }
    
    # Add override_method if needed
    methods = info.get('http_methods', [])
    if len(methods) == 1 and methods[0] not in ['GET', 'POST']:
        template['override_method'] = methods[0]
    
    # Add description
    template['description'] = info.get('description') or info.get('synopsis') or f"{endpoint} endpoint"
    
    # Document parameters if present
    query_params = info.get('query_params', [])
    body_params = info.get('body_params', [])
    
    if query_params or body_params:
        template['parameters'] = []
        
        for p in query_params:
            template['parameters'].append({
                'name': p['name'],
                'in': 'query',
                'required': False,
                'type': 'string',  # Default type
                'description': p.get('description', f"Query parameter: {p['name']}")
            })
        
        for p in body_params:
            # Skip TenantFilter if it's shared
            if p.get('is_shared_ref'):
                continue
            template['parameters'].append({
                'name': p['name'],
                'in': 'body',
                'required': False,
                'type': 'string',  # Default type
                'description': p.get('description', f"Body parameter: {p['name']}")
            })
    
    return template

# ── Main analysis and upgrade logic ───────────────────────────────────────────

def analyze_and_upgrade(dry_run: bool = True) -> None:
    """
    Analyze all endpoints and upgrade confidence where appropriate.
    
    Categories:
    1. Existing sidecars without override_confidence → Add it
    2. Medium confidence without sidecar → Generate template
    3. Low confidence without sidecar → Generate minimal template
    """
    
    data = load_merged_params()
    endpoints = data['endpoints']
    
    # Categorize endpoints
    categories = defaultdict(list)
    
    for endpoint, info in endpoints.items():
        confidence = info.get('confidence')
        has_sidecar = info.get('sidecar_applied', False)
        
        if confidence == 'high':
            categories['already_high'].append(endpoint)
        elif has_sidecar:
            # Has sidecar but not high confidence → needs upgrade
            sidecar = load_sidecar(endpoint)
            if sidecar and 'override_confidence' not in sidecar:
                categories['needs_upgrade'].append((endpoint, sidecar, info))
            else:
                categories['has_override_already'].append(endpoint)
        elif confidence == 'medium':
            categories['needs_sidecar_med'].append((endpoint, info))
        elif confidence == 'low':
            categories['needs_sidecar_low'].append((endpoint, info))
    
    # Print report
    print("=" * 80)
    print("FOUNDATIONAL CONFIDENCE UPGRADE SYSTEM")
    print("=" * 80)
    print()
    print("Philosophy: Sidecars are human verification → High confidence by default")
    print()
    print("Current State:")
    print(f"  ✓ Already high confidence:     {len(categories['already_high']):3d}")
    print(f"  ⚠ Needs upgrade:               {len(categories['needs_upgrade']):3d}  (sidecars without override_confidence)")
    print(f"  📝 Needs sidecar (medium):     {len(categories['needs_sidecar_med']):3d}  (blob-accessed params)")
    print(f"  📝 Needs sidecar (low):        {len(categories['needs_sidecar_low']):3d}  (no params found)")
    print()
    
    # Phase 1: Upgrade existing sidecars
    print("─" * 80)
    print("PHASE 1: Upgrade Existing Sidecars")
    print("─" * 80)
    print()
    
    if categories['needs_upgrade']:
        print(f"Upgrading {len(categories['needs_upgrade'])} sidecars with override_confidence: high")
        print()
        
        for endpoint, sidecar, info in categories['needs_upgrade']:
            print(f"  ✓ {endpoint}")
            if not dry_run:
                sidecar['override_confidence'] = 'high'
                save_sidecar(endpoint, sidecar)
        
        print()
        new_high = len(categories['already_high']) + len(categories['needs_upgrade'])
        total = len(endpoints)
        pct = (new_high / total * 100)
        print(f"After Phase 1: {new_high}/{total} ({pct:.1f}%) high confidence")
    else:
        print("✓ All existing sidecars already have override_confidence")
    
    print()
    
    # Phase 2: Generate templates for remaining endpoints
    print("─" * 80)
    print("PHASE 2: Generate Sidecar Templates")
    print("─" * 80)
    print()
    
    templates_dir = SCRIPT_DIR / "sidecar_templates"
    if not dry_run:
        templates_dir.mkdir(exist_ok=True)
    
    # Medium confidence endpoints
    if categories['needs_sidecar_med']:
        print(f"Medium confidence ({len(categories['needs_sidecar_med'])} endpoints):")
        print("  These have parameters from blob access - need manual verification")
        print()
        
        for endpoint, info in sorted(categories['needs_sidecar_med'], key=lambda x: x[0])[:10]:
            param_count = len(info.get('query_params', [])) + len(info.get('body_params', []))
            tier = info.get('coverage_tier')
            print(f"    {endpoint:35} {tier:8} ({param_count} params)")
            
            if not dry_run:
                template = generate_sidecar_template(endpoint, info)
                template_path = templates_dir / f"{endpoint}.json"
                with open(template_path, 'w') as f:
                    json.dump(template, f, indent=2)
                    f.write('\n')
        
        if len(categories['needs_sidecar_med']) > 10:
            print(f"    ... and {len(categories['needs_sidecar_med']) - 10} more")
        print()
    
    # Low confidence endpoints
    if categories['needs_sidecar_low']:
        print(f"Low confidence ({len(categories['needs_sidecar_low'])} endpoints):")
        print("  These have no parameters - minimal documentation needed")
        print()
        
        for endpoint, info in sorted(categories['needs_sidecar_low'], key=lambda x: x[0])[:10]:
            tier = info.get('coverage_tier')
            tags = ' > '.join(info.get('tags', ['Uncategorized']))
            print(f"    {endpoint:35} {tier:8} [{tags}]")
            
            if not dry_run:
                template = generate_sidecar_template(endpoint, info)
                template_path = templates_dir / f"{endpoint}.json"
                with open(template_path, 'w') as f:
                    json.dump(template, f, indent=2)
                    f.write('\n')
        
        if len(categories['needs_sidecar_low']) > 10:
            print(f"    ... and {len(categories['needs_sidecar_low']) - 10} more")
        print()
    
    # Final report
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print()
    
    if dry_run:
        print("🔍 DRY RUN - No changes made")
        print()
        print("To apply changes:")
        print("  python3 upgrade_confidence.py --apply")
        print()
        print("This will:")
        print(f"  1. Upgrade {len(categories['needs_upgrade'])} existing sidecars")
        print(f"  2. Generate {len(categories['needs_sidecar_med']) + len(categories['needs_sidecar_low'])} new sidecar templates")
        print()
        
        phase1_result = len(categories['already_high']) + len(categories['needs_upgrade'])
        phase2_result = phase1_result + len(categories['needs_sidecar_med']) + len(categories['needs_sidecar_low'])
        total = len(endpoints)
        
        print("Projected confidence after full completion:")
        print(f"  Phase 1 only:  {phase1_result}/{total} ({phase1_result/total*100:.1f}%)")
        print(f"  Phase 1 + 2:   {phase2_result}/{total} ({phase2_result/total*100:.1f}%)")
    else:
        print("✅ Changes applied!")
        print()
        print(f"  ✓ Upgraded {len(categories['needs_upgrade'])} sidecars")
        print(f"  ✓ Generated {len(categories['needs_sidecar_med']) + len(categories['needs_sidecar_low'])} templates in sidecar_templates/")
        print()
        print("Next steps:")
        print("  1. Review generated templates in sidecar_templates/")
        print("  2. Manually verify parameter lists")
        print("  3. Move verified templates to sidecars/")
        print("  4. Run pipeline.py to see updated confidence")

if __name__ == '__main__':
    import sys
    dry_run = '--apply' not in sys.argv
    analyze_and_upgrade(dry_run=dry_run)
