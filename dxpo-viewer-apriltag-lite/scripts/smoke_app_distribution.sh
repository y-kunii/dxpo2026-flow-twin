#!/usr/bin/env bash
set -euo pipefail

if (($# != 1)); then
  printf 'Usage: %s APPLICATION.zip\n' "$0" >&2
  exit 2
fi

zip_path="$(realpath "$1")"
[[ -f "$zip_path" ]] || { printf 'Distribution ZIP not found: %s\n' "$zip_path" >&2; exit 2; }
command -v unzip >/dev/null || { printf 'unzip is required\n' >&2; exit 2; }

temporary="$(mktemp -d /tmp/spatial-dist-smoke.XXXXXX)"
resolved_temporary="$(realpath "$temporary")"
case "$resolved_temporary" in
  /tmp/spatial-dist-smoke.*) ;;
  *) printf 'Unsafe smoke-test directory: %s\n' "$resolved_temporary" >&2; exit 2 ;;
esac

project_name="spatial-dist-smoke-$$"
app_port="${SMOKE_APP_PORT:-18080}"
distribution_root=""

cleanup() {
  status=$?
  if [[ -n "$distribution_root" && -f "$distribution_root/docker-compose.yml" ]]; then
    if ((status != 0)); then
      (
        cd "$distribution_root"
        COMPOSE_PROJECT_NAME="$project_name" APP_PORT="$app_port" \
          docker compose logs --tail=200
      ) >&2 || true
    fi
    (
      cd "$distribution_root"
      COMPOSE_PROJECT_NAME="$project_name" APP_PORT="$app_port" \
        docker compose down --volumes --remove-orphans
    ) >/dev/null 2>&1 || true
  fi
  rm -rf -- "$resolved_temporary"
  return "$status"
}
trap cleanup EXIT

unzip -q "$zip_path" -d "$resolved_temporary"
mapfile -t compose_files < <(find "$resolved_temporary" -mindepth 2 -maxdepth 2 -name docker-compose.yml -type f)
if ((${#compose_files[@]} != 1)); then
  printf 'Expected one distribution root, found %s\n' "${#compose_files[@]}" >&2
  exit 2
fi
distribution_root="$(dirname "${compose_files[0]}")"
cd "$distribution_root"

COMPOSE_PROJECT_NAME="$project_name" APP_PORT="$app_port" \
  SPATIAL_START_TIMEOUT_SEC="${SPATIAL_START_TIMEOUT_SEC:-600}" \
  ./scripts/start_distribution.sh

python3 - "$app_port" "$distribution_root/package_recipe.json" <<'PY'
import json
import sys
import urllib.request
from pathlib import Path

port = int(sys.argv[1])
recipe = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
expected_features = set(recipe.get("api_features", []))
with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=15) as response:
    html = response.read().decode("utf-8")
with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=15) as response:
    health = json.load(response)
enabled_features = set(health.get("enabled_features", []))
if not expected_features.issubset(enabled_features):
    raise RuntimeError(
        "distribution_api_features_missing:"
        + ",".join(sorted(expected_features - enabled_features))
    )
with urllib.request.urlopen(
    f"http://127.0.0.1:{port}/scene_package/distribution_index.json", timeout=15
) as response:
    scene_index = json.load(response)
scene_packages = scene_index.get("packages", [])
viewer_status = None
if scene_packages:
    viewer_url = scene_packages[0].get("viewer_url")
    if not isinstance(viewer_url, str) or not viewer_url.startswith("/"):
        raise RuntimeError("distribution_viewer_url_invalid")
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{viewer_url}", timeout=15) as response:
        viewer_status = response.status
models = None
if "frame-analysis" in expected_features:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/api/frame-analysis/models", timeout=15
    ) as response:
        models = json.load(response)
print(json.dumps({
    "status": "verified",
    "web_status": 200,
    "html_entrypoint_present": '<div id="app">' in html,
    "enabled_features": sorted(enabled_features),
    "scene_package_count": len(scene_packages),
    "viewer_status": viewer_status,
    "model_catalog": models,
}, ensure_ascii=False, indent=2))
PY

COMPOSE_PROJECT_NAME="$project_name" APP_PORT="$app_port" docker compose ps --format json
