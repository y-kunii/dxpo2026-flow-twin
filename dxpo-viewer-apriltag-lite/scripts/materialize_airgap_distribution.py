#!/usr/bin/env python3
"""Materialize Runtime, Content, reviewed models, and OCI archives for air-gap hand-off."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.distribution_contracts.artifacts import read_content_pack_manifest
from scripts.verify_local_vlm import verify as verify_local_vlm
from services.vlm_model_catalog import reviewed_vlm


SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_file() or path.is_symlink():
        raise ValueError(f"airgap_{label}_must_be_regular_file")
    return resolved


def _safe_zip_member(name: str) -> bool:
    path = PurePosixPath(name)
    return not path.is_absolute() and ".." not in path.parts and "" not in path.parts


def _runtime_manifest(path: Path) -> dict[str, Any]:
    path = _regular(path, "runtime")
    with zipfile.ZipFile(path) as archive:
        manifests = [name for name in archive.namelist() if name.endswith("/app_package_manifest.json")]
        if len(manifests) != 1:
            raise ValueError("airgap_runtime_manifest_missing_or_ambiguous")
        for item in archive.infolist():
            if not _safe_zip_member(item.filename) or ((item.external_attr >> 16) & 0o170000) == 0o120000:
                raise ValueError("airgap_runtime_member_invalid")
        value = json.loads(archive.read(manifests[0]))
    if not isinstance(value, dict) or value.get("distribution_scope") not in {
        "internal_evaluation_only", "public_release",
    }:
        raise ValueError("airgap_runtime_manifest_invalid")
    return value


def _verify_oci_archive(path: Path) -> None:
    try:
        with tarfile.open(path, "r:*") as archive:
            members = archive.getmembers()
            names = {item.name.removeprefix("./") for item in members}
            if not ({"manifest.json", "oci-layout"} & names):
                raise ValueError("airgap_oci_archive_manifest_missing")
            if any(not _safe_zip_member(item.name.removeprefix("./")) for item in members):
                raise ValueError("airgap_oci_archive_member_invalid")
            if any(item.issym() or item.islnk() or item.isdev() for item in members):
                raise ValueError("airgap_oci_archive_special_member_not_allowed")
    except (tarfile.TarError, OSError) as error:
        raise ValueError("airgap_oci_archive_invalid") from error


def _tree_entries(root: Path, prefix: str) -> list[tuple[Path, str, dict[str, Any]]]:
    if not root.is_dir() or root.is_symlink():
        raise ValueError("airgap_model_directory_invalid")
    entries = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("airgap_model_symlink_not_allowed")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        archive_path = f"artifacts/models/{prefix}/{relative}"
        entries.append((path, archive_path, {
            "path": archive_path, "sha256": _sha256(path), "size_bytes": path.stat().st_size,
        }))
    return entries


def materialize(
    runtime: Path,
    content_packs: list[Path],
    models: dict[str, Path],
    oci_archives: dict[str, Path],
    output: Path,
) -> dict[str, Any]:
    runtime = _regular(runtime, "runtime")
    runtime_manifest = _runtime_manifest(runtime)
    if not oci_archives:
        raise ValueError("airgap_oci_archive_required")
    content_records = []
    for path in content_packs:
        checked = _regular(path, "content_pack")
        manifest = read_content_pack_manifest(checked)
        content_records.append({
            "source": checked,
            "archive_path": f"artifacts/content/{_sha256(checked)}.zip",
            "scene_package_id": manifest["scene_package_id"],
            "sha256": _sha256(checked),
            "size_bytes": checked.stat().st_size,
        })
    model_records = []
    model_entries: list[tuple[Path, str, dict[str, Any]]] = []
    for model_id, directory in sorted(models.items()):
        model = reviewed_vlm(model_id)
        verification = verify_local_vlm(directory, model_id)
        entries = _tree_entries(directory.resolve(), model.storage_directory)
        model_entries.extend(entries)
        model_records.append({
            "model_id": model_id,
            "revision": model.revision,
            "license": model.license_id,
            "storage_directory": model.storage_directory,
            "weight_bytes": verification["weight_bytes"],
            "files": [item[2] for item in entries],
        })
    components = runtime_manifest.get("composition", {}).get("backend_components", [])
    if "frame-vlm-worker" in components and not model_records:
        raise ValueError("airgap_reviewed_vlm_required_by_runtime")
    oci_records = []
    checked_oci = []
    for name, path in sorted(oci_archives.items()):
        if SAFE_NAME.fullmatch(name) is None:
            raise ValueError("airgap_oci_name_invalid")
        checked = _regular(path, "oci_archive")
        _verify_oci_archive(checked)
        archive_path = f"artifacts/oci/{name}.tar"
        checked_oci.append((checked, archive_path))
        oci_records.append({
            "name": name, "path": archive_path,
            "sha256": _sha256(checked), "size_bytes": checked.stat().st_size,
        })
    output = output.resolve()
    if output.suffix.lower() != ".zip":
        raise ValueError("airgap_output_must_be_zip")
    output.parent.mkdir(parents=True, exist_ok=True)
    estimated = runtime.stat().st_size + sum(item["size_bytes"] for item in content_records)
    estimated += sum(item[2]["size_bytes"] for item in model_entries)
    estimated += sum(item[0].stat().st_size for item in checked_oci)
    free = shutil.disk_usage(output.parent).free
    if free < estimated + max(2 * 1024**3, estimated // 20):
        raise ValueError("airgap_insufficient_free_space")
    created_at = datetime.now(timezone.utc)
    manifest = {
        "schema_version": "1.0.0",
        "kind": "spatial_airgap_distribution",
        "created_at": created_at.isoformat(),
        "runtime": {
            "application_id": runtime_manifest.get("application_id"),
            "application_version": runtime_manifest.get("application_version"),
            "product_release": runtime_manifest.get("product_release"),
            "path": f"artifacts/runtime/{runtime.name}",
            "sha256": _sha256(runtime),
            "size_bytes": runtime.stat().st_size,
        },
        "content_packs": [{key: value for key, value in item.items() if key != "source"} for item in content_records],
        "models": model_records,
        "oci_archives": oci_records,
        "configuration": {
            "runtime_setup_is_authoritative": True,
            "external_network_intent_after_materialization": "none",
            "external_network_requirement_must_be_verified_by_acceptance": True,
        },
        "acceptance": {
            "offline_install_evidence_included": False,
            "qualification_claimed": False,
        },
    }
    install = "# Air-gap installation\n\n"
    install += "1. Verify the outer ZIP SHA-256 supplied by the release owner.\n"
    install += "2. Load every `artifacts/oci/*.tar` with `docker load -i <file>`.\n"
    install += "3. Extract `artifacts/runtime/*.zip` and follow its `SETUP.md`.\n"
    install += "4. Copy reviewed model directories below `artifacts/models/` into the Runtime `models/` directory.\n"
    install += "5. Install each Content Pack with the Runtime `scripts/install_content_pack.py`.\n"
    install += "6. Run startup without network and record an `offline-install` Acceptance report.\n"
    install += "\nThis archive does not make a target qualified until that external acceptance passes.\n"
    partial = output.with_name(f".{output.name}.{os.getpid()}.partial")
    partial.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(partial, "w", allowZip64=True) as archive:
            archive.writestr("airgap_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            archive.writestr("INSTALL.md", install)
            archive.write(runtime, manifest["runtime"]["path"], compress_type=zipfile.ZIP_STORED)
            for item in content_records:
                archive.write(item["source"], item["archive_path"], compress_type=zipfile.ZIP_STORED)
            for source, archive_path, _ in model_entries:
                archive.write(source, archive_path, compress_type=zipfile.ZIP_STORED)
            for source, archive_path in checked_oci:
                archive.write(source, archive_path, compress_type=zipfile.ZIP_STORED)
        os.replace(partial, output)
    finally:
        partial.unlink(missing_ok=True)
    return {
        "status": "materialized",
        "path": str(output),
        "sha256": _sha256(output),
        "size_bytes": output.stat().st_size,
        "content_pack_count": len(content_records),
        "model_count": len(model_records),
        "oci_archive_count": len(oci_records),
        "qualification_claimed": False,
    }


def _mapping(values: list[str], label: str) -> dict[str, Path]:
    result = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"airgap_{label}_mapping_required")
        key, path = value.split("=", 1)
        if not key or key in result:
            raise ValueError(f"airgap_{label}_mapping_invalid")
        result[key] = Path(path)
    return result


def _save_images(image_refs: list[str], staging: Path) -> dict[str, Path]:
    result = {}
    for index, image_ref in enumerate(image_refs, start=1):
        name = f"image-{index:02d}"
        destination = staging / f"{name}.tar"
        try:
            subprocess.run(["docker", "image", "inspect", image_ref], check=True, capture_output=True)
            subprocess.run(["docker", "save", "-o", str(destination), image_ref], check=True, capture_output=True)
        except FileNotFoundError as error:
            raise ValueError("airgap_docker_not_available") from error
        except subprocess.CalledProcessError as error:
            detail = error.stderr.decode("utf-8", errors="replace")[-1000:]
            raise ValueError(f"airgap_docker_image_export_failed:{image_ref}:{detail}") from error
        result[name] = destination
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--content-pack", action="append", default=[], type=Path)
    parser.add_argument("--model", action="append", default=[], help="Reviewed model_id=/path/to/model")
    image_group = parser.add_mutually_exclusive_group(required=True)
    image_group.add_argument("--oci-archive", action="append", help="name=/path/to/docker-save.tar")
    image_group.add_argument("--oci-image", action="append", help="Local Docker image reference to export")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="spatial-airgap-oci-") as temporary:
        oci = (
            _mapping(args.oci_archive or [], "oci")
            if args.oci_archive
            else _save_images(args.oci_image or [], Path(temporary))
        )
        result = materialize(
            args.runtime,
            args.content_pack,
            _mapping(args.model, "model"),
            oci,
            args.output,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
