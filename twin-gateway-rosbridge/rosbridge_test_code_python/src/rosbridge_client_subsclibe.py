import os, json
import websocket  # pip install websocket-client
import rel  # websocket-client が内部で利用できる軽量イベントループ

ROSBRIDGE_URL = "ws://localhost:9090"
TOPIC = "/simple_topic"
TYPE = "twin_bridge/msg/SimpleMsg"

def on_message(ws, message):
    print("[recv]", message, flush=True)

def on_open(ws):
    sub = {"op": "subscribe", "topic": TOPIC, "type": TYPE}
    ws.send(json.dumps(sub))
    print("[send] subscribe", sub, flush=True)

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
    rel.signal(2, rel.abort)
    rel.signal(15, rel.abort)
    rel.dispatch()


if __name__ == "__main__":
    main()
