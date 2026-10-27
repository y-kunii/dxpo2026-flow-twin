#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
app_dir="$(pwd)"
compile_dir="$(mktemp -d /tmp/spatial-rosbridge-test.XXXXXX)"
server_log="$compile_dir/rosbridge-mock.log"
cleanup() {
  if [[ -n "${server_pid:-}" ]]; then
    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
  fi
  rm -rf "$compile_dir"
}
trap cleanup EXIT

npx tsc src/dynamic/DynamicTrackingContract.ts src/dynamic/RosbridgeTransformClient.ts src/dynamic/RosbridgeTransformPublisher.ts scripts/rosbridge_process_integration_test.ts \
  --outDir "$compile_dir" --target ES2022 --module NodeNext --moduleResolution NodeNext --skipLibCheck
python3 scripts/rosbridge_mock_server.py 19090 >"$server_log" 2>&1 &
server_pid=$!
for _ in $(seq 1 30); do
  if grep -q "listening" "$server_log"; then
    break
  fi
  if ! kill -0 "$server_pid" 2>/dev/null; then
    cat "$server_log"
    exit 1
  fi
  sleep 0.1
done
node --import "$app_dir/scripts/node_websocket_polyfill.mjs" \
  "$compile_dir/scripts/rosbridge_process_integration_test.js"
