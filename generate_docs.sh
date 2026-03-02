#!/usr/bin/env bash
# generate_docs.sh — Generate static HTML documentation from OpenAPI spec using Redocly.
#
# Usage:
#   ./generate_docs.sh                      # generates from out/openapi.json
#   ./generate_docs.sh out/openapi.json     # explicit path
#
# Requirements:
#   Node.js and npx (npx is bundled with Node.js 5.2+)
#
# Output:
#   out/docs/index.html — Static HTML documentation

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Default to unified spec
SPEC_PATH="${1:-out/openapi.json}"
OUTPUT_DIR="out/docs"
OUTPUT_FILE="$OUTPUT_DIR/index.html"

# Check if spec exists
if [[ ! -f "$SPEC_PATH" ]]; then
  echo "✗ Spec file not found: $SPEC_PATH" >&2
  echo "  Run './run.sh' first to generate the spec." >&2
  exit 2
fi

# Check for Node.js/npx
if ! command -v npx &> /dev/null; then
  echo "✗ npx not found — Node.js is required for documentation generation" >&2
  echo "" >&2
  echo "  Install Node.js:" >&2
  echo "    • macOS:   brew install node" >&2
  echo "    • Ubuntu:  sudo apt install nodejs npm" >&2
  echo "    • Fedora:  sudo dnf install nodejs" >&2
  echo "" >&2
  echo "  Or skip docs generation — it's optional." >&2
  exit 1
fi

echo "Generating HTML documentation from $SPEC_PATH..."

# Create output directory
mkdir -p "$OUTPUT_DIR"

# Generate HTML docs with Redocly
# --output flag creates a standalone HTML file (no dependencies)
npx --yes @redocly/cli build-docs "$SPEC_PATH" \
  --output "$OUTPUT_FILE" \
  --title "CIPP API Documentation"

if [[ -f "$OUTPUT_FILE" ]]; then
  echo "✓ Documentation generated successfully"
  echo ""
  echo "  → file://$(cd "$(dirname "$OUTPUT_FILE")" && pwd)/$(basename "$OUTPUT_FILE")"
  echo ""
  echo "  Open with:"
  echo "    • macOS:   open $OUTPUT_FILE"
  echo "    • Linux:   xdg-open $OUTPUT_FILE"
  echo "    • Windows: start $OUTPUT_FILE"
else
  echo "✗ Documentation generation failed" >&2
  exit 1
fi
