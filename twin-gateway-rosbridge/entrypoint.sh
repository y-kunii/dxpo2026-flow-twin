#!/usr/bin/env bash
# rosbridge_entrypoint.sh
set -e
set -o pipefail

: "${WS_DIR:=/workspace/digital_twin_ws}"
: "${BUILD_ON_START:=1}"
: "${ROSBRIDGE_PORT:=9090}"

# --- source ---
source /opt/ros/humble/setup.bash || true

if [ -d "$WS_DIR" ]; then
  if [ -f "$WS_DIR/install/setup.bash" ]; then
    source "$WS_DIR/install/setup.bash"
  elif [ "$BUILD_ON_START" = "1" ]; then
    echo "[entrypoint] build workspace at $WS_DIR"
    cd "$WS_DIR"
    rm -rf build install log || true
    colcon build
    source "$WS_DIR/install/setup.bash"
  else
    echo "[entrypoint] install/ not found and BUILD_ON_START=0; skipping build."
  fi
fi

# --- mode ---
if [[ "${1:-start}" == "start" ]]; then
  echo "[entrypoint] starting rosbridge_websocket on :${ROSBRIDGE_PORT}"

  if [ -f "/opt/ros/humble/share/rosbridge_server/launch/rosbridge_websocket_launch.xml" ]; then
    ros2 launch rosbridge_server rosbridge_websocket_launch.xml port:=${ROSBRIDGE_PORT} &
  else
    ros2 launch rosbridge_server rosbridge_websocket.launch.py port:=${ROSBRIDGE_PORT} &
  fi
  BRIDGE_PID=$!

  term_handler() {
    echo "[entrypoint] terminating..."
    kill -TERM "$BRIDGE_PID" 2>/dev/null || true
    wait "$BRIDGE_PID" 2>/dev/null || true
    exit 143
  }
  trap term_handler SIGTERM SIGINT

  echo "[entrypoint] starting node: twin_bridge/service_server.py"
  exec ros2 run twin_bridge service_server.py
else
  exec "$@"
fi
