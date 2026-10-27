from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STAGE_STATUSES = {"pending", "running", "verified", "unverified", "failed", "reused", "skipped"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def artifact_record(path: Path, root: Path) -> dict[str, Any]:
    resolved_path = path.resolve()
    lexical_path = path.absolute()
    lexical_root = root.absolute()
    try:
        # Keep repository-relative symlink paths stable.  A job-specific input
        # is a repository symlink to a Windows file, so resolving it before
        # relative_to() would incorrectly make a valid input look external.
        display_path = str(lexical_path.relative_to(lexical_root))
        scope = "repository"
    except ValueError:
        # User-selected inputs may live on a Windows drive mounted into WSL.
        # Keep their absolute path and continue recording size/hash instead of
        # treating a valid external input as a pipeline failure.
        display_path = str(resolved_path)
        scope = "external"
    record: dict[str, Any] = {"path": display_path, "scope": scope}
    if path.is_file():
        record.update({"kind": "file", "size": path.stat().st_size, "sha256": sha256_file(path)})
    elif path.is_dir():
        record.update({"kind": "directory", "file_count": sum(1 for item in path.rglob("*") if item.is_file())})
    else:
        record.update({"kind": "missing"})
    return record


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2) + "\n"
    try:
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        os.close(fd)
        temporary = Path(temporary_name)
    except PermissionError:
        fd, temporary_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp")
        os.close(fd)
        temporary = Path(temporary_name)
        temporary.write_text(payload, encoding="utf-8")
        subprocess.run(["sudo", "-n", "mv", str(temporary), str(path)], check=True)
        subprocess.run(["sudo", "-n", "chmod", "0644", str(path)], check=True)
        return
    try:
        temporary.write_text(payload, encoding="utf-8")
        temporary.chmod(0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class StageRecord:
    stage_id: str
    status: str = "pending"
    started_at: str | None = None
    completed_at: str | None = None
    duration_sec: float | None = None
    input_artifacts: list[dict[str, Any]] = field(default_factory=list)
    output_artifacts: list[dict[str, Any]] = field(default_factory=list)
    command: list[str] | None = None
    tool_version: str | None = None
    config_hash: str | None = None
    input_hashes: dict[str, str] = field(default_factory=dict)
    output_hashes: dict[str, str] = field(default_factory=dict)
    reused: bool = False
    failure_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def start(self) -> None:
        self.status = "running"
        self.started_at = utc_now()

    def finish(self, status: str, *, failure_reasons: list[str] | None = None, warnings: list[str] | None = None) -> None:
        if status not in STAGE_STATUSES:
            raise ValueError(f"invalid stage status: {status}")
        self.status = status
        self.completed_at = utc_now()
        if self.started_at:
            started = datetime.fromisoformat(self.started_at)
            completed = datetime.fromisoformat(self.completed_at)
            self.duration_sec = (completed - started).total_seconds()
        self.failure_reasons = failure_reasons or []
        self.warnings = warnings or []

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_sec": self.duration_sec,
            "input_artifacts": self.input_artifacts,
            "output_artifacts": self.output_artifacts,
            "command": self.command,
            "tool_version": self.tool_version,
            "config_hash": self.config_hash,
            "input_hashes": self.input_hashes,
            "output_hashes": self.output_hashes,
            "reused": self.reused,
            "failure_reasons": self.failure_reasons,
            "warnings": self.warnings,
        }


def run_command(command: list[str], *, cwd: Path, timeout: float | None = None) -> dict[str, Any]:
    started = utc_now()
    completed = subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=timeout)
    return {
        "command": command,
        "started_at": started,
        "completed_at": utc_now(),
        "exit_code": completed.returncode,
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
    }
