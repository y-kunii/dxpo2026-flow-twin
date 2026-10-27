#!/usr/bin/env sh
set -eu

app_dir=/workspace/web_viewer/app
target="${SPATIAL_APP_TARGET:-}"

. /usr/local/lib/spatial/drop_web_privileges.sh
spatial_drop_web_privileges "$@"

case "$target" in
  colmap|apriltag-check|spz-import|scene-manager|project-manager|package-builder|field-memory|site-insights|frame-analysis|viewer) ;;
  custom)
    if [ -z "${SPATIAL_APP_MODES:-}" ]; then
      echo "spatial-app: SPATIAL_APP_MODES is required for a custom package" >&2
      exit 2
    fi
    ;;
  *)
    echo "spatial-app: SPATIAL_APP_TARGET must name a standalone app" >&2
    exit 2
    ;;
esac

marker="$app_dir/node_modules/.package-lock.sha256"
expected="$(sha256sum "$app_dir/package-lock.json" | cut -d ' ' -f 1)"
current=""
if [ -f "$marker" ]; then
  current="$(cat "$marker")"
fi

if [ "$current" != "$expected" ]; then
  echo "Spatial app dependencies do not match package-lock.json; running npm ci."
  npm --prefix "$app_dir" ci --no-audit --prefer-offline
  printf '%s\n' "$expected" > "$marker"
fi

exec npm --prefix "$app_dir" run dev -- --host 0.0.0.0 --mode "$target"
