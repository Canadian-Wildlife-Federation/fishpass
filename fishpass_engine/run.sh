#!/usr/bin/env bash
set -euo pipefail

if [ -z "${1:-}" ]; then
  echo "Usage: $0 <plan_code>" >&2
  exit 1
fi
PLAN_CODE="$1"

export FISHPASS_HOST="localhost"
export FISHPASS_PORT="5432"
export FISHPASS_DBNAME="fishpass"
export FISHPASS_USER="fishpass"
export FISHPASS_PASSWORD="changeme"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$SCRIPT_DIR/scripts/run_model.py" "$PLAN_CODE"
