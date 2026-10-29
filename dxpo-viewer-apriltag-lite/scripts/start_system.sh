#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=control_token.sh
source "$ROOT/scripts/control_token.sh"
# shellcheck source=application_runtime_mode.sh
source "$ROOT/scripts/application_runtime_mode.sh"

fail() {
  printf 'System startup failed: %s\n' "$1" >&2
  exit 2
}

command -v docker >/dev/null || fail 'Docker is not installed or is not on PATH'
docker compose version >/dev/null 2>&1 || fail 'Docker Compose v2 is not available'
docker info >/dev/null 2>&1 || fail 'Docker daemon is not running or is not accessible'

export LOCAL_UID="${LOCAL_UID:-$(id -u)}"
export LOCAL_GID="${LOCAL_GID:-$(id -g)}"
export ROSBRIDGE_PORT="${ROSBRIDGE_PORT:-9090}"
spatial_ensure_control_token "$ROOT"
if command -v explorer.exe >/dev/null 2>&1; then
  python3 "$ROOT/scripts/open_folder_bridge.py" ensure --repo-root "$ROOT" \
    || printf 'Warning: Windows Explorer bridge could not be started; folder paths remain copyable.\n' >&2
fi
spatial_activate_integrated_mode

case "$ROSBRIDGE_PORT" in
  ''|*[!0-9]*) fail 'ROSBRIDGE_PORT must be an integer from 1 to 65535' ;;
esac
if ((ROSBRIDGE_PORT < 1 || ROSBRIDGE_PORT > 65535)); then
  fail 'ROSBRIDGE_PORT must be an integer from 1 to 65535'
fi

windows_portproxy_for_rosbridge() {
  [[ -n "${WSL_INTEROP:-}" || -e /proc/sys/fs/binfmt_misc/WSLInterop ]] || return 1
  local netsh='/mnt/c/Windows/System32/netsh.exe'
  [[ -x "$netsh" ]] || return 1
  timeout 3 "$netsh" interface portproxy show v4tov4 2>/dev/null \
    | tr -d '\r' \
    | awk -v port="$ROSBRIDGE_PORT" '$2 == port { found=1 } END { exit(found ? 0 : 1) }'
}

if [[ -n "${ROSBRIDGE_BIND_ADDRESS:-}" ]]; then
  export ROSBRIDGE_BIND_ADDRESS
elif windows_portproxy_for_rosbridge; then
  # A Windows portproxy targets the WSL VM address, not WSL loopback. Bind the
  # managed container to the WSL interfaces so the explicitly configured
  # Windows forwarding rule can complete a WebSocket handshake.
  export ROSBRIDGE_BIND_ADDRESS='0.0.0.0'
  printf 'Windows portproxy detected for port %s; exposing rosbridge to the WSL interfaces.\n' "$ROSBRIDGE_PORT"
else
  export ROSBRIDGE_BIND_ADDRESS='127.0.0.1'
fi

port_is_open() {
  timeout 1 bash -c "</dev/tcp/127.0.0.1/${ROSBRIDGE_PORT}" >/dev/null 2>&1
}

websocket_is_available() {
  command -v curl >/dev/null || return 1
  local response
  response="$(
    curl --http1.1 --include --no-buffer --max-time 1 \
      --header 'Connection: Upgrade' \
      --header 'Upgrade: websocket' \
      --header 'Sec-WebSocket-Version: 13' \
      --header 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
      "http://127.0.0.1:${ROSBRIDGE_PORT}/" 2>/dev/null || true
  )"
  grep -q '101 Switching Protocols' <<<"$response"
}

services=(application-api web-viewer image-feature-worker frame-vlm-worker)
rosbridge_status='managed container'

if port_is_open; then
  running_container="$(
    docker ps --filter "publish=${ROSBRIDGE_PORT}" --format '{{.Names}}' \
      | head -n 1
  )"
  if [[ "$running_container" == 'spatial-rosbridge' ]]; then
    services+=(rosbridge)
  elif [[ "$running_container" == 'spatial-rosbridge-mock' ]]; then
    printf 'Stopping rosbridge-mock so the real rosbridge can use port %s.\n' "$ROSBRIDGE_PORT"
    docker compose --profile dynamic-test stop rosbridge-mock
    services+=(rosbridge)
  elif websocket_is_available; then
    rosbridge_status="existing WebSocket endpoint on port ${ROSBRIDGE_PORT}"
    printf 'Using %s; the managed rosbridge container will not be started.\n' "$rosbridge_status"
  else
    fail "port ${ROSBRIDGE_PORT} is occupied by a non-WebSocket service"
  fi
else
  services+=(rosbridge)
fi

docker compose --profile system up \
  --detach \
  --build \
  --wait \
  --wait-timeout "${SPATIAL_START_TIMEOUT_SEC:-300}" \
  "${services[@]}"

printf '\nSpatial 3DGS system is ready.\n'
printf 'Application Preview: http://localhost:5173/?preview=pipeline\n'
printf 'Application API:     http://127.0.0.1:8788/api/health\n'
printf 'rosbridge:           ws://127.0.0.1:%s (%s, bind %s)\n' "$ROSBRIDGE_PORT" "$rosbridge_status" "$ROSBRIDGE_BIND_ADDRESS"
spatial_warn_external_published_apps
printf '\nLichtFeld Studio and the external camera/CounterApp remain operator-controlled external processes.\n'
