# dxpo2026-flow-twin
DXPO2026 出展デモ：ものの滞留をデジタルツインで可視化（OSMO360 + Gaussian Splatting + エッジ認識）

## 構成

- [`twin-gateway-rosbridge/`](twin-gateway-rosbridge/) … rosbridge 経由のデジタルツイン連携ゲートウェイ
- [`cranev2_dxpo_demo/`](cranev2_dxpo_demo/) … CRANE+ V2 のマテハン動作デモ（ROS 2 + Gazebo）。
  ROS からコマンドを投げて箱を掴んで運ぶ動作を繰り返す。詳細は
  [cranev2_dxpo_demo/README.md](cranev2_dxpo_demo/README.md) を参照。
