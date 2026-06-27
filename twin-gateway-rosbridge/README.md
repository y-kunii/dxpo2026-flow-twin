# ROS2 RosBridge 

## Docker セットアップ

```
# ビルド
docker build -t twin-gateway-rosbridge:humble .
```

```
# 簡易実行（閉じるとコンテナが削除される）: ポートマッピングでホストの9090に公開
docker run --rm -it \
  -v "$(pwd)/digital_twin_ws:/workspace/digital_twin_ws" \
  -p 9090:9090 \
  --name twin-gateway-rosbridge \
  twin-gateway-rosbridge:humble
```

```
# 実行（閉じてもコンテナが残る）: ポートマッピングでホストの9090に公開
docker run -it --name twin-gateway-rosbridge \
  -v $(pwd)/digital_twin_ws:/workspace/digital_twin_ws \
  -p 9090:9090 \
  twin-gateway-rosbridge:humble
```

```
# ROS DDS 通信をコンテナ外のノードと安定して使いたい場合
# DDS のマルチキャスト等の関係で --network host を使うことが多い（ローカル開発向け）
docker run -it　--network --name twin-gateway-rosbridge \
  -v $(pwd)/digital_twin_ws:/workspace/digital_twin_ws \
  -p 9090:9090 \
  twin-gateway-rosbridge:humble
```

コンテナを起動（コンテナが作成されている場合）
```
# -ai はそのままターミナルが起動
docker start -ai twin-gateway-rosbridge
```

起動中のコンテナに入って作業する場合
```
docker exec -it twin-gateway-rosbridge bash
```

## コマンドテスト
１．上記でコンテナが起動中であること

２．別のターミナルを立ち上げる
```
docker exec -it twin-gateway-rosbridge bash
```

ROS2コマンド：下記のコマンドを実行（状況に応じてpub/subを変更してください）
```
source /opt/ros/humble/setup.bash
source /workspace/digital_twin_ws/install/setup.bash

# Publishコマンド
ros2 topic pub /simple_topic twin_bridge/msg/SimpleMsg "{id: 1, text: 'hello twin'}"

# Subsclibeコマンド
ros2 topic echo /simple_topic
```

３．さらに別のターミナルを立ち上げ、下記を実施

rosbridge_test_code_python/README.mdに説明あり

"rosbridge_test_code_python"ディレクトリ上で
```
# ビルド
docker build -t python-websocket:ws .

# コンテナ作成と起動
docker run --rm -it --name rosbridge-python-client \
  --network host \
  -v $(pwd)/src:/app/src \
  python-websocket:ws
```

Pythonスクリプト：コマンド（状況に応じてpub/subを変更してください）

```
# Publish用
python rosbridge_client_publish.py
```

```
#Subsclibe用
python rosbridge_client_subsclibe.py
```

以下の通信ができていることを確認
```
root@LAPTOP-M0P6N5MI:/app/src# python rosbridge_client_subsclibe.py
[client] connecting to ws://localhost:9090 ...
[send] subscribe {'op': 'subscribe', 'topic': '/simple_topic', 'type': 'twin_bridge/msg/SimpleMsg'}
[recv] {"op": "publish", "topic": "/simple_topic", "msg": {"id": 1, "text": "hello twin"}}
[recv] {"op": "publish", "topic": "/simple_topic", "msg": {"id": 1, "text": "hello twin"}}
```

---

# ここから下はメモ

## ROS2 パッケージの作成手順
```
source /opt/ros/humble/setup.bash
```

```
ros2 pkg create --build-type ament_python twin_bridge
```



```
cd /workspace/digital_twin_ws
colcon build
source install/setup.bash

# クリーンビルド
cd /workspace/digital_twin_ws
rm -rf build/ install/ log/
colcon build
source install/setup.bash
```

src/twin_bridge/CMakeLists.txtに追加したmsgを加える
```
# メッセージとサービスのインターフェース生成
rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/TwinEvent.msg"
  "srv/TwinCommand.srv"
  DEPENDENCIES std_msgs
)
```

```
source /opt/ros/humble/setup.bash
source /workspace/digital_twin_ws/install/setup.bash

cd /workspace/digital_twin_ws
colcon build
source install/setup.bash


# MSGの確認
ros2 interface list | grep twin_bridge
ros2 interface show twin_bridge/msg/SimpleMsg

```

### コマンドテスト

コンテナターミナルを２つ用意する。

ターミナル起動コマンド（共通）
```
docker exec -it twin-gateway-rosbridge bash
```

ターミナル１ Publish側
```
source /opt/ros/humble/setup.bash
source /workspace/digital_twin_ws/install/setup.bash

# パブリッシュ（送信）
ros2 topic pub /simple_topic twin_bridge/msg/SimpleMsg "{id: 1, text: 'hello twin'}"
```


ターミナル２ Subscribe側
```
source /opt/ros/humble/setup.bash
source /workspace/digital_twin_ws/install/setup.bash

# サブスクライブ（受信）
ros2 topic echo /simple_topic
```


### サービスノード立ち上げ
```
ros2 run twin_bridge command_service
```