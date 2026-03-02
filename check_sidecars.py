#!/usr/bin/env python3
"""
Check for endpoints that likely need sidecars but don't have them.

Identifies endpoints with:
1. Wizard pages using external form components (CippWizard* components)
2. CippFormPage with imported child components
3. Endpoints with very few parameters detected but complex frontend

Exits 0 if all are documented, 1 if missing sidecars found.
"""

import json
import sys
from pathlib import Path
from collections import defaultdict

# Wizard components that contain hidden form fields
WIZARD_COMPONENTS = [
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
]

def load_json(path):
    """Load JSON file."""
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def find_wizard_usage(frontend_repo):
    """Find all endpoints that use wizard components."""
    wizard_usage = defaultdict(list)
    
    repo_path = Path(frontend_repo) if frontend_repo else None
    if not repo_path or not repo_path.exists():
        return wizard_usage
    
    src_dir = repo_path / 'src'
    if not src_dir.exists():
        return wizard_usage
    
    # Search for wizard component imports and usage
    import re
    for jsx_file in list(src_dir.rglob('*.jsx')) + list(src_dir.rglob('*.js')):
        try:
            content = jsx_file.read_text(encoding='utf-8', errors='ignore')
            
            # Check if file uses any wizard components
            for wizard in WIZARD_COMPONENTS:
                if wizard in content:
                    # Try to extract the postUrl - use simpler pattern
                    url_match = re.search(r'postUrl=["\']([^"\']+)', content)
                    if url_match:
                        endpoint_name = url_match.group(1).split('/')[-1]
                        wizard_usage[endpoint_name].append({
                            'file': str(jsx_file.relative_to(repo_path)),
                            'wizard': wizard
                        })
                        break  # Only count once per file
        except Exception as e:
            pass
    
    return wizard_usage

def main():
    """Main entry point."""
    # Load analyzer outputs
    frontend_calls = load_json('out/frontend-calls.json')
    merged_params = load_json('out/merged-params.json')
    
    # Find existing sidecars
    sidecars_dir = Path('sidecars')
    existing_sidecars = {f.stem for f in sidecars_dir.glob('*.json') if f.name != '_template.json'}
    
    # Get FRONTEND_REPO from env, or try sibling directory
    import os
    frontend_repo = os.environ.get('CIPP_FRONTEND_REPO')
    if not frontend_repo:
        sibling = Path(__file__).parent / '../cipp-main'
        if sibling.exists():
            frontend_repo = str(sibling.resolve())
    
    wizard_usage = find_wizard_usage(frontend_repo)
    
    print('\n── Sidecar Coverage Check ──\n')
    
    # Check 1: Endpoints using wizard components
    print('1. Wizard-based endpoints:')
    wizard_endpoints = []
    for endpoint, usages in sorted(wizard_usage.items()):
        has_sidecar = endpoint in existing_sidecars
        status = '✓' if has_sidecar else '✗'
        wizard_names = ', '.join(set(u['wizard'] for u in usages))
        print(f'  {status} {endpoint:30} ({wizard_names})')
        if not has_sidecar:
            wizard_endpoints.append(endpoint)
    
    if not wizard_usage:
        print('  (No wizard usage detected - CIPP_FRONTEND_REPO may not be set)')
    
    # Check 2: Endpoints with suspiciously few parameters
    print('\n2. Endpoints with < 5 parameters (may need review):')
    sparse_endpoints = []
    if merged_params and isinstance(merged_params, dict):
        for endpoint, data in sorted(merged_params.items()):
            # Skip metadata keys
            if not isinstance(data, dict) or endpoint in ['total_endpoints', 'confidence_breakdown', 
                                                           'coverage_breakdown', 'with_sidecar_overrides']:
                continue
            
            params = data.get('parameters', [])
            param_count = len([p for p in params if p.get('name') not in ['tenantFilter']])
            
            # Skip if already has sidecar or very clearly GET endpoint
            if endpoint in existing_sidecars:
                continue
            
            methods = data.get('methods', [])
            if 'POST' in methods and param_count < 5:
                has_sidecar = endpoint in existing_sidecars
                status = '✓' if has_sidecar else '⚠'
                print(f'  {status} {endpoint:30} {param_count} params')
                if not has_sidecar:
                    sparse_endpoints.append(endpoint)
    
    # Summary
    print('\n── Summary ──')
    missing_wizard = len(wizard_endpoints)
    needs_review = len(sparse_endpoints)
    
    if missing_wizard > 0:
        print(f'✗ {missing_wizard} wizard endpoints need sidecars:')
        for ep in wizard_endpoints:
            print(f'    {ep}')
    else:
        print('✓ All wizard endpoints have sidecars')
    
    if needs_review > 0:
        print(f'\n⚠ {needs_review} POST endpoints with <5 params (review needed)')
    
    # Exit code
    if missing_wizard > 0:
        print('\nCreate sidecars with: cp sidecars/_template.json sidecars/<EndpointName>.json')
        return 1
    
    return 0

if __name__ == '__main__':
    sys.exit(main())
