#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=application_runtime_mode.sh
source "$ROOT/scripts/application_runtime_mode.sh"

docker compose --profile system stop "${SPATIAL_INTEGRATED_SERVICES[@]}"
python3 "$ROOT/scripts/open_folder_bridge.py" stop --repo-root "$ROOT" || true
