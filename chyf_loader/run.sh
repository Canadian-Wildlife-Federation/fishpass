#!/usr/bin/env bash
set -euo pipefail

# only required if postgresql isn't on the path
#export PATH="/usr/lib/postgresql/16/bin:$PATH"

export FISHPASS_HOST="localhost"
export FISHPASS_PORT="5432"
export FISHPASS_DBNAME="fishpass"
export FISHPASS_USER="fishpass"
export FISHPASS_PASSWORD="changeme"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$SCRIPT_DIR/scripts/load.py" "$@"
