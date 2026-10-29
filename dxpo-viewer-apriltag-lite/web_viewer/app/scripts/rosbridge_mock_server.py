#!/usr/bin/env python3
"""Small rosbridge-compatible WebSocket broker for local Viewer tests.

The server deliberately implements only the Viewer contract: a subscription to
``/box/relative_transform`` followed by ``geometry_msgs/TransformStamped``
messages.  It relays simulator ``publish`` messages to Viewer subscribers and
can additionally emit a deterministic 20 Hz trajectory for the pre-integration
acceptance runner.  It is intentionally not a ROS 2 implementation.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import select
import socket
import struct
import sys
import threading
import time

TOPIC = "/box/relative_transform"
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def read_exact(connection: socket.socket, length: int) -> bytes | None:
    """Read exactly ``length`` bytes or return None after a clean close."""
    payload = bytearray()
    while len(payload) < length:
        chunk = connection.recv(length - len(payload))
        if not chunk:
            return None
        payload.extend(chunk)
    return bytes(payload)


def read_frame(connection: socket.socket) -> str | None:
    header = read_exact(connection, 2)
    if header is None:
        return None
    first, second = header
    length = second & 0x7F
    if length == 126:
        extended_length = read_exact(connection, 2)
        if extended_length is None:
            return None
        length = struct.unpack("!H", extended_length)[0]
    elif length == 127:
        extended_length = read_exact(connection, 8)
        if extended_length is None:
            return None
        length = struct.unpack("!Q", extended_length)[0]
    mask = read_exact(connection, 4) if second & 0x80 else b""
    if mask is None:
        return None
    payload = read_exact(connection, length)
    if payload is None:
        return None
    payload = bytearray(payload)
    if mask:
        for index in range(len(payload)):
            payload[index] ^= mask[index % 4]
    if first & 0x0F == 0x8:
        return None
    return payload.decode("utf-8")


def send_text(connection: socket.socket, value: object) -> None:
    payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
    length = len(payload)
    if length < 126:
        header = bytes([0x81, length])
    elif length < 65536:
        header = bytes([0x81, 126]) + struct.pack("!H", length)
    else:
        header = bytes([0x81, 127]) + struct.pack("!Q", length)
    connection.sendall(header + payload)


def transform_message(
    stamp_seconds: float,
    translation: tuple[float, float, float] = (-0.18, -0.12, -0.45),
    rotation_xyzw: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0),
    frame_id: str = "apriltag_tag25h9_0",
) -> dict[str, object]:
    """Return a valid test transform with a small visible lateral motion.

    The source stamp is intentionally independent of the Viewer clock.  The
    Viewer uses it only for ordering, while its monotonic WebSocket receive
    time controls tracking timeout.
    """
    sec = int(stamp_seconds)
    nanosec = int(round((stamp_seconds - sec) * 1_000_000_000))
    if nanosec >= 1_000_000_000:
        sec += 1
        nanosec -= 1_000_000_000
    return {
        "op": "publish",
        "topic": TOPIC,
        "msg": {
            "header": {
                "stamp": {"sec": sec, "nanosec": nanosec},
                "frame_id": "apriltag_tag25h9_0",
            },
            "child_frame_id": "box_01",
            "transform": {
                "translation": {"x": translation[0], "y": translation[1], "z": translation[2]},
                "rotation": {"x": rotation_xyzw[0], "y": rotation_xyzw[1], "z": rotation_xyzw[2], "w": rotation_xyzw[3]},
            },
        },
    }


def accept_websocket(connection: socket.socket) -> bool:
    request = b""
    while b"\r\n\r\n" not in request:
        chunk = connection.recv(4096)
        if not chunk:
            return False
        request += chunk
    headers = request.decode("ascii", errors="replace").split("\r\n")
    try:
        key = next(
            line.split(":", 1)[1].strip()
            for line in headers
            if line.lower().startswith("sec-websocket-key:")
        )
    except (StopIteration, IndexError):
        return False
    accept = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
    connection.sendall((
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
    ).encode("ascii"))
    return True


def virtual_trajectory_pose(elapsed_s: float) -> tuple[tuple[float, float, float], tuple[float, float, float, float]] | None:
    """The deterministic virtual camera-side trajectory at 20 Hz.

    0–2 s stays still, 2–4 s moves +X 10 cm, 4–6 s moves +Z 10 cm,
    6–8 s yaws +90° around +Y, and 8–9 s stays still.  It then publishes
    nothing so the Viewer must transition to lost through its receive timeout.
    """
    x = 0.0
    z = 0.0
    yaw_deg = 0.0
    if elapsed_s < 0:
        return None
    if elapsed_s < 2.0:
        pass
    elif elapsed_s < 4.0:
        x = 0.10 * ((elapsed_s - 2.0) / 2.0)
    elif elapsed_s < 6.0:
        x = 0.10
        z = 0.10 * ((elapsed_s - 4.0) / 2.0)
    elif elapsed_s < 8.0:
        x = 0.10
        z = 0.10
        yaw_deg = 90.0 * ((elapsed_s - 6.0) / 2.0)
    elif elapsed_s < 9.0:
        x = 0.10
        z = 0.10
        yaw_deg = 90.0
    else:
        return None
    half_yaw_rad = math.radians(yaw_deg) / 2.0
    return (x, 0.0, z), (0.0, math.sin(half_yaw_rad), 0.0, math.cos(half_yaw_rad))


class Broker:
    """Thread-safe relay for the one Viewer transform topic."""

    def __init__(self) -> None:
        self._subscribers: set["ClientConnection"] = set()
        self._lock = threading.Lock()

    def subscribe(self, client: "ClientConnection") -> None:
        with self._lock:
            self._subscribers.add(client)

    def unsubscribe(self, client: "ClientConnection") -> None:
        with self._lock:
            self._subscribers.discard(client)

    def publish(self, value: dict[str, object]) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers)
        for client in subscribers:
            if not client.send(value):
                self.unsubscribe(client)


class ClientConnection:
    def __init__(self, connection: socket.socket) -> None:
        self.connection = connection
        self._send_lock = threading.Lock()

    def send(self, value: dict[str, object]) -> bool:
        try:
            with self._send_lock:
                send_text(self.connection, value)
            return True
        except (ConnectionError, OSError):
            return False


def handle_client(
    raw_connection: socket.socket,
    broker: Broker,
    continuous: bool,
    virtual_trajectory: bool,
    source_sequence: list[int],
    sequence_lock: threading.Lock,
) -> None:
    with raw_connection:
        if not accept_websocket(raw_connection):
            return
        connection = ClientConnection(raw_connection)
        subscribed = False
        next_publish_at = 0.0
        trajectory_started_at: float | None = None
        try:
            while True:
                poll_timeout = 0.1
                if subscribed and (continuous or virtual_trajectory):
                    poll_timeout = max(0.0, min(0.05, next_publish_at - time.monotonic()))
                readable, _, _ = select.select([raw_connection], [], [], poll_timeout)
                raw = read_frame(raw_connection) if readable else ""
                if raw is None:
                    break
                if raw:
                    try:
                        message = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if message.get("op") == "subscribe" and message.get("topic") == TOPIC:
                        subscribed = True
                        broker.subscribe(connection)
                        next_publish_at = 0.0
                        trajectory_started_at = time.monotonic()
                    elif message.get("op") == "unsubscribe" and message.get("topic") == TOPIC:
                        subscribed = False
                        broker.unsubscribe(connection)
                    elif message.get("op") == "publish" and message.get("topic") == TOPIC and isinstance(message.get("msg"), dict):
                        broker.publish({"op": "publish", "topic": TOPIC, "msg": message["msg"]})

                if subscribed and time.monotonic() >= next_publish_at:
                    now = time.monotonic()
                    if virtual_trajectory:
                        assert trajectory_started_at is not None
                        pose = virtual_trajectory_pose(now - trajectory_started_at)
                        if pose is None:
                            next_publish_at = now + 0.05
                            continue
                        translation, rotation = pose
                        with sequence_lock:
                            source_stamp = 1_700_000_000.0 + source_sequence[0] * 0.05
                            source_sequence[0] += 1
                        broker.publish(transform_message(source_stamp, translation, rotation))
                        next_publish_at = now + 0.05
                    elif continuous:
                        broker.publish(transform_message(time.time()))
                        next_publish_at = now + 0.1
        except (ConnectionError, OSError):
            pass
        finally:
            broker.unsubscribe(connection)


def serve(port: int, host: str = "127.0.0.1", continuous: bool = False, virtual_trajectory: bool = False) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen()
        print(
            f"rosbridge mock listening on ws://{host}:{port} "
            f"(continuous={continuous}, virtual_trajectory={virtual_trajectory})",
            flush=True,
        )
        broker = Broker()
        source_sequence = [0]
        sequence_lock = threading.Lock()
        while True:
            connection, _ = server.accept()
            threading.Thread(
                target=handle_client,
                args=(connection, broker, continuous, virtual_trajectory, source_sequence, sequence_lock),
                daemon=True,
            ).start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run a small rosbridge-compatible transform publisher.")
    parser.add_argument("port", type=int, nargs="?", default=19090)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--continuous", action="store_true", help="Publish at 10 Hz after a subscription.")
    parser.add_argument("--virtual-trajectory", action="store_true", help="Publish the deterministic 20 Hz virtual tracking trajectory.")
    args = parser.parse_args()
    serve(args.port, args.host, args.continuous, args.virtual_trajectory)
