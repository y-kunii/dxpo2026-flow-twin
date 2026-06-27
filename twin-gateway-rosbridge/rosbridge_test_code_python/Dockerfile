# ============================================
# Python WebSocket Client for Rosbridge
# - /src をコンテナにコピーして /app/src に配置
# - websocket-client を利用
# ============================================
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 LC_ALL=C.UTF-8

WORKDIR /app

# 必要最小限のランタイム
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

# Python ライブラリ
RUN pip install --no-cache-dir\
    websocket-client==1.8.0\
    rel

# --- ホストの src ディレクトリをコンテナにコピー ---
COPY src/ /app/src/

# Entrypoint スクリプト（任意で）
COPY entorypoint.sh /app/entorypoint.sh
RUN chmod +x /app/entorypoint.sh

# デフォルト作業ディレクトリ
WORKDIR /app/src

# デフォルトの起動コマンド（後で -e MODE=pub など渡して上書き可能）
CMD ["bash"]
