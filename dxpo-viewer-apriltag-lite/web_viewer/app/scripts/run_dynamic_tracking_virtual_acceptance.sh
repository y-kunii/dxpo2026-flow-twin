#!/usr/bin/env bash
# Run the ROS-2-free Virtual Dynamic Tracking Acceptance. This starts only the
# repository's rosbridge-compatible mock in a separate process; it neither
# installs nor starts ROS 2, apriltag_ros, or camera software.
set -euo pipefail

app_dir="$(cd "$(dirname "$0")/.." && pwd)"
repo_root="$(cd "$app_dir/../.." && pwd)"
scene_id="${DYNAMIC_ACCEPTANCE_SCENE_ID:-room_office_f26_apriltag}"
output_dir="$repo_root/var/spatial/workspaces/legacy/$scene_id/dynamic_tracking_virtual_acceptance"
output_json="$output_dir/virtual_dynamic_tracking_acceptance.json"
compile_dir="$(mktemp -d /tmp/spatial-dynamic-acceptance.XXXXXX)"
server_log="$compile_dir/rosbridge-mock.log"

port="$(python3 - <<'PY'
import socket
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as value:
    value.bind(("127.0.0.1", 0))
    print(value.getsockname()[1])
PY
)"

cleanup() {
  if [[ -n "${server_pid:-}" ]]; then
    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
  fi
  rm -rf "$compile_dir"
}
trap cleanup EXIT

mkdir -p "$output_dir"
rm -f "$output_json.tmp"

cd "$app_dir"
npx tsc \
  src/dynamic/DynamicTrackingContract.ts \
  src/dynamic/RosbridgeTransformClient.ts \
  scripts/dynamic_tracking_contract_test.ts \
  scripts/rosbridge_process_integration_test.ts \
  --outDir "$compile_dir" --target ES2022 --module ESNext --moduleResolution Bundler --skipLibCheck

# Vite accepts extensionless TypeScript imports, whereas Node's ESM loader
# requires the emitted relative specifiers to name their .js files.  Rewrite
# only the throw-away acceptance build; source imports remain Vite-native.
find "$compile_dir" -type f -name '*.js' -exec sed -E -i "s@(from[[:space:]]+['\"]\.\.?/[^'\"]+)(['\"])@\\1.js\\2@g" {} +
find "$compile_dir" -type f -name '*.js' -exec sed -i 's/\.js\.js/\.js/g' {} +

# The temporary output directory is outside the Vite package root, so retain
# the ESM interpretation used by the compiled contract tests.
printf '{"type":"module"}\n' >"$compile_dir/package.json"

# The pure contract test covers the explicit coordinate, frame, rejection,
# timeout, and fixed-tag switch scenarios before the real WebSocket process.
node "$compile_dir/scripts/dynamic_tracking_contract_test.js" >"$compile_dir/contract-test.log"

python3 scripts/rosbridge_mock_server.py "$port" --virtual-trajectory >"$server_log" 2>&1 &
server_pid=$!
for _ in $(seq 1 50); do
  if grep -q "listening" "$server_log"; then
    break
  fi
  if ! kill -0 "$server_pid" 2>/dev/null; then
    cat "$server_log" >&2
    exit 1
  fi
  sleep 0.1
done
if ! grep -q "listening" "$server_log"; then
  echo "rosbridge mock did not become ready" >&2
  cat "$server_log" >&2
  exit 1
fi

tested_commit="$(git -C "$repo_root" rev-parse HEAD)"
VIRTUAL_DYNAMIC_TRACKING_ACCEPTANCE=1 \
VIRTUAL_UNIT_TEST_RESULT=passed \
DYNAMIC_ACCEPTANCE_SCENE_ID="$scene_id" \
DYNAMIC_ACCEPTANCE_COMMIT="$tested_commit" \
ROSBRIDGE_URL="ws://127.0.0.1:$port" \
node --import "$app_dir/scripts/node_websocket_polyfill.mjs" \
  "$compile_dir/scripts/rosbridge_process_integration_test.js" >"$output_json.tmp"

# Atomic publication: a consumer never observes a partial acceptance record.
mv "$output_json.tmp" "$output_json"
echo "Virtual Dynamic Tracking Acceptance: $output_json"
