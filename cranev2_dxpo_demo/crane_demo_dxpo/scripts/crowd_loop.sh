#!/usr/bin/env bash
# DXPO デモ: pick_and_place_with_pos ノードへコマンドを順に投げ続ける。
# 各動作の完了を /operating_status_topic (operating -> idle) で検知してから次を投げる。
#
#   前提: 別ターミナルで以下が起動済みであること
#     1) ros2 launch crane_plus_gazebo crane_plus_with_table.launch.py
#     2) ros2 launch crane_demo_dxpo demo.launch.py demo:='pick_and_place_with_pos' use_sim_time:=true
#
# 使い方 (ターミナル3で):
#   ./crowd_loop.sh                 # 無限ループ
#   LOOPS=3 ./crowd_loop.sh         # 3周で終了
#   TIMEOUT=90 ./crowd_loop.sh      # 1動作の完了待ち上限(秒)。これを超えたら異常として中断
set -euo pipefail

# ROS の setup.bash は未定義変数を参照するため、source 中だけ nounset を外す
set +u
source /opt/ros/humble/setup.bash
source "$HOME/ros2_ws/install/setup.bash"
set -u

CMD_TOPIC="/pick_and_place_topic"
STATUS_TOPIC="/operating_status_topic"
LOOPS="${LOOPS:-0}"        # 0 = 無限
TIMEOUT="${TIMEOUT:-40}"   # 1動作の完了待ち上限(秒)。超えても中断せず次へ進む(status取りこぼし対策)

# 毎周の頭で箱を place1 に戻す(Gazebo専用)。摩擦掴みのズレ蓄積を相殺し長時間安定させる。
# 実機や不要時は BOX_RESET=0 で無効化。
BOX_RESET="${BOX_RESET:-1}"
WORLD="${WORLD:-default}"
BOX_MODEL="${BOX_MODEL:-aruco_cube_0}"
BOX_X="${BOX_X:-0.2}"; BOX_Y="${BOX_Y:--0.15}"; BOX_Z="${BOX_Z:-1.05}"

# 投げるコマンド列。place1(右前)の箱1個で完結するループ。
# motion1:右前(place1)の箱を掴む→固定位置へ運ぶ
# motion4:固定位置の箱を掴む→右前(place1)へ戻す
SEQUENCE=(motion1 motion4)

# --- ステータスをバックグラウンドで常時受信してファイルに流す（取りこぼし防止） ---
STATUS_FILE="$(mktemp)"
ros2 topic echo "$STATUS_TOPIC" std_msgs/msg/String >"$STATUS_FILE" 2>/dev/null &
ECHO_PID=$!

# status受信(echo)が subscribe し終わるのを待つ。
# status は volatile QoS なので、繋がる前に出た最初の 'operating' は取りこぼす。
# 先に接続を確立しておかないと、1発目で operating 待ちタイムアウトになる。
sleep 3

cleanup() {
  echo
  kill "$ECHO_PID" 2>/dev/null || true
  rm -f "$STATUS_FILE"
  echo "停止しました"
  exit 0
}
trap cleanup INT TERM

# これまでに観測した 'idle'(動作完了) の回数。動作が1回終わるごとに1増える。
idle_count() { grep -Eo 'idle' "$STATUS_FILE" 2>/dev/null | wc -l | tr -d ' '; }

# 箱を place1 に戻す(Gazebo set_pose)。失敗しても止めない。
reset_box() {
  [ "$BOX_RESET" = "1" ] || return 0
  if ign service -s "/world/$WORLD/set_pose" \
       --reqtype ignition.msgs.Pose --reptype ignition.msgs.Boolean --timeout 2000 \
       --req "name: \"$BOX_MODEL\", position: {x: $BOX_X, y: $BOX_Y, z: $BOX_Z}" >/dev/null 2>&1; then
    echo "         (箱を place1 にリセット)"
  else
    echo "  ! 箱リセット失敗(set_pose) — BOX_RESET=0 で無効化できます" >&2
  fi
}

send() {
  local cmd="$1"
  local before; before="$(idle_count)"   # 投げる前の完了回数を記録
  echo "[$(date +%H:%M:%S)] -> $cmd"
  # -w 1: 購読者(ノード)と接続してから publish。これが無いと毎回のpubでメッセージがランダムに消える。
  # ノード未起動時に永久待ちしないよう timeout 10s で打ち切り、原因を表示する。
  if ! timeout 10 ros2 topic pub --once -w 1 "$CMD_TOPIC" std_msgs/msg/String "{data: $cmd}" >/dev/null; then
    echo "  ! '$cmd' を publish できません（ノードが起動していない可能性）" >&2
    return 1
  fi
  # 動作完了 = 新しい 'idle' が増えること。'operating' は取りこぼしやすいので見ない。
  # status を取りこぼしても止めない: TIMEOUT 秒経ったら完了とみなして次へ進む。
  local elapsed=0
  while [ "$(idle_count)" -le "$before" ]; do
    sleep 0.5
    elapsed=$((elapsed + 1))
    if [ "$elapsed" -ge $((TIMEOUT * 2)) ]; then
      echo "         (完了statusを検知できず ${TIMEOUT}s 経過。次へ進みます)" >&2
      break
    fi
  done
  echo "         done"
}

echo "ステータス監視を開始しました。Ctrl+C で停止します。"
count=0
while :; do
  reset_box                              # 各周の頭で箱を定位置(place1)へ戻す
  for cmd in "${SEQUENCE[@]}"; do
    send "$cmd" || cleanup
  done
  count=$((count + 1))
  if [ "$LOOPS" -ne 0 ] && [ "$count" -ge "$LOOPS" ]; then
    echo "完了 ($count 周)"
    break
  fi
done

cleanup
