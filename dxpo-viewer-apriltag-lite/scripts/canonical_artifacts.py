#!/usr/bin/env python3
"""Create, verify and safely restore immutable canonical artifact backups."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


CANONICAL_ROOTS = (
    PurePosixPath("data/scene_repository/packages/metric_reconstructions"),
    PurePosixPath("data/scene_repository/packages/scenes"),
    PurePosixPath("data/scene_repository/assets/source_files"),
    PurePosixPath("data/scene_repository/registries/site_records"),
)
FIELD_MEMORY_DB = PurePosixPath("data/scene_repository/registries/site_records/registry.sqlite3")
EXCLUDED_PREFIXES: tuple[PurePosixPath, ...] = ()
MANIFEST_NAME = "canonical_backup_manifest.json"


def sha256_stream(stream: Any) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _excluded(path: PurePosixPath) -> bool:
    return any(path == prefix or prefix in path.parents for prefix in EXCLUDED_PREFIXES)


def canonical_files(
    repo_root: Path,
    overrides: dict[PurePosixPath, Path] | None = None,
) -> list[tuple[PurePosixPath, Path]]:
    replacements = overrides or {}
    files: list[tuple[PurePosixPath, Path]] = []
    for relative_root in CANONICAL_ROOTS:
        source_root = repo_root / Path(relative_root)
        if not source_root.is_dir():
            continue
        for source in sorted(path for path in source_root.rglob("*") if path.is_file()):
            if source.is_symlink():
                raise ValueError(f"canonical_symlink_not_allowed:{source}")
            relative = PurePosixPath(source.relative_to(repo_root).as_posix())
            if _excluded(relative) or relative.name.endswith(("-wal", "-shm")):
                continue
            files.append((relative, replacements.get(relative, source)))
    return files


def _model_provenance(repo_root: Path) -> list[dict[str, Any]]:
    provenance_path = repo_root / ".external/models/local-vlm/SPATIAL_MODEL_PROVENANCE.json"
    expected = {
        "model_id": "Qwen/Qwen3-VL-8B-Instruct",
        "revision": "0c351dd01ed87e9c1b53cbc748cba10e6187ff3b",
        "license": "Apache-2.0",
        "parameter_scale": "8B",
        "weights_included": False,
    }
    if not provenance_path.is_file():
        return [{**expected, "local_verification": "not_installed"}]
    try:
        actual = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("reviewed_model_provenance_invalid") from error
    for key in ("model_id", "revision", "license", "parameter_scale"):
        if actual.get(key) != expected[key]:
            raise ValueError(f"reviewed_model_provenance_mismatch:{key}")
    with provenance_path.open("rb") as stream:
        digest = sha256_stream(stream)
    return [{**expected, "local_verification": "verified", "provenance_sha256": digest}]


@contextmanager
def consistent_sources(repo_root: Path) -> Iterable[dict[PurePosixPath, Path]]:
    """Yield immutable sources, including an online SQLite snapshot.

    Field-memory content is content-addressed and append-only.  Taking the
    SQLite online backup before enumerating that store gives a consistent
    recoverable DB plus every object it references; concurrently appended,
    unreferenced objects are harmless and can be garbage-collected later.
    """

    source_db = repo_root / Path(FIELD_MEMORY_DB)
    with tempfile.TemporaryDirectory(prefix="spatial-canonical-snapshot-") as temporary:
        overrides: dict[PurePosixPath, Path] = {}
        if source_db.is_file():
            snapshot_db = Path(temporary) / "registry.sqlite3"
            source_uri = f"file:{source_db.as_posix()}?mode=ro"
            try:
                with sqlite3.connect(source_uri, uri=True) as source:
                    with sqlite3.connect(snapshot_db) as target:
                        source.backup(target)
                        target.execute("PRAGMA integrity_check")
            except sqlite3.Error as error:
                raise ValueError(f"field_memory_online_backup_failed:{error}") from error
            overrides[FIELD_MEMORY_DB] = snapshot_db
        yield overrides


def inventory(
    repo_root: Path,
    overrides: dict[PurePosixPath, Path] | None = None,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    total = 0
    for relative, source in canonical_files(repo_root, overrides):
        size = source.stat().st_size
        with source.open("rb") as handle:
            digest = sha256_stream(handle)
        entries.append({"path": str(relative), "size_bytes": size, "sha256": digest})
        total += size
    return {
        "schema_version": "1.1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "canonical_roots": [str(item) for item in CANONICAL_ROOTS],
        "file_count": len(entries),
        "total_size_bytes": total,
        "files": entries,
        "reviewed_models": _model_provenance(repo_root),
        "excluded_regenerable_prefixes": [str(item) for item in EXCLUDED_PREFIXES],
    }


def _is_canonical_path(path: PurePosixPath) -> bool:
    return any(path == root or root in path.parents for root in CANONICAL_ROOTS)


def create_backup(repo_root: Path, output: Path) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    output = output.resolve()
    if any((repo_root / Path(root)).resolve() in output.parents for root in CANONICAL_ROOTS):
        raise ValueError("backup_output_must_not_be_inside_canonical_roots")
    with consistent_sources(repo_root) as overrides:
        manifest = inventory(repo_root, overrides)
        sources = dict(canonical_files(repo_root, overrides))
        output.parent.mkdir(parents=True, exist_ok=True)
        free_bytes = shutil.disk_usage(output.parent).free
        required = int(manifest["total_size_bytes"]) + 64 * 1024 * 1024
        if free_bytes < required:
            raise ValueError(f"backup_free_space_insufficient:required={required}:free={free_bytes}")
        partial = output.with_name(f".{output.name}.{os.getpid()}.partial")
        if output.exists() or partial.exists():
            raise FileExistsError(f"backup_already_exists:{output}")
        manifest_bytes = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with tarfile.open(partial, "w", format=tarfile.PAX_FORMAT) as archive:
                info = tarfile.TarInfo(MANIFEST_NAME)
                info.size = len(manifest_bytes)
                info.mtime = 0
                archive.addfile(info, io.BytesIO(manifest_bytes))
                for entry in manifest["files"]:
                    relative = PurePosixPath(entry["path"])
                    archive.add(
                        sources[relative], arcname=entry["path"], recursive=False,
                        filter=_normalized_tar_info,
                    )
            os.replace(partial, output)
        except Exception:
            partial.unlink(missing_ok=True)
            raise
    return manifest


def _normalized_tar_info(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def read_and_verify(archive_path: Path) -> dict[str, Any]:
    with tarfile.open(archive_path, "r") as archive:
        members = archive.getmembers()
        manifest_members = [member for member in members if member.name == MANIFEST_NAME and member.isfile()]
        if len(manifest_members) != 1:
            raise ValueError("backup_manifest_missing_or_duplicated")
        manifest_stream = archive.extractfile(manifest_members[0])
        if manifest_stream is None:
            raise ValueError("backup_manifest_unreadable")
        manifest = json.load(manifest_stream)
        expected = {entry["path"]: entry for entry in manifest.get("files", [])}
        actual_members: dict[str, tarfile.TarInfo] = {}
        for member in members:
            if member.name == MANIFEST_NAME:
                continue
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts or not _is_canonical_path(relative):
                raise ValueError(f"backup_member_path_invalid:{member.name}")
            if not member.isfile():
                raise ValueError(f"backup_member_type_invalid:{member.name}")
            if member.name in actual_members:
                raise ValueError(f"backup_member_duplicated:{member.name}")
            actual_members[member.name] = member
        if set(expected) != set(actual_members):
            raise ValueError("backup_file_set_mismatch")
        for path, entry in expected.items():
            member = actual_members[path]
            if member.size != int(entry["size_bytes"]):
                raise ValueError(f"backup_size_mismatch:{path}")
            stream = archive.extractfile(member)
            if stream is None or sha256_stream(stream) != entry["sha256"]:
                raise ValueError(f"backup_sha256_mismatch:{path}")
    return manifest


def restore_backup(archive_path: Path, destination: Path) -> dict[str, Any]:
    manifest = read_and_verify(archive_path)
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    for root in CANONICAL_ROOTS:
        if (destination / Path(root)).exists():
            raise FileExistsError(f"restore_target_already_exists:{destination / Path(root)}")
    staging = Path(tempfile.mkdtemp(prefix=".canonical-restore-", dir=destination))
    try:
        with tarfile.open(archive_path, "r") as archive:
            for entry in manifest["files"]:
                relative = PurePosixPath(entry["path"])
                member = archive.getmember(str(relative))
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError(f"backup_member_unreadable:{relative}")
                target = staging / Path(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("wb") as output:
                    shutil.copyfileobj(stream, output, length=1024 * 1024)
        # Re-verify staged bytes before making either canonical root visible.
        for entry in manifest["files"]:
            target = staging / entry["path"]
            with target.open("rb") as handle:
                if sha256_stream(handle) != entry["sha256"]:
                    raise ValueError(f"restore_sha256_mismatch:{entry['path']}")
        for root in CANONICAL_ROOTS:
            source = staging / Path(root)
            if source.exists():
                target = destination / Path(root)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inventory")
    inspect_parser.add_argument("--output", type=Path)
    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("--output", type=Path, required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--archive", type=Path, required=True)
    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--archive", type=Path, required=True)
    restore_parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "inventory":
        repo_root = args.repo_root.resolve()
        with consistent_sources(repo_root) as overrides:
            result = inventory(repo_root, overrides)
        rendered = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
        if args.output:
            args.output.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
    elif args.command == "create":
        result = create_backup(args.repo_root, args.output)
        print(f"backup created: {args.output} ({result['file_count']} files)")
    elif args.command == "verify":
        result = read_and_verify(args.archive)
        print(f"backup verified: {args.archive} ({result['file_count']} files)")
    else:
        result = restore_backup(args.archive, args.destination)
        print(f"backup restored: {args.destination} ({result['file_count']} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
