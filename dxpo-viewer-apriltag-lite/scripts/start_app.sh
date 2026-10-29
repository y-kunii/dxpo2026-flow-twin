#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=control_token.sh
source "$ROOT/scripts/control_token.sh"
# shellcheck source=application_runtime_mode.sh
source "$ROOT/scripts/application_runtime_mode.sh"

app="${1:-}"
if [[ "$app" == 'status' ]]; then
  command -v docker >/dev/null || { echo 'Docker is not installed or is not on PATH' >&2; exit 2; }
  docker compose version >/dev/null 2>&1 || { echo 'Docker Compose v2 is not available' >&2; exit 2; }
  docker info >/dev/null 2>&1 || { echo 'Docker daemon is not running or is not accessible' >&2; exit 2; }
  printf 'Canonical integrated entry: http://localhost:5173/\n'
  printf 'Canonical integrated API:   http://127.0.0.1:8788/api/health\n\n'
  docker compose ps
  spatial_warn_external_published_apps
  exit 0
fi
case "$app" in
  all|top|suite) profile=system; services=(application-api web-viewer image-feature-worker frame-vlm-worker rosbridge); url=http://localhost:5173/ ;;
  colmap) profile=colmap-app; services=(colmap-api colmap-app); url=http://localhost:5180/ ;;
  apriltag-check) profile=apriltag-check-app; services=(apriltag-check-api apriltag-check-app); url=http://localhost:5181/ ;;
  spz-import) profile=spz-import-app; services=(spz-import-api spz-import-app); url=http://localhost:5182/ ;;
  scene-manager) profile=scene-manager-app; services=(scene-manager-api scene-manager-app); url=http://localhost:5183/ ;;
  project-manager) profile=project-manager-app; services=(project-manager-api project-manager-app); url=http://localhost:5184/ ;;
  package-builder) profile=package-builder-app; services=(package-builder-api package-builder-app); url=http://localhost:5186/ ;;
  field-memory) profile=field-memory-app; services=(field-memory-api field-memory-app); url=http://localhost:5187/ ;;
  site-insights) profile=site-insights-app; services=(site-insights-api site-insights-image-worker site-insights-vlm-worker site-insights-app); url=http://localhost:5188/ ;;
  frame-analysis) profile=frame-analysis-app; services=(frame-analysis-api frame-analysis-worker frame-analysis-app); url=http://localhost:5190/ ;;
  viewer) profile=viewer-app; services=(viewer-app); url=http://localhost:5185/ ;;
  *)
    printf 'Usage: %s {all|top|suite|status|colmap|apriltag-check|spz-import|scene-manager|project-manager|package-builder|field-memory|site-insights|frame-analysis|viewer}\n' "$0" >&2
    exit 2
    ;;
esac

if [[ "$app" == 'all' || "$app" == 'top' || "$app" == 'suite' ]]; then
  exec "$ROOT/scripts/start_system.sh"
fi

command -v docker >/dev/null || { echo 'Docker is not installed or is not on PATH' >&2; exit 2; }
docker compose version >/dev/null 2>&1 || { echo 'Docker Compose v2 is not available' >&2; exit 2; }
docker info >/dev/null 2>&1 || { echo 'Docker daemon is not running or is not accessible' >&2; exit 2; }

export LOCAL_UID="${LOCAL_UID:-$(id -u)}"
export LOCAL_GID="${LOCAL_GID:-$(id -g)}"
spatial_ensure_control_token "$ROOT"
if command -v explorer.exe >/dev/null 2>&1; then
  python3 "$ROOT/scripts/open_folder_bridge.py" ensure --repo-root "$ROOT" \
    || printf 'Warning: Windows Explorer bridge could not be started; folder paths remain copyable.\n' >&2
fi
spatial_activate_standalone_mode
# The source tree is bind-mounted into the application containers.  Recreate
# the selected services on every explicit start so a newly added API route is
# not hidden behind an already-running Python process.  Docker still reuses
# unchanged image layers, so this does not force a full image rebuild.
docker compose --profile "$profile" up --detach --build --force-recreate --wait --wait-timeout 180 "${services[@]}"

printf '\n%s is ready.\nOpen: %s\n' "$app" "$url"
printf 'Stop: docker compose stop %s\n' "${services[*]}"
spatial_warn_external_published_apps
