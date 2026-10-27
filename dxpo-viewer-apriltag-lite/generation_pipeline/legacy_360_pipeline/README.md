# Legacy 360 Pipeline Assets

> **Archive only.** The files below are reference material from the old
> 3DGS experiment. They are not runtime dependencies and
> must not be invoked by the workspace pipeline or web applications.

このディレクトリは、旧実験プロジェクトから再利用候補を隔離して整理するための領域である。

## 方針

旧プロジェクトをそのまま本番パイプラインへ取り込まない。

- 実験用スクリプトは `reference/` に隔離する。
- 再利用候補の小さな処理単位は `candidate_tools/` に置く。
- 正式採用する場合は、改めて `generation_pipeline/tools/` へ移植する。
- `.venv`, `__pycache__`, `.bak`, 生成済みデータ、COLMAP本体、動画、画像、3DGSモデルは取り込まない。

## 取り込んだ資産

- `candidate_tools/render_pano_rig.py`
  - 360度正距円筒パノラマから、COLMAP用の複数ピンホール仮想カメラ画像を生成する候補スクリプト。
  - 90度FOV、pitch 3段、yaw 4方向、合計12仮想カメラ構成。

- `candidate_tools/level_colmap_by_camera_plane.py`
  - COLMAP TXT model の camera center 群から平面を推定し、指定up axisへ回転補正する候補スクリプト。
  - スケール合わせではない。AprilTagによる実寸合わせの代替ではない。

- `reference/Run-360RigSfM.ps1`
  - 旧プロジェクトの統合ランナー。
  - Windows / PowerShell / ローカルvenv前提のため、本リポジトリでは参照用とする。

- `reference/tool_config.example.windows.json`
  - 旧Windows環境の設定例。
  - 仮のパスを含むため、本番設定としては使わない。

## 再設計後の目標

360動画から3DGSを作る処理は、今後以下のように分割する。

1. 360動画からパノラマフレームを抽出する。
2. パノラマから仮想ピンホール画像を生成する。
3. COLMAP fixed-rig SfM を実行する。
4. COLMAPに投入した仮想画像上でAprilTagを検出する。
5. COLMAP姿勢を使ってAprilTag cornerを三角測量する。
6. 実寸タグサイズからスケールを推定する。
7. `world_alignment.json` を出力する。
8. 3DGS生成・変換後、`scene_package` にまとめる。

## AprilTag処理の挿入位置

AprilTagは元の360パノラマではなく、COLMAPに投入した仮想ピンホール画像側で検出する。

理由:

- COLMAPも仮想ピンホール画像を使う。
- camera intrinsics が `render_manifest.json` / `rig_config.json` と対応する。
- AprilTag corner の pixel coordinate と COLMAP image name を対応させやすい。
- equirectangular歪みを直接扱わずに済む。
