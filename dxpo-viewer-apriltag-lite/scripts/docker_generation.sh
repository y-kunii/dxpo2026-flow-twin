#!/usr/bin/env bash
set -euo pipefail

docker compose --profile generation run --rm generation
