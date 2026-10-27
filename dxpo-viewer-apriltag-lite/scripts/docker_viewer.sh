#!/usr/bin/env bash
set -euo pipefail

docker compose --profile viewer run --rm --service-ports web-viewer
