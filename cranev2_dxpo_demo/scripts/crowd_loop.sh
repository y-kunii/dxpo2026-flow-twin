#!/usr/bin/env bash
# DXPO デモ: pick_and_place_with_pos ノードへコマンドを順に投げ続ける。
# 各動作の完了は「固定時間待ち(WAIT)」で待つ。動作時間がほぼ一定なので、
# status 監視(別プロセスの ros2 topic echo)より確実で取りこぼしが無い。
#
#   前提: 別ターミナルで以下が起動済みであること
#     1) ros2 launch crane_plus_gazebo crane_plus_with_table.launch.py \
#          world_name:=$(ros2 pkg prefix crane_demo_dxpo)/share/crane_demo_dxpo/worlds/dxpo_table.sdf
#     2) ros2 launch crane_demo_dxpo demo.launch.py demo:='pick_and_place_with_pos' use_sim_time:=true
#
# 使い方 (ターミナル3で):
#   ./crowd_loop.sh                 # 無限ループ
#   LOOPS=3 ./crowd_loop.sh         # 3周で終了
#   WAIT=30 ./crowd_loop.sh         # 1動作あたりの待ち時間(秒)。1動作にかかる時間より少し長めに
set -euo pipefail

# ROS の setup.bash は未定義変数を参照するため、source 中だけ nounset を外す
set +u
source /opt/ros/humble/setup.bash
source "$HOME/ros2_ws/install/setup.bash"
set -u

CMD_TOPIC="/pick_and_place_topic"
LOOPS="${LOOPS:-0}"     # 0 = 無限
WAIT="${WAIT:-28}"      # 1動作の待ち時間(秒)。motion1≈24s, motion4≈22s なので少し余裕をみた値

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

cleanup() {
  echo
  echo "停止しました"
  exit 0
}
trap cleanup INT TERM

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
  echo "[$(date +%H:%M:%S)] -> $cmd"
  # -w 1: 購読者(ノード)と接続してから publish。これが無いと最初のメッセージが消える。
  # ノード未起動時に永久待ちしないよう timeout 10s で打ち切り、原因を表示する。
  if ! timeout 10 ros2 topic pub --once -w 1 "$CMD_TOPIC" std_msgs/msg/String "{data: $cmd}" >/dev/null; then
    echo "  ! '$cmd' を publish できません（ノードが起動していない可能性）" >&2
    cleanup
  fi
  sleep "$WAIT"          # 動作完了を固定時間で待つ
  echo "         done"
}

echo "ループ開始。Ctrl+C で停止します。(WAIT=${WAIT}s)"
count=0
while :; do
  reset_box                              # 各周の頭で箱を定位置(place1)へ戻す
  for cmd in "${SEQUENCE[@]}"; do
    send "$cmd"
  done
  count=$((count + 1))
  if [ "$LOOPS" -ne 0 ] && [ "$count" -ge "$LOOPS" ]; then
    echo "完了 ($count 周)"
    break
  fi
done

cleanup
