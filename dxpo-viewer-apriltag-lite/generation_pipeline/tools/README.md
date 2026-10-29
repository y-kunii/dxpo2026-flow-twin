# Generation Pipeline Tools

このディレクトリには動画からの再構成、AprilTagによる実寸合わせ、Scene Packageの検証などを支える処理があります。DXPO Viewer + AprilTag確認の配布版では、ライブAprilTag検出を除き、生成画面や生成ジョブは有効にしていません。配布版の起動方法は [SETUP.md](../../SETUP.md)、画面とAPIの流れは [コード構成](../../docs/architecture.md) を参照してください。

## 主な処理

| ファイル | 役割 |
| --- | --- |
| `apriltag_live_detection.py` | カメラフレームからAprilTagと姿勢を検出 |
| `render_virtual_rig.py` | 360度パノラマから仮想カメラ画像を生成 |
| `detect_apriltags.py` | 再構成用画像内のAprilTagを検出 |
| `colmap_text_model.py`、`match_apriltag_detections_to_colmap.py` | COLMAPのカメラ姿勢と検出画像を対応付け |
| `triangulate_apriltag_corners.py`、`estimate_world_alignment.py` | タグ四隅の三角測量と実寸・座標合わせ |
| `evaluate_dense_geometry.py`、`generate_proxy_geometry.py` | 点群の品質評価と表示・測定用Proxyの生成 |
| `validate_scene_package.py` | Scene Packageの構造・資産の検証 |

これらのツールは同じ処理段階ではありません。実寸付きの再構成成果物と、3DGS表示用Scene Packageの形式はそれぞれ [Metric Reconstruction Package](../../scene_spec/metric_reconstruction_package.md) と [Scene Package](../../scene_spec/scene_package_format.md) を参照してください。

## APIとの境界

`application_preview_api.py` の `PreviewService` は共通処理への入口です。責務ごとの実装は `application_services/` に分かれており、`project_workspace.py` がライブAprilTag検出を呼びます。HTTP・認証・機能制限は `services/control_api/` が担当します。生成ジョブや取り込みを利用するには、この配布版とは別の機能設定・実行環境が必要です。

`legacy_360_pipeline/` と `external_assets/` は旧実験の参照用で、配布版の実行経路には含みません。
