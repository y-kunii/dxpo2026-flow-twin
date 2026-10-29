# Metric Reconstruction Package

Metric Reconstruction Package（MRP）は、`3D再構成（COLMAP）`が出力し、`3DGSデータ登録`が外部3DGS assetと関連付けるためのversioned contractである。3DGS assetそのものは含めない。

## 正本と構成

正本は `data/scene_repository/packages/metric_reconstructions/<scene_id>/<reconstruction_id>/` とする。大容量image/maskは重複コピーせず、package内またはworkspace内の相対参照をmanifestから一意に辿れるようにする。

## 検証レベル

MRPの通常操作で再構成workspace全体を再hashしてはいけない。検証は次の3レベルに分離する。

- `none`: MRP内のmanifest、quality、座標設定と記録済みhashの整合だけを検証する。管理一覧・ジョブ完了確認で使う。
- `presence`: 上記に加え、画像・mask・COLMAP・LFS参照先の存在、ファイル名集合、件数、必須ファイルを確認する。SPZ登録前に使う。画像decodeや全バイトhashは行わない。
- `hash`: 参照先の全ファイルを再hashし、MRP作成時のhashと比較する。MRP生成・明示的なオフライン監査だけで使う。

```bash
# 日常のPackage contract検証
python3 generation_pipeline/tools/validate_metric_reconstruction_package.py \
  --package data/scene_repository/packages/metric_reconstructions/<scene_id>/<reconstruction_id>

# 明示的な全量監査
python3 generation_pipeline/tools/validate_metric_reconstruction_package.py \
  --package data/scene_repository/packages/metric_reconstructions/<scene_id>/<reconstruction_id> \
  --source-root . \
  --source-verification hash
```

`source-root`だけを指定する旧CLIは互換のため`hash`として扱う。アプリの通常APIは`hash`を呼ばない。

```text
manifest.json
images/
masks/
rig_config.json
colmap/sparse/
colmap/text/
lfs_input/
config/world_alignment.json
quality/input_quality.json
quality/mask_quality.json
quality/sfm_quality.json
quality/metric_quality.json
camera_trajectory.json
```

schemaは `scene_spec/schemas/metric_reconstruction_manifest.schema.json`、versionは `1.0.0` である。

## manifestの責任

`manifest.json` は次だけを所有する。

- `scene_id`、`reconstruction_id`
- input video/config hash、image/mask set ID
- image/mask/SfM/world alignment/camera trajectory/quality/LichtFeld inputへの参照
- 各参照の必要なhash、coordinate stage、unit、up axis、created_at、tool versions

品質指標はmanifestに複製しない。`quality/*.json` がそれぞれの工程の唯一の品質結果を所有する。Dense/Proxy geometryについては `dense_quality.json` と `proxy_quality.json` が唯一のgeometry品質状態である。

## validation

`generation_pipeline/tools/validate_metric_reconstruction_package.py` は、schema version、relative URI、必須asset、hash、quality status、meter/+Y座標を検証する。未知schema、hash不一致、quality不合格、参照欠落はfail-closedとする。
