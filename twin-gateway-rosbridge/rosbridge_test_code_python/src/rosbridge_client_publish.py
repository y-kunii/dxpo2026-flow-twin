import os, json, time
import websocket  # pip install websocket-client
import rel  # イベントループ（websocket-client 内部互換）

ROSBRIDGE_URL = "ws://localhost:9090"
TOPIC = "/simple_topic"
TYPE = "twin_bridge/msg/SimpleMsg"

INTERVAL = 0.2  # 送信間隔（秒）

def on_message(ws, message):
    print("[recv]", message, flush=True)

def send_periodic(ws):
    msg = {"id": 1, "text": "hello twin"}
    pub = {"op": "publish", "topic": TOPIC, "type": TYPE, "msg": msg}
    ws.send(json.dumps(pub))
    print("[send]", pub, flush=True)
    rel.timeout(INTERVAL, send_periodic, ws)

def on_open(ws):
    print("[open] connection established", flush=True)
    rel.timeout(0, send_periodic, ws)

def main():
    url = ROSBRIDGE_URL
    if not (url.startswith("ws://") or url.startswith("wss://")):
        url = "ws://" + url
    print(f"[client] connecting to {url} ...", flush=True)

    ws = websocket.WebSocketApp(
        url,
        on_message=on_message,
        on_open=on_open,
    )

    ws.run_forever(dispatcher=rel)
    rel.signal(2, rel.abort)   # Ctrl+C
    rel.signal(15, rel.abort)  # SIGTERM
    rel.dispatch()

if __name__ == "__main__":
    main()
