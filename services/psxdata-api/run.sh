#!/usr/bin/env bash
# Run the self-hosted psxdata-api service (https://github.com/mtauha/psxdata-api) locally,
# without Docker, in its own virtualenv so its dependencies never mix with PSX Intelligence's.
#
#   services/psxdata-api/run.sh            # install (first run) and serve on 127.0.0.1:8100
#   PSXDATA_API_PORT=8200 services/psxdata-api/run.sh
#
# Version is pinned: psxdata-api v0.5.0 (commit 051a88b), which pins psxdata==1.2.0.
# Bump deliberately, then run the contract test (see docs/operations.md).
set -euo pipefail

PSXDATA_API_REF="v0.5.0"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/.venv"
PORT="${PSXDATA_API_PORT:-8100}"
PYTHON="${PYTHON:-python3.12}"

if [[ ! -x "$VENV/bin/uvicorn" ]] || [[ "$(cat "$VENV/.ref" 2>/dev/null)" != "$PSXDATA_API_REF" ]]; then
  "$PYTHON" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet "psxdata-api @ git+https://github.com/mtauha/psxdata-api@${PSXDATA_API_REF}"
  echo "$PSXDATA_API_REF" > "$VENV/.ref"
fi

# Bound to localhost: the service must not be reachable from other machines.
exec "$VENV/bin/uvicorn" api.main:app --host 127.0.0.1 --port "$PORT" --workers 1
