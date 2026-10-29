"""Content-addressed artifacts and safe Content Pack installation."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


ALREADY_COMPRESSED_SUFFIXES = {
    ".7z", ".gz", ".jpeg", ".jpg", ".mp4", ".png", ".spz", ".webp", ".zip",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_member(name: str) -> PurePosixPath:
    value = PurePosixPath(name)
    if value.is_absolute() or not value.parts or any(part in {"", ".", ".."} for part in value.parts):
        raise ValueError(f"content_pack_member_invalid:{name}")
    return value


def _zip_info(name: str, *, mode: int = 0o644) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | mode) << 16
    return info


def _write_file(archive: zipfile.ZipFile, source: Path, name: str) -> dict[str, Any]:
    digest = hashlib.sha256()
    compression = (
        zipfile.ZIP_STORED
        if source.suffix.lower() in ALREADY_COMPRESSED_SUFFIXES
        else zipfile.ZIP_DEFLATED
    )
    info = _zip_info(name)
    info.compress_type = compression
    size = 0
    with source.open("rb") as input_stream, archive.open(info, "w") as output_stream:
        while True:
            chunk = input_stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            output_stream.write(chunk)
            size += len(chunk)
    return {"uri": name, "size_bytes": size, "sha256": digest.hexdigest()}


def create_scene_content_pack(
    scene_package_root: Path,
    store_root: Path,
    *,
    scene_package_id: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write one deterministic Scene Package ZIP and place it by ZIP digest."""

    if scene_package_root.is_symlink() or not scene_package_root.is_dir():
        raise ValueError("content_pack_scene_package_invalid")
    files = sorted(path for path in scene_package_root.rglob("*") if path.is_file())
    if any(path.is_symlink() for path in scene_package_root.rglob("*")):
        raise ValueError("content_pack_symlink_not_allowed")
    manifest_path = scene_package_root / "scene_manifest.json"
    if not manifest_path.is_file():
        raise ValueError("content_pack_scene_manifest_missing")

    store_root.mkdir(parents=True, exist_ok=True)
    descriptor: dict[str, Any] = {
        "schema_version": "1.0.0",
        "kind": "spatial_scene_content_pack",
        "scene_package_id": scene_package_id,
        "scene_manifest_sha256": sha256_file(manifest_path),
        "mount": f"web_viewer/app/public/scene_package/runtime/{scene_package_id}",
        "files": [],
    }
    if metadata:
        descriptor["scene"] = metadata

    fd, temporary_name = tempfile.mkstemp(prefix=".content-pack-", suffix=".partial", dir=store_root)
    os.close(fd)
    partial = Path(temporary_name)
    try:
        with zipfile.ZipFile(partial, "w", allowZip64=True) as archive:
            for source in files:
                relative = source.relative_to(scene_package_root).as_posix()
                descriptor["files"].append(
                    _write_file(
                        archive,
                        source,
                        f"scene_package/{scene_package_id}/{relative}",
                    )
                )
            descriptor["file_count"] = len(descriptor["files"])
            descriptor["content_size_bytes"] = sum(
                int(item["size_bytes"]) for item in descriptor["files"]
            )
            manifest_payload = (
                json.dumps(descriptor, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            ).encode("utf-8")
            info = _zip_info("content_pack_manifest.json")
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, manifest_payload)
        digest = sha256_file(partial)
        destination = store_root / f"{digest}.zip"
        if destination.exists():
            if sha256_file(destination) != digest:
                raise ValueError("content_pack_store_digest_mismatch")
            partial.unlink()
            reused = True
        else:
            os.replace(partial, destination)
            reused = False
    finally:
        partial.unlink(missing_ok=True)
    return {
        "kind": descriptor["kind"],
        "scene_package_id": scene_package_id,
        "sha256": digest,
        "size_bytes": destination.stat().st_size,
        "content_size_bytes": descriptor["content_size_bytes"],
        "file_count": descriptor["file_count"],
        "path": str(destination),
        "filename": destination.name,
        "reused": reused,
    }


def read_content_pack_manifest(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError("content_pack_not_found")
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo("content_pack_manifest.json")
        if info.file_size > 16 * 1024 * 1024:
            raise ValueError("content_pack_manifest_too_large")
        value = json.loads(archive.read(info).decode("utf-8"))
    if not isinstance(value, dict) or value.get("kind") != "spatial_scene_content_pack":
        raise ValueError("content_pack_manifest_invalid")
    return value


def install_scene_content_pack(path: Path, distribution_root: Path) -> dict[str, Any]:
    """Validate and atomically install a Scene Pack into one Runtime Bundle."""

    manifest = read_content_pack_manifest(path)
    scene_package_id = manifest.get("scene_package_id")
    files = manifest.get("files")
    if not isinstance(scene_package_id, str) or not scene_package_id:
        raise ValueError("content_pack_scene_package_id_invalid")
    if not isinstance(files, list):
        raise ValueError("content_pack_files_invalid")
    runtime = distribution_root / "web_viewer/app/public/scene_package/runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    destination = runtime / scene_package_id
    expected_prefix = PurePosixPath("scene_package") / scene_package_id

    if destination.exists():
        installed_manifest = destination / "scene_manifest.json"
        if (
            destination.is_dir()
            and not destination.is_symlink()
            and installed_manifest.is_file()
            and sha256_file(installed_manifest) == manifest.get("scene_manifest_sha256")
        ):
            return {"status": "already_installed", "scene_package_id": scene_package_id}
        raise FileExistsError("content_pack_destination_conflict")

    with tempfile.TemporaryDirectory(prefix=".scene-content-", dir=runtime) as temporary:
        staging_root = Path(temporary)
        staging_package = staging_root / scene_package_id
        staging_package.mkdir()
        expected: dict[str, dict[str, Any]] = {}
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get("uri"), str):
                raise ValueError("content_pack_file_entry_invalid")
            member = _safe_member(item["uri"])
            if tuple(member.parts[:2]) != tuple(expected_prefix.parts):
                raise ValueError("content_pack_file_outside_scene_package")
            relative = PurePosixPath(*member.parts[2:])
            _safe_member(relative.as_posix())
            expected[item["uri"]] = item

        with zipfile.ZipFile(path) as archive:
            archive_names = set()
            for info in archive.infolist():
                if info.filename == "content_pack_manifest.json":
                    continue
                member = _safe_member(info.filename)
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode) or info.is_dir():
                    raise ValueError("content_pack_link_or_directory_not_allowed")
                item = expected.get(member.as_posix())
                if item is None or member.as_posix() in archive_names:
                    raise ValueError("content_pack_unexpected_or_duplicate_member")
                archive_names.add(member.as_posix())
                relative = PurePosixPath(*member.parts[2:])
                target = staging_package.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                size = 0
                with archive.open(info) as source, target.open("wb") as output:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        output.write(chunk)
                        size += len(chunk)
                if digest.hexdigest() != item.get("sha256") or size != item.get("size_bytes"):
                    raise ValueError("content_pack_file_digest_mismatch")
            if archive_names != set(expected):
                raise ValueError("content_pack_file_set_mismatch")
        if sha256_file(staging_package / "scene_manifest.json") != manifest.get("scene_manifest_sha256"):
            raise ValueError("content_pack_scene_manifest_digest_mismatch")
        os.replace(staging_package, destination)
    return {"status": "installed", "scene_package_id": scene_package_id}
