# カメラモデル

本書は3種類のカメラ表現を区別し、それぞれのスキーマ所有者を確定する。混同すると座標と画角の二重適用が起きるため、名前を厳密に使い分ける。

|表現|用途|所有|
|---|---|---|
|virtual rig camera|360度動画からSfM用のpinhole画像を切り出す仮想カメラ|`generation_pipeline/config/virtual_rig.default.json`|
|COLMAP camera|SfMが推定・使用する内部パラメータと姿勢|MRPの `colmap/sparse/` `colmap/text/`|
|camera preset|Viewerで検討する**設置候補カメラ**|Scene Packageの `config/camera_presets.json`|

## 1. virtual rig camera（3D再構成）

equirectangular panoramaから決定論的にpinhole画像を生成する。

```json
{
  "hfov_deg": 90.0,
  "vfov_deg": 90.0,
  "pitches_deg": [-35.0, 0.0, 35.0],
  "yaw_steps": 4,
  "camera_model": "SIMPLE_PINHOLE"
}
```

`pitches_deg` × `yaw_steps` が1 panoramaあたりの仮想カメラ数を決める（既定は 3 × 4 = 12）。COLMAPへはknown intrinsics、fixed sensor-from-rigとして渡す。rig構成はSfM中に変更しない。

## 2. COLMAP camera（3D再構成）

正式経路はCOLMAP 4.1 CUDA、`SIMPLE_PINHOLE`、fixed-rig sequential reconstructionである。exhaustive rig、camera3単独model、手動camera poseは使わない。

camera trajectoryはMRPの `camera_trajectory.json` が所有し、Viewerは床・壁・天井の判定に使うtrajectory centerとmedian heightをここから得る。

## 3. camera preset（3Dシーン閲覧）

Viewerが読む設置候補カメラの契約は、`web_viewer/app/src/scene/CameraPresets.ts` の `CameraPreset` が正である。

```jsonc
{
  "cameras": [
    {
      "id": "metric_trajectory_start",     // 必須
      "name": "Metric trajectory start",   // 必須
      "position_m": [x, y, z],             // 必須。world meter
      "look_at_m": [x, y, z],              // 必須。注視点をworld meterで指定する
      "up_world": [0.0, 1.0, 0.0],         // 任意。既定は +Y
      "vertical_fov_deg": 60.0,            // 必須。垂直FOVのみを持つ
      "resolution": [1600, 1000],          // 必須。水平FOVはaspectから導出する
      "near_m": 0.1,                       // 任意
      "far_m": 1.5                         // 任意。CameraFrustumの既定描画距離
    }
  ]
}
```

### 規約

- **向きは `look_at_m` で表す。** オイラー角（`rotation_deg`）は使わない。
- **水平FOVは保存しない。** `vertical_fov_deg` と `resolution` のaspectから導出する（`CameraFrustum.ts`）。同じ量を2箇所に持たないための規約である。
- `up_world` が forward と平行な縮退時は、right軸を `[1, 0, 0]` にフォールバックする。
- presetの生成元は `generation_pipeline/tools/generate_apriltag_camera_presets.py` である。

### 契約例

Git管理する契約例は `sample_scene_package/config/camera_presets.json` である。`generation_pipeline/tests/test_scene_contracts.py` が同ファイルを本契約に照らして検証するため、契約を変更する場合はfixtureとテストを同時に更新する。

## 4. lens preset

`config/lens_presets.json` はレンズ候補を列挙する。camera presetと異なり、設置位置を持たない。

```json
{
  "lenses": [
    {
      "id": "wide_90",
      "name": "Wide 90deg",
      "horizontal_fov_deg": 90,
      "vertical_fov_deg": 60,
      "distortion": { "k1": 0, "k2": 0, "p1": 0, "p2": 0 }
    }
  ]
}
```

歪みパラメータは現時点でViewerの描画・遮蔽判定に使用しない。記録のみとする。

## 関連文書

- [座標系](coordinate_system.md)
- [Scene Package format](scene_package_format.md)
- [再構成ツールの概要](../generation_pipeline/tools/README.md)
