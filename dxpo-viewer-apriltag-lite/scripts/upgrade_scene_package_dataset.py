#!/usr/bin/env python3
"""Create a v1.3 self-contained edition of an existing Scene Package."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "generation_pipeline" / "tools"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TOOLS))

from scene_dataset import (  # noqa: E402
    estimate_dataset_bytes,
    make_metric_manifest_portable,
    materialize_scene_dataset,
    publish_scene_package_tree,
    read_json,
    related_scene_package_ids,
    self_contained_package_id,
    sha256_file,
    source_fingerprint,
    write_json,
)
from validate_scene_package import validate_scene_package  # noqa: E402
from services.control_api.field_memory import FieldMemoryService  # noqa: E402
from packages.scene_repository import SceneRepositoryLayout, rebuild_catalog  # noqa: E402


SUPPORTED_LEGACY_SCHEMAS = {"1.0.0", "1.1.0", "1.2.0"}
DEFAULT_ROS_COORDINATE = {
    "handedness": "right", "x": "forward", "y": "left", "z": "up",
}
DEFAULT_VIEWER_COORDINATE = {
    "handedness": "right", "x": "right", "y": "up", "z": "backward",
}
DEFAULT_T_VIEWER_ROS = {
    "translation": [0.0, 0.0, 0.0],
    "rotation_xyzw": [-0.5, 0.5, 0.5, 0.5],
}
DEFAULT_DYNAMIC_OVERLAY = {
    "topic": "/box/relative_transform",
    "freshness": {
        "timeout_ms": 500,
        "on_timeout": "hide",
        "ordering": "ros_stamp",
    },
}


def _upgrade_manifest_contract(manifest: dict[str, object]) -> dict[str, object]:
    """Backfill the additive v1.2 coordinate contract for older packages."""

    upgraded = dict(manifest)
    project_id = upgraded.get("project_id") or upgraded.get("project_name")
    provenance = upgraded.get("provenance")
    if not isinstance(project_id, str) and isinstance(provenance, dict):
        project_id = provenance.get("project_name")
    if not isinstance(project_id, str) or not project_id:
        reconstruction_id = str(upgraded.get("reconstruction_id", "scene"))
        project_id = reconstruction_id.removesuffix("-fixed-rig")
    upgraded["project_id"] = project_id

    coordinate = dict(upgraded.get("coordinate_system", {}))
    coordinate.setdefault("right_axis", "X")
    coordinate.setdefault("up_axis", "Y")
    coordinate.setdefault("forward_axis", "Z")
    coordinate.setdefault("ros", DEFAULT_ROS_COORDINATE)
    coordinate.setdefault("viewer", DEFAULT_VIEWER_COORDINATE)
    coordinate.setdefault("T_viewer_ros", DEFAULT_T_VIEWER_ROS)
    upgraded["coordinate_system"] = coordinate
    if not isinstance(upgraded.get("dynamic_overlay"), dict) or not upgraded["dynamic_overlay"]:
        upgraded["dynamic_overlay"] = DEFAULT_DYNAMIC_OVERLAY
    return upgraded


def _source_package_ids(package: Path) -> set[str]:
    dataset = read_json(package / "dataset" / "manifest.json")
    provenance = dataset.get("provenance") if isinstance(dataset, dict) else None
    values = provenance.get("source_package_ids") if isinstance(provenance, dict) else None
    if not isinstance(values, list):
        return set()
    return {
        value for value in values
        if isinstance(value, str) and value
    }


def _existing_upgrade(root: Path, source_package_id: str) -> str | None:
    packages_root = SceneRepositoryLayout.from_root(root).scene_packages
    if not packages_root.is_dir():
        return None
    for package in sorted(packages_root.iterdir()):
        if not package.is_dir() or package.is_symlink():
            continue
        manifest = read_json(package / "scene_manifest.json")
        if not isinstance(manifest, dict) or manifest.get("schema_version") != "1.3.0":
            continue
        if source_package_id in _source_package_ids(package):
            return package.name
        provenance = manifest.get("provenance")
        dataset_provenance = provenance.get("dataset") if isinstance(provenance, dict) else None
        if (
            isinstance(dataset_provenance, dict)
            and dataset_provenance.get("source_scene_package_id") == source_package_id
        ):
            return package.name
    return None


def audit_upgrade(
    root: Path,
    source_package_id: str,
    *,
    person_mask_root: Path | None = None,
    colmap_mask_root: Path | None = None,
    mask_report_path: Path | None = None,
) -> dict[str, object]:
    """Resolve one upgrade without changing package, runtime, or registry state."""

    root = root.resolve()
    layout = SceneRepositoryLayout.from_root(root)
    source = layout.scene_packages / source_package_id
    manifest = read_json(source / "scene_manifest.json")
    failures: list[str] = []
    if not source.is_dir() or source.is_symlink() or not isinstance(manifest, dict):
        failures.append(f"scene_package_not_found:{source_package_id}")
        return {
            "status": "blocked",
            "source_scene_package_id": source_package_id,
            "failure_reasons": failures,
        }
    if manifest.get("scene_package_id") != source_package_id:
        failures.append("scene_package_identity_mismatch")
    schema_version = manifest.get("schema_version")
    if schema_version == "1.3.0":
        return {
            "status": "already_self_contained",
            "source_scene_package_id": source_package_id,
            "scene_package_id": source_package_id,
            "schema_version": schema_version,
            "estimated_dataset_bytes": 0,
            "failure_reasons": [],
        }
    if schema_version not in SUPPORTED_LEGACY_SCHEMAS:
        failures.append(f"unsupported_legacy_schema:{schema_version}")

    existing = _existing_upgrade(root, source_package_id)
    if existing is not None:
        return {
            "status": "already_upgraded",
            "source_scene_package_id": source_package_id,
            "scene_package_id": existing,
            "schema_version": schema_version,
            "estimated_dataset_bytes": 0,
            "failure_reasons": failures,
        }

    scene_id = manifest.get("scene_id")
    reconstruction_id = manifest.get("reconstruction_id")
    if not isinstance(scene_id, str) or not isinstance(reconstruction_id, str):
        failures.append("scene_package_reconstruction_identity_missing")
        return {
            "status": "blocked",
            "source_scene_package_id": source_package_id,
            "schema_version": schema_version,
            "failure_reasons": failures,
        }
    mrp = layout.metric_reconstructions / scene_id / reconstruction_id
    if not (mrp / "manifest.json").is_file():
        failures.append(f"metric_reconstruction_package_missing:{mrp.relative_to(root).as_posix()}")

    splats = manifest.get("assets", {}).get("splats", [])
    primary = splats[0] if isinstance(splats, list) and splats else None
    asset_hash = primary.get("sha256") if isinstance(primary, dict) else None
    if not isinstance(asset_hash, str) or len(asset_hash) != 64:
        failures.append("scene_package_primary_splat_hash_missing")

    target_package_id: str | None = None
    estimated = 0
    if not failures:
        try:
            fingerprint = source_fingerprint(
                root,
                mrp,
                person_mask_root=person_mask_root,
                colmap_mask_root=colmap_mask_root,
                mask_report_path=mask_report_path,
            )
            target_package_id = self_contained_package_id(reconstruction_id, asset_hash, fingerprint)
            estimated = estimate_dataset_bytes(
                root,
                mrp,
                person_mask_root=person_mask_root,
                colmap_mask_root=colmap_mask_root,
            )
            if (layout.scene_packages / target_package_id).exists():
                failures.append(f"target_scene_package_collision:{target_package_id}")
        except (OSError, ValueError, json.JSONDecodeError) as error:
            failures.append(str(error))
    return {
        "status": "ready" if not failures else "blocked",
        "source_scene_package_id": source_package_id,
        "scene_package_id": target_package_id,
        "schema_version": schema_version,
        "scene_id": scene_id,
        "reconstruction_id": reconstruction_id,
        "estimated_dataset_bytes": estimated,
        "failure_reasons": failures,
    }


def audit_legacy_packages(root: Path) -> dict[str, object]:
    root = root.resolve()
    packages_root = SceneRepositoryLayout.from_root(root).scene_packages
    entries: list[dict[str, object]] = []
    for package in sorted(packages_root.iterdir() if packages_root.is_dir() else []):
        if not package.is_dir() or package.is_symlink() or package.name.startswith("."):
            continue
        manifest = read_json(package / "scene_manifest.json")
        if not isinstance(manifest, dict) or manifest.get("schema_version") == "1.3.0":
            continue
        entries.append(audit_upgrade(root, package.name))
    counts: dict[str, int] = {}
    for entry in entries:
        status = str(entry["status"])
        counts[status] = counts.get(status, 0) + 1
    unique_targets = {
        str(entry["scene_package_id"]): int(entry.get("estimated_dataset_bytes", 0))
        for entry in entries
        if entry.get("status") == "ready" and isinstance(entry.get("scene_package_id"), str)
    }
    return {
        "status": "blocked" if counts.get("blocked") else "verified",
        "legacy_package_count": len(entries),
        "counts": counts,
        "unique_upgrade_count": len(unique_targets),
        "estimated_dataset_bytes": sum(unique_targets.values()),
        "packages": entries,
    }


def upgrade(
    root: Path,
    source_package_id: str,
    *,
    publish: bool = True,
    person_mask_root: Path | None = None,
    colmap_mask_root: Path | None = None,
    mask_report_path: Path | None = None,
    register_scene_version: bool = False,
) -> dict[str, object]:
    root = root.resolve()
    layout = SceneRepositoryLayout.from_root(root)
    source = layout.scene_packages / source_package_id
    manifest_path = source / "scene_manifest.json"
    if not manifest_path.is_file() or source.is_symlink():
        raise FileNotFoundError(f"scene_package_not_found:{source_package_id}")
    manifest = read_json(manifest_path)
    if manifest.get("scene_package_id") != source_package_id:
        raise ValueError("scene_package_identity_mismatch")
    scene_id = manifest.get("scene_id")
    reconstruction_id = manifest.get("reconstruction_id")
    if not isinstance(scene_id, str) or not isinstance(reconstruction_id, str):
        raise ValueError("scene_package_reconstruction_identity_missing")
    splats = manifest.get("assets", {}).get("splats", [])
    primary = splats[0] if isinstance(splats, list) and splats else None
    asset_hash = primary.get("sha256") if isinstance(primary, dict) else None
    if not isinstance(asset_hash, str) or len(asset_hash) != 64:
        raise ValueError("scene_package_primary_splat_hash_missing")

    mrp = layout.metric_reconstructions / scene_id / reconstruction_id
    fingerprint = source_fingerprint(
        root,
        mrp,
        person_mask_root=person_mask_root,
        colmap_mask_root=colmap_mask_root,
        mask_report_path=mask_report_path,
    )
    target_package_id = self_contained_package_id(reconstruction_id, asset_hash, fingerprint)
    canonical = layout.scene_packages / target_package_id
    runtime = root / "web_viewer" / "app" / "public" / "scene_package" / "runtime" / target_package_id
    if canonical.exists() or (publish and runtime.exists()):
        raise FileExistsError(f"self_contained_scene_package_exists:{target_package_id}")

    estimated = estimate_dataset_bytes(
        root,
        mrp,
        person_mask_root=person_mask_root,
        colmap_mask_root=colmap_mask_root,
    )
    required = estimated
    free = shutil.disk_usage(root).free
    if free < required:
        raise OSError(f"self_contained_scene_package_insufficient_storage:free={free}:required={required}")

    token = uuid.uuid4().hex
    staging = canonical.with_name(f".{target_package_id}.dataset.{token}")
    runtime_staging = runtime.with_name(f".{target_package_id}.publish.{token}")
    publication_storage: dict[str, object] | None = None
    try:
        shutil.copytree(source, staging, ignore=shutil.ignore_patterns("dataset"))
        root_metric_manifest = staging / "metric_reconstruction" / "manifest.json"
        if root_metric_manifest.is_file():
            make_metric_manifest_portable(root_metric_manifest)
        source_ids = related_scene_package_ids(root, scene_id, reconstruction_id)
        if source_package_id not in source_ids:
            source_ids.append(source_package_id)
        materialize_scene_dataset(
            root,
            mrp,
            staging / "dataset",
            scene_id=scene_id,
            reconstruction_id=reconstruction_id,
            scene_package_id=target_package_id,
            source_package_ids=sorted(set(source_ids)),
            person_mask_root=person_mask_root,
            colmap_mask_root=colmap_mask_root,
            mask_report_path=mask_report_path,
        )
        upgraded = _upgrade_manifest_contract(manifest)
        upgraded["schema_version"] = "1.3.0"
        upgraded["scene_package_id"] = target_package_id
        upgraded["dataset"] = {
            "manifest_uri": "dataset/manifest.json",
            "manifest_sha256": sha256_file(staging / "dataset" / "manifest.json"),
        }
        if root_metric_manifest.is_file():
            upgraded.setdefault("metric_reconstruction", {})["manifest_sha256"] = sha256_file(
                root_metric_manifest
            )
        provenance = dict(upgraded.get("provenance", {}))
        provenance["dataset"] = {
            "kind": "self_contained",
            "source_scene_package_id": source_package_id,
            "source_fingerprint": fingerprint,
        }
        upgraded["provenance"] = provenance
        write_json(staging / "scene_manifest.json", upgraded)
        validation = validate_scene_package(staging, scene_id)
        if validation.get("status") != "verified":
            raise ValueError(
                "self_contained_scene_package_validation_failed:"
                + ",".join(str(item) for item in validation.get("failure_reasons", []))
            )
        canonical.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, canonical)

        if publish:
            publication_storage = publish_scene_package_tree(canonical, runtime_staging)
            runtime_validation = validate_scene_package(runtime_staging, scene_id)
            if runtime_validation.get("status") != "verified":
                raise ValueError("self_contained_scene_package_runtime_validation_failed")
            runtime.parent.mkdir(parents=True, exist_ok=True)
            os.replace(runtime_staging, runtime)
            refresh = root / "web_viewer" / "app" / "scripts" / "refresh_scene_package_index.mjs"
            if refresh.is_file():
                subprocess.run(["node", str(refresh)], cwd=root / "web_viewer" / "app", check=True)
        scene_version_registration: dict[str, object] | None = None
        if register_scene_version:
            dataset = read_json(canonical / "dataset" / "manifest.json")
            field = dataset.get("artifacts", {}).get("field_memory", {})
            target_scene_version_id = field.get("scene_version_id") if isinstance(field, dict) else None
            field_db = layout.site_registry
            if not isinstance(target_scene_version_id, str) or not field_db.is_file():
                raise ValueError("scene_package_scene_version_registration_source_missing")
            with sqlite3.connect(field_db) as connection:
                marks = ",".join("?" for _ in source_ids)
                source_scene = connection.execute(
                    f"SELECT site_id, label, captured_at FROM scene_versions "
                    f"WHERE scene_package_id IN ({marks}) ORDER BY created_at DESC LIMIT 1",
                    source_ids,
                ).fetchone()
            if source_scene is None:
                raise ValueError("scene_package_source_scene_version_missing")
            field_memory = FieldMemoryService(
                root,
                scene_packages_provider=lambda: [{
                    "scene_package_id": target_package_id,
                    "scene_id": scene_id,
                    "status": "verified",
                }],
            )
            scene_version_registration = field_memory.create_scene_version(
                str(source_scene[0]),
                {
                    "scene_version_id": target_scene_version_id,
                    "scene_package_id": target_package_id,
                    "label": f"{source_scene[1]} / person-mask edition",
                    "captured_at": source_scene[2],
                },
            )
        repository_catalog = rebuild_catalog(root)
        return {
            "status": "verified",
            "source_scene_package_id": source_package_id,
            "scene_package_id": target_package_id,
            "canonical_uri": canonical.relative_to(root).as_posix(),
            "runtime_uri": runtime.relative_to(root).as_posix() if publish else None,
            "publication_storage": publication_storage if publish else None,
            "dataset_bytes": sum(path.stat().st_size for path in (canonical / "dataset").rglob("*") if path.is_file()),
            "validation": validation,
            "scene_version_registration": scene_version_registration,
            "repository_catalog": repository_catalog,
        }
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(runtime_staging, ignore_errors=True)
        if canonical.exists() and not (publish and runtime.exists()):
            shutil.rmtree(canonical, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--source-package")
    parser.add_argument("--all-legacy", action="store_true")
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--person-mask-root", type=Path)
    parser.add_argument("--colmap-mask-root", type=Path)
    parser.add_argument("--mask-report", type=Path)
    parser.add_argument("--register-scene-version", action="store_true")
    parser.add_argument("--no-publish", action="store_true")
    args = parser.parse_args()
    if bool(args.source_package) == bool(args.all_legacy):
        parser.error("specify exactly one of --source-package or --all-legacy")
    try:
        if args.all_legacy:
            audit = audit_legacy_packages(args.repo_root)
            if args.audit:
                result = audit
            else:
                results: list[dict[str, object]] = []
                for entry in audit["packages"]:
                    if entry["status"] != "ready":
                        results.append(entry)
                        continue
                    current = audit_upgrade(args.repo_root, str(entry["source_scene_package_id"]))
                    if current["status"] != "ready":
                        results.append(current)
                        continue
                    results.append(upgrade(
                        args.repo_root,
                        str(entry["source_scene_package_id"]),
                        publish=not args.no_publish,
                        register_scene_version=args.register_scene_version,
                    ))
                result = {
                    "status": "partial" if audit["status"] == "blocked" else "verified",
                    "audit": audit,
                    "results": results,
                }
        elif args.audit:
            result = audit_upgrade(
                args.repo_root,
                args.source_package,
                person_mask_root=args.person_mask_root,
                colmap_mask_root=args.colmap_mask_root,
                mask_report_path=args.mask_report,
            )
        else:
            result = upgrade(
                args.repo_root,
                args.source_package,
                publish=not args.no_publish,
                person_mask_root=args.person_mask_root,
                colmap_mask_root=args.colmap_mask_root,
                mask_report_path=args.mask_report,
                register_scene_version=args.register_scene_version,
            )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "failed", "failure_reasons": [str(error)]}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
