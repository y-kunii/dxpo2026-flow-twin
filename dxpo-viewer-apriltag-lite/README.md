# DXPO 3DGS Viewer + AprilTag確認

DXPOの3Dシーンを表示し、カメラ映像でAprilTagを確認する内部評価用アプリケーションです。このフォルダーは元の開発リポジトリではなく、検証用に切り出された配布パッケージです。公開・再配布については [INTERNAL_EVALUATION_ONLY.md](INTERNAL_EVALUATION_ONLY.md) を参照してください。

## はじめに

Docker Composeで起動し、ブラウザーから `http://localhost:8080/` にアクセスします。WindowsではWSL内から実行します。必要条件、起動・停止、データの扱いは [SETUP.md](SETUP.md) にまとめています。

- 同梱シーン: `20260817_DXPO-fixed-rig-spzv4-495a985dfdf1`（SPZ本体はGit LFSで管理）
- 使用する画面: 多機能3DGS Viewer、AprilTagカメラ確認
- 静的表示とAprilTag確認にGPUやrosbridgeは不要。動的なViewer連携には外部rosbridgeが必要

## 主な構成

| ディレクトリ | 内容 |
| --- | --- |
| `web_viewer/app/` | TypeScript / Viteによる画面と3D Viewer |
| `services/control_api/` | FastAPIによるシーン参照・AprilTag等のAPI |
| `generation_pipeline/` | 動画からの再構成やシーン生成に使う共通処理 |
| `scene_spec/` | シーン形式、座標系、カメラモデルの仕様 |
| `scripts/` | 起動、検証、バックアップ等の運用ツール |
| `web_viewer/app/public/scene_package/runtime/` | 選択済みシーン。SPZはGit LFS、関連設定は通常のGitで管理 |
| `data/`, `state/` | 運用データ。Git管理対象外 |

この配布版では [docker-compose.yml](docker-compose.yml) がAPI機能を `scene-reader` と `apriltag-check` に限定します。共通ソースや一部の内部READMEには、この配布版に含まれない機能や元リポジトリ向けの起動方法も記載されています。

画面、API、シーン読み込み、AprilTag検出のつながりは [コード構成](docs/architecture.md) を参照してください。

## 開発時の確認

Node.js 20.19以降がある環境で、画面の検査とビルドを実行できます。

```bash
cd web_viewer/app
npm ci
npm run lint
npm run build:app
npm run test:smoke
```

`npm ci` は `node_modules` を作り直します。`test:smoke` はシーン配置後に実行してください。画面遷移の検査は次のコマンドで実行できます。読み取り専用のシーン一覧をテスト中に更新しない設定です。

```bash
npx playwright install chromium --only-shell
SCENE_PACKAGE_INDEX_READ_ONLY=1 npx playwright test tests/e2e/scene_capabilities.spec.ts
```

実際の3D表示やカメラ確認にはDockerでアプリを起動してください。

## Gitとデータ

環境設定、認証情報、運用データ、モデル、生成物と、同梱対象以外のシーンは [.gitignore](.gitignore) で除外しています。対象のDXPOシーンは例外として管理しており、取得にはGit LFSが必要です。手順は [SETUP.md](SETUP.md) に記載しています。

誤追加を避けるため、画像・動画・表計算・PDF・ログ・圧縮ファイルも原則として除外しています。共有用のサンプルが必要な場合は内容を確認したうえで個別に追加してください。`.gitignore` は既に追跡済みのファイルやコード中の秘密情報を保護せず、強制追加でも回避できます。公開前に `git status --short`、`git diff --cached --name-only` とコミット内容を確認してください。

`app_package_manifest.json` と `checksums.sha256` は元の配布物の記録です。編集後の作業ツリーに対する最新の来歴やチェックサムではありません。配布元を検証するときは、変更前の配布ZIPを使ってください。
