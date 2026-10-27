#!/usr/bin/env python3
"""Create a support bundle that excludes secrets and canonical scene content."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.distribution_contracts.lifecycle import artifact_inventory
from packages.distribution_contracts.release import application_runtime_contract
from packages.scene_repository import SceneRepositoryLayout


SECRET_PATTERNS = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]{8,}"),
    re.compile(r"(?i)authorization\s*[:=]\s*[^\r\n]+"),
    re.compile(r"(?i)(token|secret|password|cookie)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
)
LEAK_PATTERNS = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]{8,}"),
    re.compile(r"(?i)authorization\s*[:=]\s*(?!\[REDACTED\])[^\r\n]+"),
    re.compile(
        r"(?i)(?:token|secret|password|cookie)\s*[:=]\s*(?!\[REDACTED\])[^\s,;]+"
    ),
    re.compile(
        r'(?i)"(?:token|secret|password|cookie)"\s*:\s*"(?!\[REDACTED\])[^"\r\n]+"'
    ),
)


def _redact(text: str) -> str:
    value = text
    for pattern in SECRET_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    value = re.sub(
        r"(?<![0-9])(?:\d{1,3}\.){3}\d{1,3}(?![0-9])", "[IP_REDACTED]", value
    )
    return value


def _command(argv: list[str]) -> dict[str, Any]:
    try:
        process = subprocess.run(
            argv, capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"command": argv, "status": "unavailable", "error": _redact(str(error))}
    return {
        "command": argv,
        "status": "ok" if process.returncode == 0 else "failed",
        "exit_code": process.returncode,
        "output": _redact((process.stdout + process.stderr)[-12000:]),
    }


def _database_status(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False}
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            allowed = {
                "sites",
                "scene_versions",
                "assets",
                "inspections",
                "inspection_observations",
                "issues",
                "corrective_actions",
                "corrective_action_approvals",
                "analysis_runs",
                "frame_analyses",
                "frame_analysis_review_events",
                "analysis_run_contracts",
                "frame_observation_sets",
                "frame_observation_items",
            }
            counts = {
                table: int(
                    connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                )
                for table in sorted(tables & allowed)
            }
            return {
                "exists": True,
                "quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
                "schema_version": int(
                    connection.execute("PRAGMA user_version").fetchone()[0]
                ),
                "counts": counts,
                "size_bytes": path.stat().st_size,
            }
    except (OSError, sqlite3.Error) as error:
        return {"exists": True, "status": "failed", "error": _redact(str(error))}


def _safe_release_summary(root: Path) -> dict[str, Any]:
    inventory = artifact_inventory(root)
    return {
        "summary": inventory["summary"],
        "releases": [
            {
                "release_id": item.get("release_id"),
                "product": item.get("product"),
                "lifecycle": item.get("lifecycle"),
                "created_at": item.get("created_at"),
                "artifact_kinds": [
                    artifact.get("kind") for artifact in item.get("artifacts", [])
                ],
            }
            for item in inventory["releases"]
        ],
    }


def _write_json(root: Path, relative: str, value: Any) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    value.update(path.read_bytes())
    return value.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--include-logs",
        action="store_true",
        help="Include redacted tails from var/spatial/logs",
    )
    args = parser.parse_args()
    layout = SceneRepositoryLayout.from_root(ROOT)
    created = datetime.now(timezone.utc)
    output = args.output or (
        layout.deliverables
        / "diagnostics"
        / f"diagnostic-{created.strftime('%Y%m%dT%H%M%S%fZ')}.zip"
    )
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="spatial-diagnostic-") as temporary:
        staging = Path(temporary)
        _write_json(
            staging,
            "system.json",
            {
                "schema_version": "1.0.0",
                "generated_at": created.isoformat(),
                "os": platform.system(),
                "os_release": platform.release(),
                "architecture": platform.machine(),
                "python": platform.python_version(),
                "machine_identity_included": False,
            },
        )
        _write_json(staging, "product-release.json", _safe_release_summary(ROOT))
        _write_json(staging, "runtime.json", application_runtime_contract())
        _write_json(
            staging,
            "gpu.json",
            {
                "nvidia_smi": _command(
                    [
                        "nvidia-smi",
                        "--query-gpu=name,driver_version,memory.total,compute_cap",
                        "--format=csv,noheader,nounits",
                    ]
                ),
                "docker_gpu": _command(
                    [
                        "docker",
                        "run",
                        "--pull=never",
                        "--rm",
                        "--gpus",
                        "all",
                        "ubuntu:24.04",
                        "nvidia-smi",
                        "-L",
                    ]
                ),
            },
        )
        _write_json(
            staging,
            "docker.json",
            {
                "docker": _command(["docker", "version", "--format", "{{json .}}"]),
                "compose": _command(["docker", "compose", "version"]),
                "services": _command(["docker", "compose", "ps", "--format", "json"]),
                "environment_or_compose_config_included": False,
            },
        )
        usage = shutil.disk_usage(ROOT)
        _write_json(
            staging,
            "disk.json",
            {
                "repository_filesystem": {
                    "total_bytes": usage.total,
                    "used_bytes": usage.used,
                    "free_bytes": usage.free,
                },
                "canonical_scene_content_included": False,
            },
        )
        _write_json(staging, "database.json", _database_status(layout.site_registry))
        profiles = []
        for path in sorted(
            (ROOT / "packages/distribution_contracts/profiles").glob("*.json")
        ):
            value = json.loads(path.read_text(encoding="utf-8"))
            profiles.append(
                {
                    "id": value["metadata"]["id"],
                    "version": value["metadata"]["version"],
                    "qualification_targets": value["platforms"][
                        "qualification_targets"
                    ],
                }
            )
        _write_json(staging, "capabilities.json", {"product_profiles": profiles})
        if args.include_logs and layout.logs.is_dir():
            for source in sorted(layout.logs.glob("*.log")):
                if source.is_symlink() or not source.is_file():
                    continue
                destination = staging / "logs" / source.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(
                    _redact(
                        source.read_text(encoding="utf-8", errors="replace")[-200_000:]
                    ),
                    encoding="utf-8",
                )
        files = []
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                files.append(
                    {
                        "path": path.relative_to(staging).as_posix(),
                        "sha256": _digest(path),
                        "size_bytes": path.stat().st_size,
                    }
                )
        _write_json(
            staging,
            "bundle-manifest.json",
            {
                "schema_version": "1.0.0",
                "generated_at": created.isoformat(),
                "files": files,
                "privacy": {
                    "secrets_included": False,
                    "personal_data_included": False,
                    "logs_included": args.include_logs,
                },
            },
        )
        # Fail closed if the sanitizer missed a known credential shape.
        for path in staging.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if any(pattern.search(text) for pattern in LEAK_PATTERNS):
                raise SystemExit(f"diagnostic bundle secret scan failed: {path.name}")
        partial = output.with_name(f".{output.name}.{os.getpid()}.partial")
        partial.unlink(missing_ok=True)
        with zipfile.ZipFile(
            partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(staging).as_posix())
        os.replace(partial, output)
    print(
        json.dumps(
            {
                "status": "created",
                "path": str(output),
                "sha256": _digest(output),
                "size_bytes": output.stat().st_size,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
