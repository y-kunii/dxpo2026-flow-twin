#!/usr/bin/env python3
"""Carry writable Site Records and reviewed local models into an update."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    from manage_distribution_overlay import assert_distribution_stopped, backup, database_status
except ModuleNotFoundError:  # Imported as scripts.transfer_distribution_state in tests/tools.
    from scripts.manage_distribution_overlay import assert_distribution_stopped, backup, database_status


def load_distribution(root: Path) -> dict[str, object]:
    manifest_path = root / "app_package_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"distribution_manifest_not_found:{root}")
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not str(value.get("kind", "")).startswith("spatial_3dgs_"):
        raise RuntimeError(f"distribution_manifest_invalid:{root}")
    return value


def _reviewed_models(manifest: dict[str, object]) -> dict[str, dict[str, object]]:
    catalog = manifest.get("reviewed_model_catalog")
    if not isinstance(catalog, dict):
        return {}
    models = catalog.get("models")
    if not isinstance(models, list):
        return {}
    result: dict[str, dict[str, object]] = {}
    for value in models:
        if not isinstance(value, dict):
            continue
        model_id = value.get("model_id")
        storage_directory = value.get("storage_directory")
        revision = value.get("revision")
        if not all(isinstance(item, str) and item for item in (model_id, storage_directory, revision)):
            continue
        if Path(storage_directory).name != storage_directory or storage_directory in {".", ".."}:
            continue
        result[model_id] = value
    return result


def _installed_model(root: Path, model: dict[str, object]) -> bool:
    provenance_path = root / "SPATIAL_MODEL_PROVENANCE.json"
    config_path = root / "config.json"
    if not root.is_dir() or not config_path.is_file() or not provenance_path.is_file():
        return False
    if root.is_symlink() or any(path.is_symlink() for path in root.rglob("*")):
        raise RuntimeError(f"distribution_model_symlink_not_allowed:{root.name}")
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"distribution_model_provenance_invalid:{root.name}") from error
    return bool(
        isinstance(provenance, dict)
        and provenance.get("model_id") == model["model_id"]
        and provenance.get("revision") == model["revision"]
        and any(root.rglob("*.safetensors"))
    )


def transfer_reviewed_models(
    source_root: Path,
    target_root: Path,
    source_manifest: dict[str, object],
    target_manifest: dict[str, object],
) -> list[dict[str, object]]:
    """Copy only complete, pinned models accepted by the new distribution."""

    source_catalog = _reviewed_models(source_manifest)
    target_catalog = _reviewed_models(target_manifest)
    transferred: list[dict[str, object]] = []
    target_models_root = target_root / "models"
    target_models_root.mkdir(parents=True, exist_ok=True)
    for model_id, target_model in target_catalog.items():
        source_model = source_catalog.get(model_id)
        if source_model is None or source_model.get("revision") != target_model.get("revision"):
            continue
        source = source_root / "models" / str(source_model["storage_directory"])
        if not _installed_model(source, source_model):
            continue
        destination = target_models_root / str(target_model["storage_directory"])
        if _installed_model(destination, target_model):
            transferred.append({
                "model_id": model_id,
                "revision": target_model["revision"],
                "storage_directory": target_model["storage_directory"],
                "status": "already_installed",
            })
            continue
        if destination.exists():
            allowed_placeholders = {"README.md", ".distribution-model-store"}
            if destination.is_symlink() or not destination.is_dir():
                raise RuntimeError(f"target_distribution_model_path_invalid:{destination.name}")
            if {path.name for path in destination.iterdir()} - allowed_placeholders:
                raise RuntimeError(f"target_distribution_model_contains_unreviewed_files:{destination.name}")
        staged = target_models_root / f".{destination.name}.update-{uuid.uuid4().hex}"
        shutil.copytree(source, staged)
        if not _installed_model(staged, target_model):
            shutil.rmtree(staged, ignore_errors=True)
            raise RuntimeError(f"copied_distribution_model_invalid:{destination.name}")
        old = target_models_root / f".{destination.name}.update-old-{uuid.uuid4().hex}"
        if destination.exists():
            os.replace(destination, old)
        try:
            os.replace(staged, destination)
        except BaseException:
            if old.exists() and not destination.exists():
                os.replace(old, destination)
            raise
        shutil.rmtree(old, ignore_errors=True)
        transferred.append({
            "model_id": model_id,
            "revision": target_model["revision"],
            "storage_directory": target_model["storage_directory"],
            "status": "copied",
        })
    return transferred


def transfer(source_root: Path, target_root: Path) -> Path:
    source_root = source_root.expanduser().resolve()
    target_root = target_root.expanduser().resolve()
    if source_root == target_root:
        raise ValueError("distribution_state_source_and_target_must_differ")
    source_manifest = load_distribution(source_root)
    target_manifest = load_distribution(target_root)
    assert_distribution_stopped(source_root)
    assert_distribution_stopped(target_root)
    transferred_models = transfer_reviewed_models(
        source_root,
        target_root,
        source_manifest,
        target_manifest,
    )
    source_state = source_root / "state" / "site-records"
    target_state = target_root / "state" / "site-records"
    if database_status(source_state / "registry.sqlite3")["integrity"] != "ok":
        raise RuntimeError("source_distribution_overlay_database_invalid")
    if (target_state / "registry.sqlite3").is_file():
        backup(target_root)
    target_state.parent.mkdir(parents=True, exist_ok=True)
    staged = target_state.parent / f".site-records-update-{uuid.uuid4().hex}"
    if source_state.is_symlink() or any(path.is_symlink() for path in source_state.rglob("*")):
        raise RuntimeError("source_distribution_overlay_symlink_not_allowed")
    shutil.copytree(source_state, staged)
    if database_status(staged / "registry.sqlite3")["integrity"] != "ok":
        shutil.rmtree(staged, ignore_errors=True)
        raise RuntimeError("copied_distribution_overlay_database_invalid")
    old = target_state.parent / f".site-records-update-old-{uuid.uuid4().hex}"
    if target_state.exists():
        os.replace(target_state, old)
    try:
        os.replace(staged, target_state)
    except BaseException:
        if old.exists() and not target_state.exists():
            os.replace(old, target_state)
        raise
    shutil.rmtree(old, ignore_errors=True)
    record = {
        "schema_version": "1.0.0",
        "kind": "spatial_3dgs_distribution_state_transfer",
        "transferred_at": datetime.now(timezone.utc).isoformat(),
        "source_root": str(source_root),
        "source_application_id": source_manifest.get("application_id"),
        "source_application_version": source_manifest.get("application_version"),
        "target_application_id": target_manifest.get("application_id"),
        "target_application_version": target_manifest.get("application_version"),
        "database": database_status(target_state / "registry.sqlite3"),
        "reviewed_models": transferred_models,
    }
    (target_state / "state_transfer.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target_state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="source", required=True, type=Path)
    parser.add_argument("--to", dest="target", required=True, type=Path)
    args = parser.parse_args()
    state = transfer(args.source, args.target)
    record = json.loads((state / "state_transfer.json").read_text(encoding="utf-8"))
    print(json.dumps({
        "status": "verified",
        "state": str(state),
        "reviewed_models": record.get("reviewed_models", []),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
