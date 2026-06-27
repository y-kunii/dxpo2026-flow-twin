# python Websocketのテストスクリプト

## セットアップ
"rosbridge_test_code_python"ディレクトリ上で

簡易起動（閉じるとコンテナを削除します）
```
# ビルド
docker build -t python-websocket:ws .

# コンテナ作成と起動
docker run --rm -it --name rosbridge-python-client \
  --network host \
  -v $(pwd)/src:/app/src \
  python-websocket:ws
```

コンテナを残す場合
```
docker build -t python-websocket:ws .
docker run -it --name rosbridge-python-client \
  --network host \
  -v $(pwd)/src:/app/src \
  python-websocket:ws
```

## Python起動
root@LAPTOP-M0P6N5MI:/app/src# 
```
# Publish用
python rosbridge_client_publish.py
```

```
#Subsclibe用
python rosbridge_client_subsclibe.py
```