#!/usr/bin/env bash
set -euo pipefail

app_dir="$(cd "$(dirname "$0")/.." && pwd)"
repo_root="$(cd "$app_dir/../.." && pwd)"
compile_dir="$(mktemp -d /tmp/spatial-rosbridge-docker-test.XXXXXX)"
cleanup() {
  rm -rf "$compile_dir"
}
trap cleanup EXIT

docker compose -f "$repo_root/docker-compose.yml" --profile dynamic-test up -d --build rosbridge-mock

for _ in $(seq 1 30); do
  if docker compose -f "$repo_root/docker-compose.yml" ps --status running --services | grep -qx "rosbridge-mock"; then
    break
  fi
  sleep 0.2
done

if ! docker compose -f "$repo_root/docker-compose.yml" ps --status running --services | grep -qx "rosbridge-mock"; then
  docker compose -f "$repo_root/docker-compose.yml" logs rosbridge-mock
  exit 1
fi

cd "$app_dir"
npx tsc src/dynamic/DynamicTrackingContract.ts src/dynamic/RosbridgeTransformClient.ts src/dynamic/RosbridgeTransformPublisher.ts scripts/rosbridge_process_integration_test.ts \
  --outDir "$compile_dir" --target ES2022 --module NodeNext --moduleResolution NodeNext --skipLibCheck
ROSBRIDGE_URL="ws://127.0.0.1:9090" \
  node --import "$app_dir/scripts/node_websocket_polyfill.mjs" \
  "$compile_dir/scripts/rosbridge_process_integration_test.js"
echo "Docker rosbridge test passed; spatial-rosbridge-mock remains running on ws://127.0.0.1:9090"
