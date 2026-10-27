#!/usr/bin/env python3
"""Inspect, back up, and restore a distribution's writable Site Records state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def distribution_root(value: str | None) -> Path:
    return (
        (Path(value) if value else Path(__file__).resolve().parents[1])
        .expanduser()
        .resolve()
    )


def state_root(root: Path) -> Path:
    path = root / "state" / "site-records"
    if path.is_symlink():
        raise RuntimeError("distribution_state_symlink_not_allowed")
    return path


def assert_distribution_stopped(root: Path) -> None:
    if not (root / "docker-compose.yml").is_file() or shutil.which("docker") is None:
        return
    result = subprocess.run(
        ["docker", "compose", "ps", "--status", "running", "--quiet"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        raise RuntimeError(
            "distribution_must_be_stopped_before_overlay_backup_or_restore"
        )


def database_status(database: Path) -> dict[str, object]:
    if not database.is_file():
        return {"exists": False, "integrity": "not_initialized", "counts": {}}
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        counts = {}
        for table in (
            "sites",
            "scene_versions",
            "analysis_runs",
            "frame_analyses",
            "frame_analysis_review_events",
            "analysis_run_contracts",
            "frame_observation_sets",
            "frame_observation_items",
        ):
            if table in tables:
                counts[table] = int(
                    connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                )
        return {"exists": True, "integrity": integrity, "counts": counts}
    finally:
        connection.close()


def status(root: Path) -> dict[str, object]:
    state = state_root(root)
    attachments = state / "attachments" / "sha256"
    attachment_files = (
        [
            path
            for path in attachments.rglob("*")
            if path.is_file() and not path.is_symlink()
        ]
        if attachments.is_dir()
        else []
    )
    return {
        "schema_version": "1.0.0",
        "status": "verified",
        "distribution_root": str(root),
        "state_uri": "state/site-records",
        "database": database_status(state / "registry.sqlite3"),
        "attachment_count": len(attachment_files),
        "attachment_bytes": sum(path.stat().st_size for path in attachment_files),
    }


def backup(root: Path, output: Path | None = None) -> Path:
    assert_distribution_stopped(root)
    state = state_root(root)
    database = state / "registry.sqlite3"
    if not database.is_file():
        raise FileNotFoundError("distribution_overlay_database_not_initialized")
    if database_status(database)["integrity"] != "ok":
        raise RuntimeError("distribution_overlay_database_integrity_failed")
    backup_root = root / "backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = (
        (output or backup_root / f"site-records-{timestamp}-{uuid.uuid4().hex[:8]}.zip")
        .expanduser()
        .resolve()
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.partial")
    with tempfile.TemporaryDirectory(prefix="spatial-overlay-backup-") as temporary:
        staged = Path(temporary) / "site-records"
        staged.mkdir()
        source_connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        target_connection = sqlite3.connect(staged / "registry.sqlite3")
        try:
            source_connection.backup(target_connection)
            target_connection.commit()
        finally:
            target_connection.close()
            source_connection.close()
        for path in state.rglob("*"):
            if path.is_symlink():
                raise RuntimeError(
                    f"distribution_overlay_symlink_not_allowed:{path.relative_to(state)}"
                )
            if not path.is_file() or path == database:
                continue
            destination_path = staged / path.relative_to(state)
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination_path)
        files = {
            path.relative_to(staged.parent).as_posix(): {
                "size_bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
            for path in sorted(staged.rglob("*"))
            if path.is_file()
        }
        manifest = {
            "schema_version": "1.0.0",
            "kind": "spatial_3dgs_distribution_overlay_backup",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_application": json.loads(
                (root / "app_package_manifest.json").read_text(encoding="utf-8")
            )
            if (root / "app_package_manifest.json").is_file()
            else None,
            "database": database_status(staged / "registry.sqlite3"),
            "files": files,
        }
        manifest_path = Path(temporary) / "backup_manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        try:
            with zipfile.ZipFile(
                partial, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
            ) as archive:
                archive.write(manifest_path, "backup_manifest.json")
                for path in sorted(staged.rglob("*")):
                    if path.is_file():
                        archive.write(path, path.relative_to(staged.parent).as_posix())
            os.replace(partial, destination)
        finally:
            partial.unlink(missing_ok=True)
    return destination


def _verified_restore_tree(archive_path: Path, temporary: Path) -> Path:
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            target = (temporary / member.filename).resolve()
            if (
                temporary.resolve() not in target.parents
                and target != temporary.resolve()
            ):
                raise RuntimeError("overlay_backup_member_outside_root")
        archive.extractall(temporary)
    manifest_path = temporary / "backup_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "spatial_3dgs_distribution_overlay_backup":
        raise RuntimeError("overlay_backup_manifest_invalid")
    restored = temporary / "site-records"
    for relative, expected in manifest.get("files", {}).items():
        path = temporary / relative
        if (
            not path.is_file()
            or path.stat().st_size != int(expected["size_bytes"])
            or sha256(path) != expected["sha256"]
        ):
            raise RuntimeError(f"overlay_backup_checksum_mismatch:{relative}")
    if database_status(restored / "registry.sqlite3")["integrity"] != "ok":
        raise RuntimeError("overlay_backup_database_integrity_failed")
    return restored


def restore(root: Path, archive_path: Path) -> Path:
    assert_distribution_stopped(root)
    archive_path = archive_path.expanduser().resolve()
    if not archive_path.is_file():
        raise FileNotFoundError("overlay_backup_not_found")
    state = state_root(root)
    if (state / "registry.sqlite3").is_file():
        backup(root)
    with tempfile.TemporaryDirectory(
        prefix="spatial-overlay-restore-", dir=state.parent
    ) as temporary_value:
        temporary = Path(temporary_value)
        restored = _verified_restore_tree(archive_path, temporary)
        old = state.parent / f".site-records-restore-old-{uuid.uuid4().hex}"
        if state.exists():
            os.replace(state, old)
        try:
            os.replace(restored, state)
        except BaseException:
            if old.exists() and not state.exists():
                os.replace(old, state)
            raise
        shutil.rmtree(old, ignore_errors=True)
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "backup", "restore"))
    parser.add_argument("--root")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--input", type=Path)
    args = parser.parse_args()
    root = distribution_root(args.root)
    if args.command == "status":
        result: object = status(root)
    elif args.command == "backup":
        result = {"status": "verified", "backup": str(backup(root, args.output))}
    else:
        if args.input is None:
            parser.error("restore requires --input")
        result = {"status": "verified", "state": str(restore(root, args.input))}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
