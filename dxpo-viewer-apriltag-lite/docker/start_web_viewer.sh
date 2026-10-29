#!/usr/bin/env sh
set -eu

app_dir=/workspace/web_viewer/app

. /usr/local/lib/spatial/drop_web_privileges.sh
spatial_drop_web_privileges "$@"

marker="$app_dir/node_modules/.package-lock.sha256"
expected="$(sha256sum "$app_dir/package-lock.json" | cut -d ' ' -f 1)"
current=""
if [ -f "$marker" ]; then
  current="$(cat "$marker")"
fi

if [ "$current" != "$expected" ]; then
  echo "Viewer dependencies do not match package-lock.json; running npm ci."
  npm --prefix "$app_dir" ci --no-audit --prefer-offline
  printf '%s\n' "$expected" > "$marker"
fi

exec npm --prefix "$app_dir" run dev -- --host 0.0.0.0
