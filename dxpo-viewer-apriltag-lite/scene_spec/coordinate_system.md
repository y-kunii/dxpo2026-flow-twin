# ROS-to-Viewer coordinate contract (2026-07)

Each schema-v1.2 Scene Package declares both coordinate systems and their
conversion. The Viewer applies the package value; it has no scene-name-based
coordinate conversion.

```yaml
coordinate_system:
  ros:
    handedness: right
    x: forward
    y: left
    z: up
  viewer:
    handedness: right
    x: right
    y: up
    z: backward
  T_viewer_ros:
    translation: [0, 0, 0]
    rotation_xyzw: [-0.5, 0.5, 0.5, 0.5]
```

The exact meaning is:

```text
p_viewer = R_viewer_ros * p_ros + t_viewer_ros
```

For a received relative pose the Viewer uses the conjugated transform
`T_viewerParent_viewerChild = T_viewer_ros * T_rosParent_rosChild *
inverse(T_viewer_ros)` before composing with its fixed-tag annotation. Units
are metres and quaternion components are `x, y, z, w`.

Every package and ROS message quaternion must be finite, non-zero, and unit
length within `abs(norm - 1) <= 1e-6`. Invalid values are rejected at the
package validation, Viewer load, and rosbridge receive boundaries; they are
not normalized implicitly.

## 座標系

本書はScene PackageとViewerが共有する座標契約を記載する。回帰基準値の出典は元の実装計画書6章である（計画書はこの配布版に含まれない）。

## 1. world座標（`gs_world`）

|項目|値|
|---|---|
|単位|メートル|
|up軸|`+Y`|
|right軸|`+X`|
|forward軸|`+Z`|
|quaternion順序|`xyzw`|

`scene_manifest.json` の `coordinate_system` と `unit` がこの契約を宣言する。Viewer、Proxy、測距、遮蔽、動的重畳はすべて同じ `gs_world` 上で動作し、形式ごとの別座標系を持たない。

## 2. 生成座標からworldへの変換

```text
world = scale × Rx(180°) × horizontalized_colmap
```

変換パラメータは `config/world_alignment.json` が**単独で所有**する。

```json
{
  "unit": "meter",
  "status": "verified | unverified",
  "method": "apriltag | apriltag_pending | ...",
  "generated_to_world": {
    "scale": 1.0,
    "rotation_quaternion_xyzw": [0, 0, 0, 1],
    "translation_m": [0, 0, 0]
  }
}
```

`status` が `verified` でない場合、Viewer上の距離値は参考値として扱い、`measurement` / `surface_placement` / `fov` / `occlusion` を有効にしない。

## 3. 二重適用の禁止

同じtransformを2回適用してはならない。これは実測で最も発生しやすい不具合であり、以下を規約とする。

- world alignmentはScene Package読込時に**一度だけ**適用する。
- SPZでは、LichtFeld座標からの軸変換を**Spark decoderが担う**。Viewer側で追加の軸変換を書かない。
- Gaussian PLY / RADでは、同じ変換が自動適用されると**仮定しない**。asset単位で coordinate stage、up axis、handedness、scale、rotation、translation、およびGaussian covariance / extentへのscale適用を検証する。
- LichtFeld Studioへ渡す `lfs_ready/` は、`colmap_text/` と `sparse/0/` に**同一の水平化回転**を適用済みとする。軌跡だけが傾く組合せはfail-closedで拒否する。
- Studio側で追加すべき / すべきでないtransformは、MRPの `lfs_input/manifest.json` に明示する。

## 4. coordinate stage

assetとPackageは、どの段階の座標にあるかを `coordinate_stage` として記録する。関連付け検証はこの値の一致を条件とする。

|stage|意味|
|---|---|
|生成直後|3DGS / COLMAPの生成単位。メートルとは限らない|
|horizontalized|rig-up水平化を適用済み。スケールは未適用|
|world (metric)|scale適用済み。メートル、`+Y` up|

## 5. 回帰基準（`metric-scene-v1`）

次の値は固定であり、都合よく変更しない。

|項目|値|
|---|---|
|tag|`metric-scene-v1`|
|scene_id|`room_office_f26_apriltag`|
|scale|`0.5764923484005094 m / generated unit`|
|AprilTag|ID 0、黒枠一辺 `0.082 m`|
|world transform|`world = scale × Rx(180°) × horizontalized_colmap`|

## 6. 動的重畳の座標

動的objectは静的world alignmentを変更・二重適用しない。

```text
T_viewer_box = T_viewer_fixedTag × T_viewer_ros × T_ros_fixedTag_box × inverse(T_viewer_ros)
```

- `T_gs_fixedTag` — 固定タグframeから `gs_world` への姿勢。Scene Packageの `annotations/fixed_tag_map.json` が所有する。**Identityを仮定せず、実測して登録する。**
- `T_ros_fixedTag_box` — カメラ側PCがpublishする`TransformStamped`。ROS座標系の親から子への変換である。
- `T_viewer_ros` — Scene Packageが宣言するROS座標系からViewer座標系への固定変換である。Viewerはこれを共役変換として一度だけ適用する。
- render座標またはmodel軸が異なる場合だけ `T_render_box = T_render_viewer × T_viewer_box × T_box_model` を使う。

ROS側のstampはmessageの順序付けにのみ使用し、timeout判定はWebSocketのmonotonic受信時刻で行う。

## 関連文書

- [スケール合わせ](scale_alignment.md)
- [カメラモデル](camera_model.md)
- [Scene Package format](scene_package_format.md)
- [動的追跡の実装](../web_viewer/app/src/dynamic/DynamicTrackingContract.ts)
