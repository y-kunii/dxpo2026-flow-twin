#!/usr/bin/env python3
"""Open approved repository folders in Windows Explorer from Docker APIs.

The bridge listens on a repository-local Unix socket.  It runs in the WSL
host namespace where ``explorer.exe`` is available; container services can
only request an existing directory below the project-workspace boundary.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SOCKET_URI = "var/spatial/locks/open-folder-bridge.sock"
PID_URI = "var/spatial/locks/open-folder-bridge.pid"
LOG_URI = "var/spatial/logs/open-folder-bridge.log"
MAX_REQUEST_BYTES = 8 * 1024


def _paths(root: Path) -> tuple[Path, Path, Path]:
    return root / SOCKET_URI, root / PID_URI, root / LOG_URI


def request(socket_path: Path, payload: dict[str, Any], timeout: float = 2.0) -> dict[str, Any]:
    encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ValueError("open_folder_bridge_request_too_large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(socket_path))
        connection.sendall(encoded)
        response = bytearray()
        while not response.endswith(b"\n"):
            chunk = connection.recv(MAX_REQUEST_BYTES + 1 - len(response))
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > MAX_REQUEST_BYTES:
                raise ValueError("open_folder_bridge_response_too_large")
    value = json.loads(response.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("open_folder_bridge_response_invalid")
    return value


def _approved_target(root: Path, value: Any) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("open_folder_path_required")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("open_folder_path_must_be_repository_relative")
    target = (root / relative).resolve()
    workspace_root = (root / "var/spatial/workspaces/projects").resolve()
    if workspace_root not in target.parents or not target.is_dir():
        raise ValueError("open_folder_path_not_approved")
    return target


def _handle(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    command = payload.get("command")
    if command == "ping":
        return {"status": "verified", "service": "open-folder-bridge"}
    if command != "open":
        raise ValueError("open_folder_command_invalid")
    target = _approved_target(root, payload.get("path"))
    converted = subprocess.run(
        ["wslpath", "-w", str(target)],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.strip()
    if not converted.startswith("\\\\"):
        raise ValueError("open_folder_windows_path_invalid")
    executable = Path("/mnt/c/Windows/explorer.exe")
    if not executable.is_file():
        raise FileNotFoundError("explorer_exe_not_available")
    subprocess.Popen(
        [str(executable), converted],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {"status": "verified", "opened": True, "path": converted}


def serve(root: Path) -> int:
    socket_path, pid_path, _ = _paths(root)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.unlink(missing_ok=True)
    stop = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        server.listen(4)
        server.settimeout(1.0)
        pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
        try:
            while not stop:
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    continue
                with connection:
                    try:
                        raw = bytearray()
                        while not raw.endswith(b"\n"):
                            chunk = connection.recv(MAX_REQUEST_BYTES + 1 - len(raw))
                            if not chunk:
                                break
                            raw.extend(chunk)
                            if len(raw) > MAX_REQUEST_BYTES:
                                raise ValueError("open_folder_bridge_request_too_large")
                        payload = json.loads(raw.decode("utf-8"))
                        if not isinstance(payload, dict):
                            raise ValueError("open_folder_bridge_payload_invalid")
                        response = _handle(root, payload)
                    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as error:
                        response = {"status": "failed", "opened": False, "failure_reason": str(error)}
                    connection.sendall((json.dumps(response) + "\n").encode("utf-8"))
        finally:
            socket_path.unlink(missing_ok=True)
            pid_path.unlink(missing_ok=True)
    return 0


def ensure(root: Path) -> int:
    socket_path, _, log_path = _paths(root)
    try:
        if request(socket_path, {"command": "ping"}).get("status") == "verified":
            print("Open-folder bridge: ready")
            return 0
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "serve", "--repo-root", str(root)],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            if request(socket_path, {"command": "ping"}, timeout=0.5).get("status") == "verified":
                print("Open-folder bridge: started")
                return 0
        except (OSError, ValueError, json.JSONDecodeError):
            time.sleep(0.1)
    raise RuntimeError("open_folder_bridge_start_timeout")


def stop(root: Path) -> int:
    socket_path, pid_path, _ = _paths(root)
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
        if b"open_folder_bridge.py" not in command:
            raise ValueError("open_folder_bridge_pid_mismatch")
        os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError):
        socket_path.unlink(missing_ok=True)
        pid_path.unlink(missing_ok=True)
        return 0
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and socket_path.exists():
        time.sleep(0.1)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("ensure", "serve", "stop"))
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    args = parser.parse_args()
    root = args.repo_root.resolve()
    if args.command == "serve":
        return serve(root)
    if args.command == "stop":
        return stop(root)
    return ensure(root)


if __name__ == "__main__":
    raise SystemExit(main())
