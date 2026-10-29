# コード構成

この文書は、DXPO Viewer + AprilTag確認の配布設定で使うコードを説明します。シーン生成やデータ管理のコードも含まれますが、この配布版の画面・APIでは有効にしていません。

## 起動と画面

1. [docker-compose.yml](../docker-compose.yml) は `app`（Vite）と `application-api`（FastAPI）を起動します。ブラウザーからは既定で `localhost:8080` に接続します。
2. [vite.config.ts](../web_viewer/app/vite.config.ts) が画面の種類を切り替え、`/api` をAPIへ転送します。制御トークンはサーバー側のプロキシが付与します。
3. [composed.ts](../web_viewer/app/src/composed.ts) が配布版の入口です。URLに `scene_package` があればViewerを、それ以外はアプリ選択画面を読み込みます。
4. [ApplicationPreview.ts](../web_viewer/app/src/preview/ApplicationPreview.ts) が画面の切り替えとシーン一覧を担当します。APIの有効機能を確認し、この配布版では管理操作を出さず閲覧画面を表示します。

## 3Dシーン表示

- [PreviewApiClient.ts](../web_viewer/app/src/preview/PreviewApiClient.ts) はシーン一覧を取得します。この配布版では `public/scene_package/distribution_index.json` の一覧を使います。
- [ViewerApplication.ts](../web_viewer/app/src/ViewerApplication.ts) が多機能Viewerの表示と操作を組み立てます。[SceneLoader.ts](../web_viewer/app/src/scene/SceneLoader.ts) はScene Packageのマニフェストを読み、参照される資産のURLを解決します。
- [GaussianSplatRuntime.ts](../web_viewer/app/src/renderer/GaussianSplatRuntime.ts) がSparkを使って3DGSを描画します。視点操作は `src/camera/`、座標とシーンの型は `src/scene/` にあります。
- シーン本体はGit管理外です。配置方法は [SETUP.md](../SETUP.md)、形式と座標系は [scene_spec](../scene_spec/scene_package_format.md) を参照してください。

## AprilTagカメラ確認

1. [AprilTagLiveCheck.ts](../web_viewer/app/src/preview/AprilTagLiveCheck.ts) がブラウザーのカメラ映像と検出結果を表示します。
2. [PreviewApiClient.ts](../web_viewer/app/src/preview/PreviewApiClient.ts) が映像フレームとカメラ設定を `PUT /api/apriltag-detect` へ送ります。
3. [application.py](../services/control_api/application.py) が入力のサイズ・形式と機能権限を確認し、[apriltag_live_detection.py](../generation_pipeline/tools/apriltag_live_detection.py) が検出とカメラ基準の姿勢推定を行います。フレームは処理後に破棄されます。
4. 動的なViewer連携を使う場合は、[AprilTagViewerBridge.ts](../web_viewer/app/src/preview/AprilTagViewerBridge.ts) と外部rosbridgeを使います。rosbridge自体はこの配布版に含まれません。

## APIと生成処理

FastAPIの入口は [services/control_api/__main__.py](../services/control_api/__main__.py)、HTTP処理は [application.py](../services/control_api/application.py)、共通APIの振り分けは [routes.py](../services/control_api/routes.py) です。[features.py](../services/control_api/features.py) が利用可能なAPIを制限します。この配布版の設定は `scene-reader,apriltag-check` です。

`generation_pipeline/` には動画からの再構成やScene Package生成に使う共通コードもあります。主な処理は [tools/README.md](../generation_pipeline/tools/README.md) を参照してください。これらの生成画面やジョブは、この配布設定では有効ではありません。
