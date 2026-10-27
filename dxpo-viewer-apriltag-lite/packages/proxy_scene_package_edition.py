"""Create an immutable proxy-enabled edition of a verified Scene Package.

The source Package is never modified. Large immutable payloads are hardlinked
into a staging tree, identity-bearing JSON and SQLite files are replaced with
new inodes, and the complete derived Package is validated before it is
atomically published to the canonical repository and Viewer runtime.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import struct
import subprocess
from typing import Any, Iterator
import uuid

from generation_pipeline.tools.scene_dataset import publish_scene_package_tree
from generation_pipeline.tools.validate_scene_package import validate_scene_package
from packages.scene_repository import SceneRepositoryLayout, rebuild_catalog


PACKAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
PROXY_FILENAMES = ("proxy.glb", "proxy_metadata.json", "proxy_quality.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{label}_invalid:{error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label}_invalid")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_glb(path: Path) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size < 20:
        raise ValueError("proxy_glb_missing_or_empty")
    with path.open("rb") as handle:
        header = handle.read(12)
    magic, version, declared_length = struct.unpack("<4sII", header)
    if magic != b"glTF" or version != 2 or declared_length != path.stat().st_size:
        raise ValueError("proxy_glb_header_invalid")


def validate_proxy_artifacts(proxy_directory: Path) -> dict[str, Any]:
    """Validate the bounded proxy artifact set before any repository write."""

    directory = proxy_directory.resolve()
    if proxy_directory.is_symlink() or not directory.is_dir():
        raise ValueError("proxy_directory_invalid")
    paths = {name: directory / name for name in PROXY_FILENAMES}
    for name, path in paths.items():
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"proxy_artifact_missing:{name}")
    _validate_glb(paths["proxy.glb"])
    metadata = _read_object(paths["proxy_metadata.json"], "proxy_metadata")
    quality = _read_object(paths["proxy_quality.json"], "proxy_quality")
    if quality.get("status") != "verified" or quality.get("proxy_kind") != "surface_mesh":
        raise ValueError("proxy_quality_not_verified")
    mesh = metadata.get("mesh")
    if not isinstance(mesh, dict):
        raise ValueError("proxy_metadata_mesh_missing")
    vertex_count = quality.get("vertex_count")
    triangle_count = quality.get("triangle_count")
    if (
        not isinstance(vertex_count, int)
        or isinstance(vertex_count, bool)
        or vertex_count <= 0
        or not isinstance(triangle_count, int)
        or isinstance(triangle_count, bool)
        or triangle_count <= 0
        or mesh.get("vertex_count") != vertex_count
        or mesh.get("triangle_count") != triangle_count
    ):
        raise ValueError("proxy_metadata_quality_count_mismatch")
    coordinate = metadata.get("coordinate_system")
    if metadata.get("unit") != "meter" or not isinstance(coordinate, dict) or coordinate.get("up_axis") != "Y":
        raise ValueError("proxy_metadata_coordinate_mismatch")
    hashes = {name: _sha256(path) for name, path in paths.items()}
    return {
        "status": "verified",
        "paths": paths,
        "hashes": hashes,
        "metadata": metadata,
        "quality": quality,
    }


def _derived_package_id(source_manifest: dict[str, Any], hashes: dict[str, str]) -> str:
    source_id = str(source_manifest["scene_package_id"])
    reconstruction_id = str(source_manifest["reconstruction_id"])
    identity = hashlib.sha256()
    identity.update(source_id.encode("utf-8"))
    for name in PROXY_FILENAMES:
        identity.update(name.encode("utf-8"))
        identity.update(hashes[name].encode("ascii"))
    suffix = f"-proxyv1-{identity.hexdigest()[:12]}"
    prefix = reconstruction_id[: 64 - len(suffix)].rstrip(".-_")
    value = prefix + suffix
    if not PACKAGE_ID_RE.fullmatch(value):
        raise ValueError("derived_scene_package_id_invalid")
    return value


def _refresh_runtime_index(root: Path) -> list[str]:
    script = root / "web_viewer/app/scripts/refresh_scene_package_index.mjs"
    node = shutil.which("node")
    if not script.is_file():
        return []
    if node is None:
        return ["viewer_runtime_index_not_refreshed:node_not_found"]
    completed = subprocess.run(
        [node, str(script)],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        return ["viewer_runtime_index_not_refreshed:runner_failed"]
    return []


def _replace_field_memory_snapshot(
    source_database: Path,
    destination_database: Path,
    source_package_id: str,
    destination_package_id: str,
) -> None:
    """Create a coherent SQLite snapshot and rewrite Package foreign keys."""

    temporary = destination_database.with_name(f".{destination_database.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    try:
        with sqlite3.connect(f"file:{source_database}?mode=ro", uri=True) as source:
            with sqlite3.connect(temporary) as destination:
                source.backup(destination)
                tables = [
                    str(row[0])
                    for row in destination.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    )
                ]
                for table in tables:
                    quoted = '"' + table.replace('"', '""') + '"'
                    columns = {
                        str(row[1]) for row in destination.execute(f"PRAGMA table_info({quoted})")
                    }
                    if "scene_package_id" in columns:
                        destination.execute(
                            f"UPDATE {quoted} SET scene_package_id = ? WHERE scene_package_id = ?",
                            (destination_package_id, source_package_id),
                        )
                destination.commit()
                result = destination.execute("PRAGMA quick_check").fetchone()
                if result is None or result[0] != "ok":
                    raise ValueError("derived_field_memory_quick_check_failed")
        os.replace(temporary, destination_database)
        destination_database.with_name(destination_database.name + "-wal").unlink(missing_ok=True)
        destination_database.with_name(destination_database.name + "-shm").unlink(missing_ok=True)
        destination_database.with_name(destination_database.name + "-journal").unlink(missing_ok=True)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _publication_lock(layout: SceneRepositoryLayout, package_id: str) -> Iterator[None]:
    lock = layout.locks / "proxy-editions" / f"{package_id}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def create_proxy_scene_package_edition(
    root: Path,
    source_scene_package_id: str,
    proxy_directory: Path,
) -> dict[str, Any]:
    """Publish a new proxy-enabled v1.3 Scene Package and runtime tree."""

    repository_root = root.resolve()
    layout = SceneRepositoryLayout.from_root(repository_root)
    layout.ensure_runtime_roots()
    if not PACKAGE_ID_RE.fullmatch(source_scene_package_id):
        raise ValueError("source_scene_package_id_invalid")
    source = layout.scene_packages / source_scene_package_id
    if source.is_symlink() or not source.is_dir():
        raise FileNotFoundError(f"source_scene_package_not_found:{source_scene_package_id}")
    source_manifest = _read_object(source / "scene_manifest.json", "source_scene_manifest")
    if source_manifest.get("schema_version") != "1.3.0":
        raise ValueError("proxy_edition_requires_self_contained_v1_3_package")
    if source_manifest.get("scene_package_id") != source_scene_package_id:
        raise ValueError("source_scene_package_identity_mismatch")
    source_validation = validate_scene_package(source, str(source_manifest.get("scene_id")))
    if source_validation.get("status") != "verified":
        raise ValueError("source_scene_package_not_verified")
    if source_manifest.get("assets", {}).get("proxy_mesh") is not None:
        raise ValueError("source_scene_package_already_has_proxy")

    proxy = validate_proxy_artifacts(proxy_directory)
    package_id = _derived_package_id(source_manifest, proxy["hashes"])
    canonical = layout.scene_packages / package_id
    runtime = repository_root / "web_viewer/app/public/scene_package/runtime" / package_id
    token = uuid.uuid4().hex
    staging = canonical.with_name(f".{package_id}.proxy-edition.{token}")
    runtime_staging = runtime.with_name(f".{package_id}.publish.{token}")
    committed = False
    publication_storage: dict[str, int | str] = {}
    try:
        with _publication_lock(layout, package_id):
            if canonical.exists():
                raise FileExistsError(f"scene_package_id_already_exists:{package_id}")
            if runtime.exists():
                raise FileExistsError(f"viewer_runtime_already_exists:{package_id}")

            # Hardlink the large immutable payload. Identity-bearing files are
            # subsequently replaced atomically, so their source inodes remain
            # byte-for-byte unchanged.
            publish_scene_package_tree(source, staging)
            geometry = staging / "geometry"
            geometry.mkdir(parents=True, exist_ok=True)
            for name in PROXY_FILENAMES:
                shutil.copy2(proxy["paths"][name], geometry / name)

            dataset_manifest_path = staging / "dataset/manifest.json"
            dataset_manifest = _read_object(dataset_manifest_path, "scene_dataset_manifest")
            dataset_manifest["scene_package_id"] = package_id
            provenance = dataset_manifest.setdefault("provenance", {})
            if not isinstance(provenance, dict):
                raise ValueError("scene_dataset_provenance_invalid")
            source_ids = provenance.get("source_package_ids")
            if not isinstance(source_ids, list):
                source_ids = []
            provenance["source_package_ids"] = list(dict.fromkeys([
                *(str(item) for item in source_ids if isinstance(item, str)),
                source_scene_package_id,
            ]))
            provenance["proxy_edition"] = {
                "source_scene_package_id": source_scene_package_id,
                "proxy_sha256": proxy["hashes"]["proxy.glb"],
            }

            field_memory = dataset_manifest.get("artifacts", {}).get("field_memory", {})
            database_uri = field_memory.get("database_uri") if isinstance(field_memory, dict) else None
            if not isinstance(database_uri, str):
                raise ValueError("scene_dataset_field_memory_database_uri_invalid")
            source_database = source / "dataset" / database_uri
            destination_database = staging / "dataset" / database_uri
            _replace_field_memory_snapshot(
                source_database,
                destination_database,
                source_scene_package_id,
                package_id,
            )
            field_memory["database_sha256"] = _sha256(destination_database)
            _atomic_json(dataset_manifest_path, dataset_manifest)

            manifest = _read_object(staging / "scene_manifest.json", "scene_manifest")
            manifest["created_at"] = datetime.now(timezone.utc).isoformat()
            manifest["scene_package_id"] = package_id
            manifest["dataset"]["manifest_sha256"] = _sha256(dataset_manifest_path)
            manifest["assets"]["proxy_mesh"] = {
                "uri": "geometry/proxy.glb",
                "purpose": "measurement_and_occlusion",
                "coordinate_space": "world",
                "metadata_uri": "geometry/proxy_metadata.json",
                "quality_uri": "geometry/proxy_quality.json",
                "sha256": proxy["hashes"]["proxy.glb"],
                "metadata_sha256": proxy["hashes"]["proxy_metadata.json"],
                "quality_sha256": proxy["hashes"]["proxy_quality.json"],
            }
            manifest["capabilities"].update({
                "display": True,
                "measurement": True,
                "surface_placement": True,
                "fov": True,
                "occlusion": True,
            })
            manifest["provenance"]["proxy_geometry"] = {
                "status": "verified",
                "source_scene_package_id": source_scene_package_id,
                "proxy_kind": "surface_mesh",
                "asset_sha256": proxy["hashes"]["proxy.glb"],
                "metadata_sha256": proxy["hashes"]["proxy_metadata.json"],
                "quality_sha256": proxy["hashes"]["proxy_quality.json"],
            }
            _atomic_json(staging / "scene_manifest.json", manifest)

            validation = validate_scene_package(staging, str(manifest["scene_id"]))
            if validation.get("status") != "verified":
                reasons = ",".join(str(item) for item in validation.get("failure_reasons", []))
                raise ValueError(f"proxy_scene_package_validation_failed:{reasons}")
            canonical.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, canonical)
            committed = True

            publication_storage = publish_scene_package_tree(canonical, runtime_staging)
            runtime_validation = validate_scene_package(runtime_staging, str(manifest["scene_id"]))
            if runtime_validation.get("status") != "verified":
                raise ValueError("proxy_viewer_runtime_validation_failed")
            runtime.parent.mkdir(parents=True, exist_ok=True)
            os.replace(runtime_staging, runtime)
            rebuild_catalog(repository_root)
    except Exception:
        if committed and canonical.exists() and not runtime.exists():
            shutil.rmtree(canonical, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(runtime_staging, ignore_errors=True)

    return {
        "status": "verified",
        "source_scene_package_id": source_scene_package_id,
        "scene_package_id": package_id,
        "scene_id": source_manifest["scene_id"],
        "canonical_uri": canonical.relative_to(repository_root).as_posix(),
        "publish_uri": runtime.relative_to(repository_root).as_posix(),
        "capabilities": {
            "display": True,
            "measurement": True,
            "surface_placement": True,
            "fov": True,
            "occlusion": True,
        },
        "proxy": {
            "sha256": proxy["hashes"]["proxy.glb"],
            "vertex_count": proxy["quality"]["vertex_count"],
            "triangle_count": proxy["quality"]["triangle_count"],
        },
        "publication_storage": publication_storage,
        "viewer_url": f"/?scene_package={package_id}",
        "warnings": _refresh_runtime_index(repository_root),
    }
