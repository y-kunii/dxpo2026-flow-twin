# Scene Package v1.3 self-contained canonical contract

This section takes precedence over legacy v1.0/v1.1 layout notes below.

The canonical package layout remains flat:

```text
data/scene_repository/packages/scenes/<scene_package_id>/
```

The physical directory does not encode the project hierarchy. The immutable
identity is declared in `scene_manifest.json`:

```yaml
schema_version: "1.3.0"
project_id: <immutable work project ID>
scene_id: <stable logical scene ID>
scene_package_id: <immutable artifact snapshot ID>
```

`scene_package_id` is write-once. A builder must reject an existing canonical
directory instead of modifying it. A package built under a new contract uses a
new ID, even when its source SPZ is the same.

Schema v1.2 additionally requires `coordinate_system.ros`,
`coordinate_system.viewer`, `coordinate_system.T_viewer_ros`, and
`dynamic_overlay`. The formal JSON Schema is
`scene_spec/schemas/scene_manifest_v1_2.schema.json`.

Schema v1.3 additionally requires `dataset/manifest.json`. The dataset is the
portable, application-facing snapshot and contains the complete MRP metadata,
registered frame images, person/COLMAP masks, COLMAP text/sparse model and
`database.db`, plus a SceneVersion-scoped Field Memory SQLite snapshot and its
content-addressed image files. Its schema is
`scene_spec/schemas/scene_dataset_manifest.schema.json`.

新規に登録するv1.3 Packageは、`scene_manifest.json`の任意項目`created_at`へPackage登録日時をISO 8601で記録する。撮影日時が入力契約から確定できる場合だけ`captured_at`を記録し、生成日時やファイル更新日時を撮影日時として代用しない。日時項目を持たない既存Packageは有効なまま維持し、管理UIが名称の日付やmanifest更新日時を出典付きで補完する。

Dataset manifest v1.1 separates `frames/person_masks`（white=person）from
`frames/colmap_masks`（white=retained）。Frame analysis reads and inverts only
the semantic person mask; COLMAP and reconstruction keep masks remain separate.
`config/person_mask_report.json` records the complete generation
configuration, pinned model revisions, quality results, and per-frame decisions.
Runtime publication normally hardlinks immutable canonical files;
copy fallback is allowed only when the filesystem cannot create hardlinks.
公開時はcanonicalの通常fileからwrite bitを外し、runtime側で同じinodeを共有する。
削除は一方のdirectory entryだけを対象とするため、もう一方の実体は維持される。

The Viewer consumes only a validated runtime copy derived from this package.
The Viewer still reads only display assets, while packaged backend applications
read the v1.3 dataset directly. Older v1.0-v1.2 packages remain readable for
their existing capabilities but are not self-contained datasets.

## Scene Package format

`scene_manifest.json`は生成側（3D再構成／Python処理）、3DGSデータ登録、3Dシーン閲覧の境界である。全座標はメートルworld、up = `+Y` とする（[座標系](coordinate_system.md)）。

## 1. 保存先

|役割|パス|
|---|---|
|旧正本（v1.0 / 移行前runner、読み取り専用互換）|`data/scene_repository/packages/scenes/<scene_id>/`|
|正本（v1.2 / v1.3、3DGSデータ登録）|`data/scene_repository/packages/scenes/<scene_package_id>/`（manifestにproject/scene/package ID）|
|旧Viewer公開コピー（v1.0、読み取り専用互換）|`web_viewer/app/public/scene_package/`|
|Viewer・各package app公開コピー（v1.1-v1.3）|`web_viewer/app/public/scene_package/runtime/<scene_package_id>/`|
|Git管理する契約例|`sample_scene_package/`|

旧階層は既存成果物を読むためだけに残る。現行の作成・更新・Viewer入力は常にフラットな`scene_package_id`階層であり、旧公開コピーを生成元や更新元にしない。**正本からViewer公開先への一方向公開**であり、逆方向の書き戻しを行わない。

publishはトップレベルrunnerがコピーをstagingし、全URIとhash / sizeを検証してから原子的に行う。大容量assetはGit管理外とする。

## 2. Version

|schema_version|schema|位置づけ|
|---|---|---|
|`1.0.0`|`scene_spec/schemas/scene_manifest.schema.json`|Metric Scene v1 / Surface Mesh MVPの既存互換format。**変更しない**|
|`1.1.0`|`scene_spec/schemas/scene_manifest_v1_1.schema.json`|外部3DGS importとMRP関連付けを持つ二アプリformat|
|`1.2.0`|`scene_spec/schemas/scene_manifest_v1_2.schema.json`|不変ID、ROS/Viewer座標契約、Package所有の動的freshnessを持つ従来format|
|`1.3.0`|`scene_spec/schemas/scene_manifest_v1_3.schema.json`|フレーム、マスク、COLMAP、Field Memoryを内蔵する自己完結format（現行）|

## 3. 検証済みpackageの構成

verifiedなpackageは次を持つ。

- Gaussian asset（splat）
- `config/world_alignment.json` とcamera設定
- annotations
- `dataset/manifest.json` と、そこから辿れる登録フレーム、マスク、COLMAP、Field Memory snapshot（v1.3）
- Proxy gateを通過した場合のみ、Proxy GLB、`proxy_metadata.json`、`proxy_quality.json` への参照

Proxy品質gateが不合格なら `assets.proxy_mesh` を `null` にし、Gaussian assetの経路は使用可能なまま残す。

## 4. v1.1の追加責任

`scene_manifest.json` は `reconstruction_id`、MRP manifest URI / hash、Gaussian source / runtime format、asset SHA-256、coordinate stage、capabilities、provenanceを持つ。

asset metadata、品質指標、world alignmentの**内容**は所有する個別JSONに一度だけ保持し、manifestはURI / hashで参照する。同じ値を2箇所に持たない。

### capability gate

MRP / asset / Proxyのいずれかが未検証なら `display` だけをtrueにできる。`measurement`、`surface_placement`、`fov`、`occlusion` はfalseとし、proxy URIをnullにする。Proxy実体を残さず3DGS-onlyを継続する。

### 品質情報の所有

|JSON|所有|
|---|---|
|`dense_quality.json`|Dense点群の指標|
|`proxy_quality.json`|Surface Mesh / GLB / Viewerの指標|
|`pipeline_report.json`|実行履歴のみ。品質reportを複製せず、品質状態の代替にもしない|

## 5. 動的追跡annotation（任意）

AprilTagで追跡する動的objectを扱うPackageは、`annotations` に次を追加できる。いずれも**静的SPZ / Proxy Viewerには不要**である。

### 5.1 `fixed_tag_map`

`fixed_tag_map.schema.json` 準拠。固定タグframeから `gs_world` へのmeter姿勢（`translation_m` + `rotation_xyzw`）を所有する。**world整合の唯一の所有者**であり、動的追跡を実運用する唯一の必須annotationである。

未知の固定タグframeは常にfail-closedで拒否する。

### 5.2 `dynamic_objects`

v1.2では `dynamic_objects_v1_2.schema.json`、v1.0／v1.1の読取互換では
`dynamic_objects.schema.json` に準拠する。schemaのtitleどおり **「任意のoverride」であり、追跡可能な箱の許可リストではない**。

各objectの必須fieldは `id`（`box_<正の整数>`）だけである。指定しないfieldはViewerの既定値を使う。

|既定値| |
|---|---|
|表示|120サイズcube `[0.5, 0.4, 0.3] m`|
|render offset|identity|
|freshness timeout|Packageの`dynamic_overlay.freshness.timeout_ms`（v1.2の各Package必須）|
|translation jump上限|0.5 m|
|rotation jump上限|45 deg|

v1.2で上書きできるのはcube寸法・色・GLB（URI / SHA-256）・render offset・jump制限だけである。object単位の`timeout_ms`は契約違反として拒否し、freshnessの正本は常にPackageの`dynamic_overlay.freshness.timeout_ms`である。`box_tag_frame` は記録用で省略可能とし、`fixed_tag_map` に存在するframeを指定した場合は契約エラーとする。v1.0／v1.1の既存annotationにある`timeout_ms`はread-only互換情報であり、v1.2 Packageへは持ち込まない。

### 5.3 組合せの意味

|`fixed_tag_map`|`dynamic_objects`|Viewerの動作|
|---|---|---|
|あり|なし|**通常構成。** 受信した任意の有効な `box_<N>` を既定の120サイズcubeで追跡する|
|あり|あり|同上に加え、同じ `id` を持つ箱だけ見た目・jump閾値を上書きする|
|なし|あり|world整合が無いため動的接続を待機する。Viewerで仮想固定タグをruntime登録すると開始できる（runtime-onlyであり、実験シミュレータの開始条件には使えない）|
|なし|なし|動的追跡を行わない。静的Viewerとして正常|

Viewerは受信した有効IDをその場で生成して個別に追跡する。**Scene Packageに事前登録されていないIDも表示される**（runtime上限64個は物理的な箱の登録数ではなくresource boundである）。

動的モデルやannotationが不正でも、静的SPZの表示と既存quality gateを巻き戻さない。ROS接続URLと受信履歴はScene Packageへ保存しない。

## 6. validation

`generation_pipeline/tools/validate_scene_package.py` がv1.0-v1.3を読む。

v1.3ではdataset identity、file count、COLMAP/Field Memory SQLiteの
`quick_check`、database hash、content manifestも検証する。中央の
`var/spatial/workspaces`や外部MRPが存在しなくても、
package内datasetだけから登録フレーム解析を開始できなければならない。

v1.1では MRP linkage、asset hash、coordinate stage、capabilities、world alignmentを検証する。次はfail-closedとする。

- unknown schema
- hash mismatch
- MRPのscene ID / reconstruction ID不一致
- unverified world alignment

`fixed_tag_map` と `dynamic_objects` は、存在する場合にそれぞれ独立に検証する。片方だけの指定はvalidatorのエラーではない（意味は 5.3 のとおり）。

## 関連文書

- [座標系](coordinate_system.md) / [カメラモデル](camera_model.md) / [スケール合わせ](scale_alignment.md)
- [Metric Reconstruction Package](metric_reconstruction_package.md)
- [動的追跡の実装](../web_viewer/app/src/dynamic/DynamicTrackingContract.ts)
- [AprilTagとViewerの連携実装](../web_viewer/app/src/preview/AprilTagViewerBridge.ts)
