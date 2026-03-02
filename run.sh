#!/usr/bin/env bash
# run.sh — Convenience wrapper for the CIPP OAS Generator pipeline.
# Exports repo paths and delegates all args to pipeline.py.
#
# Usage:
#   ./run.sh                        # full corpus
#   ./run.sh --endpoint AddUser     # single endpoint
#   ./run.sh --stage 1              # stage 1 only
#   ./run.sh --validate-only        # CI diff check
#   ./run.sh --check-patterns                      # post-release health check: are assumptions still valid?
#   ./run.sh --validate-endpoint AddUser           # full parameter trace for one endpoint
#   ./run.sh --validate-endpoint AddUser --param displayName  # trace one specific param

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Repo paths — override these env vars if your layout differs.
export CIPP_API_REPO="${CIPP_API_REPO:-$(cd "$SCRIPT_DIR/../cipp-api-master" 2>/dev/null && pwd || echo "")}"
export CIPP_FRONTEND_REPO="${CIPP_FRONTEND_REPO:-$(cd "$SCRIPT_DIR/../cipp-main" 2>/dev/null && pwd || echo "")}"

if [[ -z "$CIPP_API_REPO" ]]; then
  echo "ERROR: CIPP_API_REPO not set and ../cipp-api-master not found."
  echo "Set CIPP_API_REPO=/path/to/CIPP-API and re-run."
  exit 1
fi

if [[ -z "$CIPP_FRONTEND_REPO" ]]; then
  echo "ERROR: CIPP_FRONTEND_REPO not set and ../cipp-main not found."
  echo "Set CIPP_FRONTEND_REPO=/path/to/CIPP and re-run."
  exit 1
fi

echo "API repo:      $CIPP_API_REPO"
echo "Frontend repo: $CIPP_FRONTEND_REPO"
echo ""

cd "$SCRIPT_DIR"
exec python3 pipeline.py "$@"
