#!/usr/bin/env bash
# run.sh — Convenience wrapper for the CIPP OAS Generator pipeline.
# Exports repo paths and delegates all args to pipeline.py.
#
# Usage:
#   ./run.sh                        # full corpus (uses local repos or fetches remote)
#   ./run.sh --endpoint AddUser     # single endpoint
#   ./run.sh --stage 1              # stage 1 only
#   ./run.sh --validate-only        # CI diff check
#   ./run.sh --check-patterns       # post-release health check
#   ./run.sh --validate-endpoint AddUser
#   ./run.sh --validate-endpoint AddUser --param displayName
#
# Repo resolution order (first match wins):
#   1. CIPP_API_REPO / CIPP_FRONTEND_REPO env vars (explicit local paths)
#   2. Sibling directories ../cipp-api-master and ../cipp-main (cipp-repos/ layout)
#   3. --fetch flag: shallow-clone from GitHub into a temp directory
#
# Remote defaults (used with --fetch or auto-fetch):
#   API:      https://github.com/KelvinTegelaar/CIPP-API  (branch: master)
#   Frontend: https://github.com/KelvinTegelaar/CIPP       (branch: main)
#   Override: CIPP_API_REMOTE / CIPP_API_BRANCH / CIPP_FRONTEND_REMOTE / CIPP_FRONTEND_BRANCH

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Remote defaults ────────────────────────────────────────────────────────────
CIPP_API_REMOTE="${CIPP_API_REMOTE:-https://github.com/KelvinTegelaar/CIPP-API}"
CIPP_API_BRANCH="${CIPP_API_BRANCH:-master}"
CIPP_FRONTEND_REMOTE="${CIPP_FRONTEND_REMOTE:-https://github.com/KelvinTegelaar/CIPP}"
CIPP_FRONTEND_BRANCH="${CIPP_FRONTEND_BRANCH:-main}"

# ── Parse --fetch flag (consumed here, not passed to pipeline.py) ──────────────
FETCH=0
PASSTHROUGH_ARGS=()
for arg in "$@"; do
  if [[ "$arg" == "--fetch" ]]; then
    FETCH=1
  else
    PASSTHROUGH_ARGS+=("$arg")
  fi
done

# ── Resolve repo paths ─────────────────────────────────────────────────────────

# Helper: shallow-clone a remote repo into a temp dir, print the path
fetch_repo() {
  local remote="$1" branch="$2" label="$3"
  local tmp_dir
  tmp_dir="$(mktemp -d "/tmp/cipp-oas-${label}-XXXXXX")"
  echo "Fetching ${label} (${remote}@${branch})..." >&2
  git clone --depth 1 --branch "$branch" --single-branch "$remote" "$tmp_dir" --quiet
  echo "$tmp_dir"
}

if [[ -n "${CIPP_API_REPO:-}" ]]; then
  # Explicit env var — use as-is
  export CIPP_API_REPO
elif [[ -d "$SCRIPT_DIR/../cipp-api-master" ]]; then
  export CIPP_API_REPO="$(cd "$SCRIPT_DIR/../cipp-api-master" && pwd)"
elif [[ "$FETCH" -eq 1 ]]; then
  export CIPP_API_REPO="$(fetch_repo "$CIPP_API_REMOTE" "$CIPP_API_BRANCH" "api")"
  _CLEANUP_API="$CIPP_API_REPO"
else
  echo "ERROR: CIPP_API_REPO not set, ../cipp-api-master not found, and --fetch not specified."
  echo ""
  echo "Options:"
  echo "  1. Set env var:  export CIPP_API_REPO=/path/to/CIPP-API"
  echo "  2. Use remote:   ./run.sh --fetch"
  echo "     (fetches KelvinTegelaar/CIPP-API@${CIPP_API_BRANCH} into a temp dir)"
  exit 1
fi

if [[ -n "${CIPP_FRONTEND_REPO:-}" ]]; then
  export CIPP_FRONTEND_REPO
elif [[ -d "$SCRIPT_DIR/../cipp-main" ]]; then
  export CIPP_FRONTEND_REPO="$(cd "$SCRIPT_DIR/../cipp-main" && pwd)"
elif [[ "$FETCH" -eq 1 ]]; then
  export CIPP_FRONTEND_REPO="$(fetch_repo "$CIPP_FRONTEND_REMOTE" "$CIPP_FRONTEND_BRANCH" "frontend")"
  _CLEANUP_FRONTEND="$CIPP_FRONTEND_REPO"
else
  echo "ERROR: CIPP_FRONTEND_REPO not set, ../cipp-main not found, and --fetch not specified."
  echo ""
  echo "Options:"
  echo "  1. Set env var:  export CIPP_FRONTEND_REPO=/path/to/CIPP"
  echo "  2. Use remote:   ./run.sh --fetch"
  echo "     (fetches KelvinTegelaar/CIPP@${CIPP_FRONTEND_BRANCH} into a temp dir)"
  exit 1
fi

echo "API repo:      $CIPP_API_REPO"
echo "Frontend repo: $CIPP_FRONTEND_REPO"
echo ""

# ── Cleanup trap for temp dirs ─────────────────────────────────────────────────
cleanup() {
  if [[ -n "${_CLEANUP_API:-}" ]]; then
    echo "Cleaning up temp API repo..." >&2
    rm -rf "$_CLEANUP_API"
  fi
  if [[ -n "${_CLEANUP_FRONTEND:-}" ]]; then
    echo "Cleaning up temp frontend repo..." >&2
    rm -rf "$_CLEANUP_FRONTEND"
  fi
}
trap cleanup EXIT

cd "$SCRIPT_DIR"

exec python3 pipeline.py "${PASSTHROUGH_ARGS[@]}"
