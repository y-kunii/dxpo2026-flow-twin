#!/usr/bin/env bash
set -euo pipefail

# Repository-level entry point retained for the documented acceptance command.
# The implementation belongs to the Viewer because it compiles and exercises
# the Viewer dynamic-tracking contract.
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec bash "$repo_root/web_viewer/app/scripts/run_dynamic_tracking_virtual_acceptance.sh"
