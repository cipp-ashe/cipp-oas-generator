#!/usr/bin/env python3
"""
Auto-generate sidecars for wizard endpoints.

Extracts form fields from wizard components (CippWizardOffboarding, etc.)
and generates sidecar JSON files with proper parameter definitions.

Usage:
  python3 generate_wizard_sidecars.py              # Generate all missing sidecars
  python3 generate_wizard_sidecars.py --endpoint AddIntuneTemplate  # Generate one sidecar
  python3 generate_wizard_sidecars.py --dry-run    # Preview without writing files
"""

import argparse
import json
import re
from pathlib import Path
from collections import defaultdict

# Wizard component file mappings
WIZARD_COMPONENT_FILES = {
    'CippWizardOffboarding': 'src/components/CippWizard/CippWizardOffboarding.jsx',
    'CippWizardBulkOptions': 'src/components/CippWizard/CippWizardBulkOptions.jsx',
    'CippWizardAutopilotOptions': 'src/components/CippWizard/CippWizardAutopilotOptions.jsx',
    'CippWizardAutopilotImport': 'src/components/CippWizard/CippWizardAutopilotImport.jsx',
    'CippWizardGroupTemplates': 'src/components/CippWizard/CippWizardGroupTemplates.jsx',
    'CippWizardAssignmentFilterTemplates': 'src/components/CippWizard/CippWizardAssignmentFilterTemplates.jsx',
    'CippWizardAppApproval': 'src/components/CippWizard/CippWizardAppApproval.jsx',
    'CippIntunePolicy': 'src/components/CippWizard/CippIntunePolicy.jsx',
    'CippAlertsStep': 'src/components/CippWizard/CippAlertsStep.jsx',
    'CippBaselinesStep': 'src/components/CippWizard/CippBaselinesStep.jsx',
    'CippNotificationsStep': 'src/components/CippWizard/CippNotificationsStep.jsx',
    'CippPSASyncOptions': 'src/components/CippComponents/CippPSASyncOptions.jsx',
}

# Type mapping from CippFormComponent type= to OAS type
TYPE_MAPPING = {
    'switch': 'boolean',
    'checkbox': 'boolean',
    'number': 'integer',
    'textField': 'string',
    'textarea': 'string',
    'password': 'string',
    'select': 'string',
    'radio': 'string',
    'autoComplete': 'object',  # LabelValue
    'datePicker': 'string',
    'dateTimePicker': 'string',
}

def find_frontend_repo():
    """Find the frontend repo path."""
    import os
    frontend_repo = os.environ.get('CIPP_FRONTEND_REPO')
    if frontend_repo and Path(frontend_repo).exists():
        return Path(frontend_repo)
    
    # Try sibling directory
    sibling = Path(__file__).parent / '../cipp-main'
    if sibling.exists():
        return sibling.resolve()
    
    raise RuntimeError("Frontend repo not found. Set CIPP_FRONTEND_REPO env var.")

def extract_form_fields(wizard_file: Path) -> list[dict]:
    """
    Extract form fields from a wizard component JSX file.
    Returns list of parameter definitions.
    """
    content = wizard_file.read_text(encoding='utf-8', errors='ignore')
    fields = []
    seen_names = set()
    
    # Find all <CippFormComponent positions
    component_starts = []
    for m in re.finditer(r'<CippFormComponent\b', content):
        component_starts.append(m.start())
    
    for start_pos in component_starts:
        # Scan forward to find the closing /> or >
        # Handle nested braces properly
        pos = start_pos + len('<CippFormComponent')
        depth = 0
        tag_body = []
        
        while pos < len(content):
            char = content[pos]
            
            if char == '{':
                depth += 1
                tag_body.append(char)
            elif char == '}':
                depth -= 1
                tag_body.append(char)
            elif char == '/' and pos + 1 < len(content) and content[pos + 1] == '>' and depth == 0:
                # Found closing />
                break
            elif char == '>' and depth == 0:
                # Found closing > (rare for self-closing components but handle it)
                break
            else:
                tag_body.append(char)
            
            pos += 1
        
        tag_body_str = ''.join(tag_body)
        
        # Extract name
        name_match = re.search(r'name\s*=\s*["\']([A-Za-z][A-Za-z0-9_.[\]]*)["\']', tag_body_str)
        if not name_match:
            continue
        
        field_name = name_match.group(1)
        
        # Skip if we've seen this field already or if it's a hidden/internal field
        if field_name in seen_names or field_name.startswith('HIDDEN_'):
            continue
        seen_names.add(field_name)
        
        # Extract type
        type_match = re.search(r'type\s*=\s*["\']([A-Za-z][A-Za-z0-9_]*)["\']', tag_body_str)
        field_type = type_match.group(1) if type_match else 'textField'
        
        # Extract label (for description)
        label_match = re.search(r'label\s*=\s*["\']([^"\']+)["\']', tag_body_str)
        label = label_match.group(1) if label_match else field_name
        
        # Check if required
        required_match = re.search(r'required\s*=\s*\{?\s*true\s*\}?', tag_body_str, re.IGNORECASE)
        is_required = bool(required_match)
        
        # Check for multiple (array type)
        multiple_match = re.search(r'multiple\s*=\s*\{?\s*true\s*\}?', tag_body_str, re.IGNORECASE)
        is_multiple = bool(multiple_match)
        
        # Map to OAS type
        oas_type = TYPE_MAPPING.get(field_type, 'string')
        
        param = {
            'name': field_name,
            'in': 'body',
            'required': is_required,
            'type': oas_type,
            'description': label,
        }
        
        # Handle array types
        if is_multiple and oas_type == 'object':
            param['type'] = 'array'
            param['items'] = {'type': 'object'}
        
        # Handle date formats
        if field_type in ('datePicker', 'dateTimePicker'):
            param['format'] = 'date-time'
        
        fields.append(param)
    
    return fields

def load_wizard_endpoint_mapping():
    """
    Load the mapping of endpoints to wizard components from frontend-calls.json.
    Returns dict: {endpoint: [wizard_components]}
    """
    frontend_calls = Path(__file__).parent / 'out/frontend-calls.json'
    if not frontend_calls.exists():
        return {}
    
    data = json.loads(frontend_calls.read_text())
    endpoints = data.get('endpoints', {})
    
    mapping = {}
    for endpoint, ep_data in endpoints.items():
        wizard_comps = ep_data.get('wizard_components', [])
        if wizard_comps:
            mapping[endpoint] = wizard_comps
    
    return mapping

def generate_sidecar(endpoint: str, wizard_components: list[str], frontend_repo: Path, dry_run: bool = False, force: bool = False) -> Path | None:
    """
    Generate a sidecar for an endpoint based on its wizard components.
    Returns the path to the generated sidecar, or None if generation failed.
    """
    sidecars_dir = Path(__file__).parent / 'sidecars'
    sidecar_path = sidecars_dir / f'{endpoint}.json'
    
    # Check if sidecar already exists
    if sidecar_path.exists() and not force:
        print(f"  ⚠️  {endpoint}: Sidecar already exists, skipping")
        return None
    
    # Extract fields from all wizard components
    all_fields = []
    sources = []
    
    for wizard in wizard_components:
        wizard_file_rel = WIZARD_COMPONENT_FILES.get(wizard)
        if not wizard_file_rel:
            print(f"  ⚠️  {endpoint}: Unknown wizard component {wizard}")
            continue
        
        wizard_file = frontend_repo / wizard_file_rel
        if not wizard_file.exists():
            print(f"  ⚠️  {endpoint}: Wizard file not found: {wizard_file_rel}")
            continue
        
        fields = extract_form_fields(wizard_file)
        all_fields.extend(fields)
        sources.append(f"{wizard} ({len(fields)} fields)")
    
    if not all_fields:
        print(f"  ✗  {endpoint}: No fields extracted from {', '.join(wizard_components)}")
        return None
    
    # Deduplicate fields by name (keep first occurrence)
    seen = set()
    unique_fields = []
    for field in all_fields:
        if field['name'] not in seen:
            seen.add(field['name'])
            unique_fields.append(field)
    
    # Create sidecar structure
    sidecar = {
        "_comment": f"Auto-generated from: {', '.join(sources)}",
        "override_method": "POST",
        "parameters": unique_fields,
    }
    
    if dry_run:
        print(f"  ✓  {endpoint}: Would generate sidecar with {len(unique_fields)} parameters")
        print(f"     Sources: {', '.join(sources)}")
        return None
    else:
        sidecar_path.write_text(json.dumps(sidecar, indent=2) + '\n')
        print(f"  ✓  {endpoint}: Generated sidecar with {len(unique_fields)} parameters")
        print(f"     Sources: {', '.join(sources)}")
        return sidecar_path

def main():
    parser = argparse.ArgumentParser(description='Auto-generate wizard endpoint sidecars')
    parser.add_argument('--endpoint', help='Generate sidecar for specific endpoint only')
    parser.add_argument('--dry-run', action='store_true', help='Preview without writing files')
    parser.add_argument('--force', action='store_true', help='Overwrite existing sidecars')
    args = parser.parse_args()
    
    try:
        frontend_repo = find_frontend_repo()
    except RuntimeError as e:
        print(f"Error: {e}")
        return 1
    
    print(f"\n── Auto-generating Wizard Sidecars ──")
    print(f"Frontend repo: {frontend_repo}")
    
    # Load endpoint to wizard mapping
    mapping = load_wizard_endpoint_mapping()
    if not mapping:
        print("\nNo wizard endpoints found. Run Stage 2 first:")
        print("  python3 pipeline.py --stage 2")
        return 1
    
    # Filter to specific endpoint if requested
    if args.endpoint:
        if args.endpoint not in mapping:
            print(f"\nEndpoint '{args.endpoint}' not found in wizard mappings")
            return 1
        mapping = {args.endpoint: mapping[args.endpoint]}
    
    # Check for existing sidecars
    sidecars_dir = Path(__file__).parent / 'sidecars'
    existing = {f.stem for f in sidecars_dir.glob('*.json') if f.name != '_template.json'}
    
    # Generate sidecars
    generated = []
    skipped = []
    failed = []
    
    for endpoint, wizards in sorted(mapping.items()):
        if endpoint in existing and not args.force:
            skipped.append(endpoint)
            continue
        
        result = generate_sidecar(endpoint, wizards, frontend_repo, dry_run=args.dry_run, force=args.force)
        if result:
            generated.append(endpoint)
        else:
            if endpoint not in existing:
                failed.append(endpoint)
    
    # Summary
    print(f"\n── Summary ──")
    if generated:
        print(f"✓ Generated {len(generated)} sidecars")
    if skipped:
        print(f"⚠️  Skipped {len(skipped)} existing sidecars")
    if failed:
        print(f"✗ Failed to generate {len(failed)} sidecars:")
        for ep in failed:
            print(f"    {ep}")
    
    if args.dry_run:
        print("\n(Dry run - no files written)")
    
    return 0 if not failed else 1

if __name__ == '__main__':
    import sys
    sys.exit(main())
