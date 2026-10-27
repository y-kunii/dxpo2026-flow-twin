#!/usr/bin/env bash
set -euo pipefail

docker compose --profile packager run --rm scene-packager
