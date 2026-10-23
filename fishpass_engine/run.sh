#!/usr/bin/env bash
set -euo pipefail

if [ -z "${1:-}" ]; then
  echo "Usage: $0 <plan_code> [--export-to-azure | --export-to-local --local-export-directory <dir>] [--export-type versioned|unversioned [--export-version <version>]]" >&2
  exit 1
fi
PLAN_CODE="$1"
shift

export FISHPASS_HOST="localhost"
export FISHPASS_PORT="5432"
export FISHPASS_DBNAME="fishpass"
export FISHPASS_USER="fishpass"
export FISHPASS_PASSWORD="changeme"

# Only used with --export-to-azure. Sign in first with `az login` (see README.md).
export AZURE_STORAGE_ACCOUNT_URL="${AZURE_STORAGE_ACCOUNT_URL:-https://changeme.blob.core.windows.net}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$SCRIPT_DIR/scripts/run_model.py" "$PLAN_CODE" "$@"
