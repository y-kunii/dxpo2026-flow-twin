# crane_demo_dxpo

DXPO2026 出展デモ用の CRANE+ V2 マテハン動作パッケージ。
ROS 2 からコマンドを投げると、Gazebo 上で CRANE+ V2 が箱を掴んで運び、また掴んで戻す動作を繰り返す。

- 対象: ROS 2 Humble / Ignition Gazebo (gz sim 6, Fortress)
- ベース: [rt-net/crane_plus](https://github.com/rt-net/crane_plus) の `crane_plus_demo` を改変
- websocket ブリッジ（rosbridge）は使わず、ROS トピックで直接制御する

## セットアップ

CRANE+ 一式（`crane_plus_control` / `crane_plus_description` / `crane_plus_gazebo` /
`crane_plus_moveit_config` など）が `~/ros2_ws/src` 配下にある前提。

本パッケージはリポジトリ内の実体（`dxpo2026-flow-twin/cranev2_dxpo_demo`）を `~/ros2_ws/src` に
シンボリックリンクして colcon でビルドする（フォルダ名は `cranev2_dxpo_demo`、パッケージ名は `crane_demo_dxpo`）。

```sh
ln -s ~/work/dxpo2026-flow-twin/cranev2_dxpo_demo ~/ros2_ws/src/crane_demo_dxpo
cd ~/ros2_ws
colcon build --packages-select crane_demo_dxpo --symlink-install
```

> C++（`src/*.cpp`）を変更したら再ビルドが必要。launch / config / world / scripts は
> `--symlink-install` のおかげで再ビルド不要。新規ビルド・再ビルド後は各ターミナルで
> `source ~/ros2_ws/install/setup.bash` を読み直すこと。

## 起動（Gazebo）

各ターミナルの先頭で環境を読み込む。

```sh
source /opt/ros/humble/setup.bash
source ~/ros2_ws/install/setup.bash
```

### 1. Gazebo 一式（箱を置いた専用 world を渡す）

箱（aruco_cube）を掴み位置 place1=(0.2, -0.15) に置いた `dxpo_table.sdf` を、上流の
launch に `world_name:=` で渡す（上流パッケージは無変更）。

```sh
ros2 launch crane_plus_gazebo crane_plus_with_table.launch.py \
  world_name:=$(ros2 pkg prefix crane_demo_dxpo)/share/crane_demo_dxpo/worlds/dxpo_table.sdf
```

### 2. デモノード（Gazebo では `use_sim_time:=true` 必須）

```sh
ros2 launch crane_demo_dxpo demo.launch.py demo:='pick_and_place_with_pos' use_sim_time:=true
```

### 3. 動かす

単発でコマンドを投げる場合（`-w 1` でノードと接続してから送る）:

```sh
ros2 topic pub --once -w 1 /pick_and_place_topic std_msgs/msg/String "{data: motion1}"
```

連続デモ（箱を place1 ⇄ 固定位置で往復させ続ける）はループスクリプト:

```sh
scripts/crowd_loop.sh            # 無限ループ（Ctrl+C で停止）
LOOPS=2 scripts/crowd_loop.sh    # 2 周だけ
```

`crowd_loop.sh` は `/operating_status_topic` の `operating`→`idle` を監視し、各動作の完了を
待ってから次のコマンドを送る（取りこぼし・タイミングずれを防止）。

## コマンド一覧（`/pick_and_place_topic` に String で publish）

| 値 | 動作 |
| --- | --- |
| motion1 | 右前(place1)の箱を掴み、固定位置へ運ぶ |
| motion2 | 中央前(place2)の箱を掴み、固定位置へ運ぶ |
| motion3 | 左前(place3)の箱を掴み、固定位置へ運ぶ |
| motion4 | 固定位置の箱を掴み、右前(place1)へ戻す |
| motion5 | 固定位置の箱を掴み、中央前(place2)へ戻す |
| motion6 | 固定位置の箱を掴み、左前(place3)へ戻す |
| pose1〜3 | place1〜3 へ移動するだけ（掴まない・位置確認用） |
| pose_handover | アームから見て真左へ移動 |

状態通知トピック:

```sh
ros2 topic echo /operating_status_topic   # operating / idle
ros2 topic echo /gripper_status_topic      # open / close
```

## メモ

- 連続ループは place1 の箱 1 個で完結する `motion1` ⇄ `motion4`（`crowd_loop.sh` の既定）。
- Gazebo の摩擦掴みは place1=(0.2,-0.15) なら安定して掴める（回転も許容範囲）。
- 動作を速くする（キビキビ化）ため、`picking()` / `put_back()` 末尾の「少し持ち上げ」と
  末尾のグリッパ閉じを省略している（コメントアウトで残してある）。さらに速くするには
  URDF の `JOINT_VELOCITY_LIMIT`（既定 2.0 rad/s）の引き上げが必要。
