# セットアップと運用

## 必要な環境

- Docker EngineとDocker Compose v2以降。WindowsではWSL 2上のDocker、またはWSL連携を有効にしたDocker Desktopを使用します。
- Git LFS。クローン時に同梱シーンのSPZを取得するために使用します。
- Webブラウザー。AprilTagのカメラ確認にはlocalhostまたはHTTPSでのカメラ権限が必要です。
- 初回はDockerイメージのビルドに時間と空き容量が必要です。静的なシーン表示とAprilTag確認にGPUは必須ではありません。

## Gitからのシーン取得

シーンのSPZはGit LFSで管理し、マニフェストや座標設定などの関連ファイルは通常のGitで管理しています。クローン前にGit LFSをインストールし、`git lfs install` を実行してください。WSLでGitを使う場合は、WSL側にもGit LFSが必要です。クローン後、Viewerを含むブランチをチェックアウトしたリポジトリのルートで、次のコマンドを実行します。

```bash
git lfs pull
```

Git LFSを入れずにクローンした場合も、インストール後に上記を実行してSPZを取得できます。対象のシーンは次の場所にあります。

```text
web_viewer/app/public/scene_package/runtime/20260817_DXPO-fixed-rig-spzv4-495a985dfdf1/
```

Node.jsがあれば、このアプリのフォルダーで `node web_viewer/app/scripts/scene_package_smoke.mjs` を実行すると、構成とSPZの取得・整合性を確認できます。LFSポインターだけがある場合はシーンを表示できません。

## 起動

配布フォルダーのルートで実行します。

```bash
bash scripts/start_distribution.sh
```

Windows PowerShellからWSLを使う場合は、`/path/to/package` を実際のWSL内パスに置き換えます。

```powershell
wsl.exe -d Ubuntu-24.04 --cd /path/to/package -- bash scripts/start_distribution.sh
```

起動後に [http://localhost:8080/](http://localhost:8080/) を開きます。シーンを配置済みの場合は次のURLから直接開けます。

[DXPOシーンを開く](http://localhost:8080/?scene_package=20260817_DXPO-fixed-rig-spzv4-495a985dfdf1)

APIはDockerネットワーク内で動作し、ホストの8788番には公開しません。8080番が使用中の場合は、WSL内で `APP_PORT=8081 bash scripts/start_distribution.sh` のように変更します。初回起動時、制御トークンとポート等が `.env` に保存されます。このファイルは共有しないでください。

配布版にはrosbridgeを同梱していません。AprilTagのカメラ確認と静的な3D表示は利用できます。AprilTagを使った動的なViewer連携には、別途rosbridgeを起動して画面にWebSocket URLを指定します。

## 停止と再開

同じフォルダーで実行します。

```bash
docker compose stop
docker compose start
```

コンテナーを削除する場合は `docker compose down` を実行します。削除後もイメージと `.env` が残っていれば `docker compose up -d --no-build` で再開できます。

コンテナーには自動再起動を設定しています。WSLやDockerが再起動した場合、Dockerの起動後にアプリも再開されます。WSL自体が停止している間はアプリに接続できないため、必要に応じてUbuntuを開いてDockerを起動してください。手動で `docker compose stop` した場合は `docker compose start` で再開します。

## データとバックアップ

Scene Packageの配置先は `web_viewer/app/public/scene_package/runtime/` です。選択済みDXPOシーンは読み取り専用の表示資産として同梱されます。解析DBと添付画像は `state/site-records/`、導入済みモデルは `models/` に保存されます。これらはGit管理対象外です。必要に応じて運用データのバックアップを保管してください。

状態確認・バックアップ・旧版からの移行には次のツールを使います。

```bash
python3 scripts/manage_distribution_overlay.py status
python3 scripts/manage_distribution_overlay.py backup
python3 scripts/transfer_distribution_state.py --from /path/to/old-distribution --to .
```

復元はアプリを停止してから `python3 scripts/manage_distribution_overlay.py restore --input backups/<backup>.zip` を実行します。復元前の現状も自動バックアップされます。

## トラブルシューティング

- Dockerに接続できない場合は、WSL内で `docker version` と `docker compose version` を確認します。
- 画面が開かない場合は `docker compose ps` で両サービスの状態を確認し、`docker compose logs app application-api` でログを確認します。
- カメラを利用できない場合はブラウザーの権限とlocalhostまたはHTTPSで開いていることを確認します。
- サポート用の診断ZIPは `python3 scripts/create_diagnostic_bundle.py` で作成します。ログを含める場合は `--include-logs` を指定します。
