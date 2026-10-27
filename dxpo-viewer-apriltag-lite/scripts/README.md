# 運用スクリプト

DXPO Viewer + AprilTag確認の配布版で利用する入口です。実行はこのアプリのルートから行います。

| スクリプト | 役割 |
| --- | --- |
| `start_distribution.sh` | 配布版のDockerサービスを起動 |
| `manage_distribution_overlay.py` | 運用データの状態確認・バックアップ・復元 |
| `transfer_distribution_state.py` | 旧配布版から運用データを移行 |
| `create_diagnostic_bundle.py` | サポート向けの診断情報を作成 |

必要条件、具体的なコマンド、データの保存先は [SETUP.md](../SETUP.md) を参照してください。

このディレクトリには元の共通基盤向けの生成・移行・検証スクリプトも含まれます。これらが含まれていることは、配布版の画面で対応機能が有効であることを意味しません。データを変更するコマンドは対象とバックアップを確認してから実行してください。
