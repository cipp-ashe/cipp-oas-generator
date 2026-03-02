#!/usr/bin/env python3
"""
Validate OpenAPI spec(s) for structural correctness.

Usage:
  python3 validate_spec.py                          # validates out/openapi.json
  python3 validate_spec.py out/openapi.json         # explicit path
  python3 validate_spec.py out/domain/*.json        # multiple specs
  python3 validate_spec.py --quiet out/openapi.json # minimal output (CI mode)

Exit codes:
  0 — All specs are valid
  1 — One or more specs failed validation
  2 — File not found or read error
"""

import argparse
import sys
from pathlib import Path


def validate_spec(spec_path: Path, quiet: bool = False) -> bool:
    """
    Validate a single OpenAPI spec file.

    Args:
        spec_path: Path to the OpenAPI JSON file
        quiet: If True, suppress success messages

    Returns:
        True if valid, False if validation failed
    """
    try:
        # Late import so the script can run --help without dependencies
        from openapi_spec_validator import validate
        from openapi_spec_validator.readers import read_from_filename
    except ImportError as e:
        print(f"✗ Missing dependency: {e}", file=sys.stderr)
        print("Install with: pip install -r requirements.txt", file=sys.stderr)
        return False

    if not spec_path.exists():
        print(f"✗ File not found: {spec_path}", file=sys.stderr)
        return False

    try:
        # Read and validate the spec
        spec_dict, spec_url = read_from_filename(str(spec_path))
        validate(spec_dict)
        
        if not quiet:
            print(f"✓ {spec_path.name} is valid OAS 3.1")
        
        return True

    except Exception as e:
        print(f"✗ {spec_path.name} validation failed:", file=sys.stderr)
        print(f"  {e}", file=sys.stderr)
        return False


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Validate OpenAPI 3.1 spec(s) for structural correctness",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                          # validates out/openapi.json
  %(prog)s out/openapi.json         # explicit path
  %(prog)s out/domain/*.json        # multiple specs
  %(prog)s --quiet out/openapi.json # minimal output (CI mode)
        """,
    )
    parser.add_argument(
        "specs",
        nargs="*",
        default=["out/openapi.json"],
        help="Path(s) to OpenAPI spec file(s) (default: out/openapi.json)",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress success messages (only show errors)",
    )

    args = parser.parse_args()

    # Convert string paths to Path objects
    spec_paths = [Path(s) for s in args.specs]

    # Validate all specs
    results = []
    for spec_path in spec_paths:
        result = validate_spec(spec_path, quiet=args.quiet)
        results.append(result)

    # Summary
    total = len(results)
    passed = sum(results)
    failed = total - passed

    if not args.quiet and total > 1:
        print()
        if failed == 0:
            print(f"✓ All {total} specs are valid")
        else:
            print(f"✗ {failed}/{total} spec(s) failed validation")

    # Exit with appropriate code
    if all(results):
        return 0  # All valid
    elif any(r is False for r in results):
        return 1  # Validation failure
    else:
        return 2  # File not found or other error


if __name__ == "__main__":
    sys.exit(main())
