#!/usr/bin/env bash

# Runtime-mode contract shared by the integrated and standalone launchers.
# Only one repository-owned Web/API mode is kept running at a time. Derived
# distribution projects are reported but never stopped implicitly.

SPATIAL_INTEGRATED_SERVICES=(
  application-api
  web-viewer
  image-feature-worker
  frame-vlm-worker
  rosbridge
)

SPATIAL_STANDALONE_SERVICES=(
  colmap-api colmap-app
  apriltag-check-api apriltag-check-app
  spz-import-api spz-import-app
  scene-manager-api scene-manager-app
  project-manager-api project-manager-app
  package-builder-api package-builder-app
  field-memory-api field-memory-app
  site-insights-api site-insights-image-worker site-insights-vlm-worker site-insights-app
  frame-analysis-api frame-analysis-worker frame-analysis-app
  viewer-app
)

spatial_stop_running_services() {
  local reason="$1"
  shift
  local running target
  local -a selected=()
  while IFS= read -r running; do
    [[ -n "$running" ]] || continue
    for target in "$@"; do
      if [[ "$running" == "$target" ]]; then
        selected+=("$running")
        break
      fi
    done
  done < <(docker compose ps --services --status running 2>/dev/null || true)
  if ((${#selected[@]} == 0)); then
    return
  fi
  printf '%s: %s\n' "$reason" "${selected[*]}"
  docker compose stop "${selected[@]}"
}

spatial_activate_integrated_mode() {
  spatial_stop_running_services \
    'Stopping standalone application mode before starting the integrated suite' \
    "${SPATIAL_STANDALONE_SERVICES[@]}"
}

spatial_activate_standalone_mode() {
  spatial_stop_running_services \
    'Stopping the integrated suite before starting a standalone application' \
    "${SPATIAL_INTEGRATED_SERVICES[@]}"
  spatial_stop_running_services \
    'Stopping the previous standalone application mode' \
    "${SPATIAL_STANDALONE_SERVICES[@]}"
}

spatial_warn_external_published_apps() {
  local name project ports
  local found=0
  while IFS='|' read -r name project ports; do
    [[ -n "$name" && "$project" != 'spatial-3dgs-system' ]] || continue
    if [[ "$ports" =~ (0\.0\.0\.0|127\.0\.0\.1|\[::\]):(51[0-9]{2}|80[0-9]{2}|87[0-9]{2}|90[0-9]{2}) ]]; then
      if ((found == 0)); then
        printf '\nNotice: separately deployed application containers are still publishing ports:\n'
      fi
      printf '  %s (%s): %s\n' "$name" "${project:-no-compose-project}" "$ports"
      found=1
    fi
  done < <(
    docker ps --format '{{.Names}}|{{.Label "com.docker.compose.project"}}|{{.Ports}}' 2>/dev/null || true
  )
  if ((found)); then
    printf 'These derived deployments are not stopped automatically. Stop them from their own extracted package when no longer needed.\n'
  fi
}
