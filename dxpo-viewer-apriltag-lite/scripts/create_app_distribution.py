#!/usr/bin/env python3
"""Create a feature-restricted Docker ZIP for one Spatial 3DGS application.

The suite source intentionally shares contracts and implementation modules.
The generated package fixes one Web entry point and starts the local API with
an explicit feature allowlist. Canonical working data is not copied implicitly;
only explicitly selected, verified Scene Packages may be included as immutable,
self-contained datasets shared by the bundled applications and Viewer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if not __package__ and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.create_viewer_distribution import (
    DistributionError,
    create_distribution as create_viewer_distribution,
    index_entry as viewer_index_entry,
    verified_package,
)
from packages.distribution_contracts import (
    application_runtime_contract,
    build_provenance,
    create_scene_content_pack,
    normalize_package_recipe,
)
from packages.scene_repository import SceneRepositoryLayout
from services.control_api.field_memory import (
    FieldMemoryService,
    SEARCH_INDEX_REBUILD_SQL,
)
from services.vlm_model_catalog import (
    DEFAULT_VLM,
    REVIEWED_VLMS,
    VRAM_RECOMMENDATIONS,
)
from generation_pipeline.tools.validate_scene_package import validate_scene_package

APP_VERSION = application_runtime_contract()["version"]
ProgressCallback = Callable[[str, int, int, str], None]
CancelCallback = Callable[[], bool]


class DistributionCancelled(DistributionError):
    """Raised at a safe file boundary when an operator cancels a build."""


def canonical_json_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _report_progress(
    callback: ProgressCallback | None,
    stage: str,
    current: int,
    total: int,
    message: str,
) -> None:
    if callback is not None:
        callback(stage, current, max(total, 1), message)


def _require_not_cancelled(callback: CancelCallback | None) -> None:
    if callback is not None and callback():
        raise DistributionCancelled("application_distribution_cancelled")


APP_SPECS: dict[str, dict[str, Any]] = {
    "suite": {
        "label": "Spatial 3DGS 統合TOP",
        "feature": "suite",
        "needs_gpu": True,
        "needs_docker_socket": True,
        "needs_camera": True,
        "supports_rosbridge": True,
        "reads": [
            "data/scene_repository/assets/source_files",
            "var/spatial",
            "data/scene_repository/packages/metric_reconstructions",
            "data/scene_repository/packages/scenes",
            "data/scene_repository/registries/site_records",
            "local VLM model",
        ],
        "writes": [
            "var/spatial",
            "data/scene_repository/index/catalog.sqlite3",
            "data/scene_repository/packages/metric_reconstructions",
            "data/scene_repository/packages/scenes",
            "data/scene_repository/registries/site_records",
            "data/scene_repository/deliverables/applications",
        ],
        "include_image_feature_worker": True,
        "include_frame_vlm_worker": True,
    },
    "colmap": {
        "label": "3D再構成（COLMAP）",
        "feature": "pipeline",
        "needs_gpu": True,
        "needs_docker_socket": True,
        "needs_camera": False,
        "supports_rosbridge": False,
        "reads": [
            "data/scene_repository/assets/source_files",
            "var/spatial/workspaces",
        ],
        "writes": [
            "var/spatial",
            "data/scene_repository/index/catalog.sqlite3",
            "data/scene_repository/packages/metric_reconstructions",
        ],
    },
    "apriltag-check": {
        "label": "AprilTagカメラ確認",
        "feature": "apriltag-check",
        "needs_gpu": False,
        "needs_camera": True,
        "supports_rosbridge": True,
        "reads": [],
        "writes": [],
    },
    "spz-import": {
        "label": "3DGSデータ登録",
        "feature": "spz-import",
        "needs_gpu": False,
        "needs_camera": False,
        "supports_rosbridge": False,
        "reads": ["data/scene_repository/packages/metric_reconstructions"],
        "writes": [
            "data/scene_repository/packages/scenes",
            "web_viewer/app/public/scene_package/runtime",
        ],
    },
    "scene-manager": {
        "label": "シーンデータ管理",
        "feature": "scene-manager",
        "needs_gpu": False,
        "needs_camera": False,
        "supports_rosbridge": False,
        "reads": [
            "data/scene_repository",
            "web_viewer/app/public/scene_package/runtime",
            "bundled verified Scene Packages",
        ],
        "writes": [
            "var/spatial/trash/repository",
            "var/spatial/trash/scene_packages",
            "data/scene_repository/index/catalog.sqlite3",
            "web_viewer/app/public/scene_package/distribution_index.json",
        ],
    },
    "project-manager": {
        "label": "処理履歴・成果物管理",
        "feature": "project-manager",
        "needs_gpu": False,
        "needs_camera": False,
        "supports_rosbridge": False,
        "reads": [
            "data/scene_repository/index/catalog.sqlite3",
            "var/spatial/workspaces",
            "data/scene_repository/packages/metric_reconstructions",
            "data/scene_repository/packages/scenes",
        ],
        "writes": ["delete only terminal, unreferenced failed project workspaces"],
    },
    "package-builder": {
        "label": "配布アプリ作成",
        "feature": "package-builder",
        "needs_gpu": False,
        "needs_camera": False,
        "supports_rosbridge": False,
        "reads": [
            "application source and distribution contracts",
            "data/scene_repository/packages/scenes",
        ],
        "writes": ["data/scene_repository/deliverables/applications"],
    },
    "field-memory": {
        "label": "現場・設備台帳",
        "feature": "field-memory",
        "needs_gpu": False,
        "needs_camera": False,
        "supports_rosbridge": False,
        "reads": [
            "data/scene_repository/packages/scenes",
            "data/scene_repository/registries/site_records",
        ],
        "writes": ["data/scene_repository/registries/site_records"],
    },
    "site-insights": {
        "label": "現場インサイト",
        "feature": ["field-memory", "spatial-search", "evidence-ai", "change-review"],
        "needs_gpu": True,
        "needs_docker_socket": False,
        "needs_camera": False,
        "supports_rosbridge": False,
        "reads": [
            "data/scene_repository/registries/site_records",
            "bundled verified Scene Packages",
            "local VLM model",
        ],
        "writes": [
            "var/spatial/cache/site_records",
            "data/scene_repository/registries/site_records",
        ],
        "include_image_feature_worker": True,
        "include_frame_vlm_worker": True,
    },
    "frame-analysis": {
        "label": "登録画像AI解析",
        "feature": ["frame-analysis", "spatial-search"],
        "needs_gpu": True,
        "needs_docker_socket": False,
        "needs_camera": False,
        "supports_rosbridge": False,
        "reads": [
            "self-contained Scene Package dataset",
            "data/scene_repository/registries/site_records writable overlay",
            "local VLM model",
        ],
        "writes": ["data/scene_repository/registries/site_records"],
        "include_frame_vlm_worker": True,
    },
    "viewer": {
        "label": "3Dシーン閲覧",
        "feature": None,
        "needs_gpu": False,
        "needs_camera": False,
        "supports_rosbridge": True,
        "reads": ["bundled verified Scene Packages"],
        "writes": [],
    },
}
ALREADY_COMPRESSED_SUFFIXES = {".gz", ".spz", ".zip", ".rad", ".glb"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    if path.exists():
        path.chmod(path.stat().st_mode | stat.S_IWUSR)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def normalize_bundled_scene_package_metadata(package: Path) -> None:
    """Remove build-host paths from a copied v1.3 Scene Package.

    Large image/COLMAP/SPZ artifacts remain byte-for-byte identical.  Only
    portable JSON metadata and its explicit hash chain are regenerated inside
    the distribution staging area.
    """

    scene_manifest_path = package / "scene_manifest.json"
    scene_manifest = json.loads(scene_manifest_path.read_text(encoding="utf-8"))
    if scene_manifest.get("schema_version") != "1.3.0":
        return
    dataset_path = package / str(scene_manifest["dataset"]["manifest_uri"])
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    artifacts = dataset["artifacts"]

    report_path = dataset_path.parent / str(artifacts["mask_generation"]["report_uri"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["outputs"] = {
        "person_masks": "package://dataset/frames/person_masks",
        "colmap_masks": "package://dataset/frames/colmap_masks",
    }
    _write_json(report_path, report)
    artifacts["mask_generation"]["report_sha256"] = sha256(report_path)

    portable_config = {
        "schema_version": "1.0.0",
        "kind": "portable_scene_dataset_config",
        "scene_id": dataset["scene_id"],
        "reconstruction_id": dataset["reconstruction_id"],
        "scene_package_id": dataset["scene_package_id"],
        "frame_interval_sec": dataset["frame_interval_sec"],
        "registered_frames_uri": "package://dataset/frames/images",
        "person_mask": {
            "backend": artifacts["mask_generation"].get("backend"),
            "status": artifacts["mask_generation"].get("status"),
            "report_uri": "package://dataset/config/person_mask_report.json",
            "person_masks_uri": "package://dataset/frames/person_masks",
            "colmap_masks_uri": "package://dataset/frames/colmap_masks",
        },
        "colmap": {
            "database_uri": "package://dataset/colmap/database.db",
            "text_uri": "package://dataset/colmap/text",
            "sparse_uri": "package://dataset/colmap/sparse",
        },
        "field_memory": {
            "database_uri": "package://dataset/field_memory/field_memory.sqlite3",
        },
    }
    _write_json(dataset_path.parent / "config/scene.json", portable_config)

    for metric_path in (
        package / str(scene_manifest["metric_reconstruction"]["manifest_uri"]),
        dataset_path.parent / str(artifacts["metric_reconstruction"]["manifest_uri"]),
    ):
        metric = json.loads(metric_path.read_text(encoding="utf-8"))
        provenance = metric.setdefault("provenance", {})
        provenance.pop("source_workspace_uri", None)
        provenance["source_workspace_included"] = False
        provenance["portable_package_uri"] = "package://dataset/metric_reconstruction"
        _write_json(metric_path, metric)

    scene_metric_path = package / str(
        scene_manifest["metric_reconstruction"]["manifest_uri"]
    )
    dataset_metric_path = dataset_path.parent / str(
        artifacts["metric_reconstruction"]["manifest_uri"]
    )
    scene_manifest["metric_reconstruction"]["manifest_sha256"] = sha256(
        scene_metric_path
    )
    artifacts["metric_reconstruction"]["manifest_sha256"] = sha256(dataset_metric_path)
    provenance = dataset.setdefault("provenance", {})
    provenance.pop("source_workspace_uri", None)
    provenance["source_workspace_included"] = False
    provenance["portable_package_uri"] = "package://dataset"
    dataset["distribution_contract"] = {
        "portable": True,
        "host_workspace_required": False,
        "uri_scheme": "package://",
    }
    _write_json(dataset_path, dataset)
    scene_manifest["dataset"]["manifest_sha256"] = sha256(dataset_path)
    _write_json(scene_manifest_path, scene_manifest)


def _portable_package_reference(package: Path, uri: object, label: str) -> tuple[Path, Path]:
    if not isinstance(uri, str) or not uri or "\\" in uri:
        raise DistributionError(f"display_projection_{label}_uri_invalid")
    relative = Path(uri)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise DistributionError(f"display_projection_{label}_uri_invalid")
    source = package / relative
    if source.is_symlink() or not source.is_file():
        raise DistributionError(f"display_projection_{label}_missing:{uri}")
    try:
        source.resolve().relative_to(package.resolve())
    except ValueError as error:
        raise DistributionError(f"display_projection_{label}_outside_package") from error
    return source, relative


def display_scene_package_projection_plan(
    package: Path,
    manifest: dict[str, Any],
) -> list[tuple[Path, Path]]:
    """Return only files needed by the full/simple Viewer and AprilTag UI."""

    if manifest.get("schema_version") != "1.3.0":
        return [
            (path, path.relative_to(package))
            for path in package.rglob("*")
            if path.is_file()
        ]
    references: list[tuple[object, str]] = [("scene_manifest.json", "manifest")]
    assets = manifest.get("assets") if isinstance(manifest.get("assets"), dict) else {}
    splats = assets.get("splats") if isinstance(assets, dict) else None
    if not isinstance(splats, list) or not splats:
        raise DistributionError("display_projection_splat_missing")
    references.extend(
        (asset.get("uri"), f"splat_{index}")
        for index, asset in enumerate(splats)
        if isinstance(asset, dict)
    )
    metric = manifest.get("metric_reconstruction")
    if not isinstance(metric, dict):
        raise DistributionError("display_projection_metric_manifest_missing")
    references.append((metric.get("manifest_uri"), "metric_manifest"))
    configs = manifest.get("configs") if isinstance(manifest.get("configs"), dict) else {}
    for key in ("world_alignment", "camera_presets"):
        references.append((configs.get(key), f"config_{key}"))
    annotations = manifest.get("annotations") if isinstance(manifest.get("annotations"), dict) else {}
    for key in ("apriltag_corners_world", "fixed_tag_map"):
        if key == "fixed_tag_map" and key not in annotations:
            continue
        references.append((annotations.get(key), f"annotation_{key}"))
    licenses = manifest.get("licenses") if isinstance(manifest.get("licenses"), dict) else {}
    references.append((licenses.get("notice"), "license_notice"))

    planned: dict[str, tuple[Path, Path]] = {}
    for uri, label in references:
        source, relative = _portable_package_reference(package, uri, label)
        planned[relative.as_posix()] = (source, relative)
    return [planned[key] for key in sorted(planned)]


def display_scene_package_projection_size(
    package: Path,
    manifest: dict[str, Any],
) -> int:
    return sum(source.stat().st_size for source, _ in display_scene_package_projection_plan(package, manifest))


def project_display_scene_package(
    package: Path,
    destination: Path,
    manifest: dict[str, Any],
    *,
    copy_function: Callable[[str, str], str],
) -> dict[str, Any]:
    """Create a verified v1.2 display projection without analysis datasets."""

    for source, relative in display_scene_package_projection_plan(package, manifest):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        copy_function(str(source), str(target))
    if manifest.get("schema_version") != "1.3.0":
        return json.loads((destination / "scene_manifest.json").read_text(encoding="utf-8"))

    projected = json.loads(json.dumps(manifest))
    projected["schema_version"] = "1.2.0"
    projected.pop("created_at", None)
    projected.pop("captured_at", None)
    projected.pop("dataset", None)
    projected_assets = projected.get("assets", {})
    projected_assets["proxy_mesh"] = None
    projected_assets["human_models"] = []
    for asset in projected_assets.get("splats", []):
        if isinstance(asset, dict):
            asset.pop("metadata_uri", None)
    projected["configs"] = {
        key: value
        for key, value in projected.get("configs", {}).items()
        if key in {"world_alignment", "camera_presets"}
    }
    projected["annotations"] = {
        key: value
        for key, value in projected.get("annotations", {}).items()
        if key in {"apriltag_corners_world", "fixed_tag_map"}
    }
    capabilities = projected.setdefault("capabilities", {})
    capabilities.update({
        "display": True,
        "measurement": False,
        "surface_placement": False,
        "fov": False,
        "occlusion": False,
    })
    projected.setdefault("provenance", {})["distribution_projection"] = {
        "kind": "display_apriltag_only",
        "source_schema_version": "1.3.0",
        "self_contained_dataset_included": False,
        "omitted": ["registered_images", "person_masks", "colmap_masks", "colmap_database"],
    }
    _write_json(destination / "scene_manifest.json", projected)
    return projected


def frame_analysis_mask_contract(
    package: Path, *, required: bool
) -> dict[str, Any] | None:
    scene_manifest = json.loads(
        (package / "scene_manifest.json").read_text(encoding="utf-8")
    )
    dataset_reference = scene_manifest.get("dataset")
    if not isinstance(dataset_reference, dict):
        if required:
            raise DistributionError("frame_analysis_scene_dataset_missing")
        return None
    dataset_path = package / str(dataset_reference.get("manifest_uri", ""))
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    artifacts = (
        dataset.get("artifacts") if isinstance(dataset.get("artifacts"), dict) else {}
    )
    registered = (
        artifacts.get("registered_frames") if isinstance(artifacts, dict) else None
    )
    people = artifacts.get("person_masks") if isinstance(artifacts, dict) else None
    generation = (
        artifacts.get("mask_generation") if isinstance(artifacts, dict) else None
    )
    colmap_masks = (
        artifacts.get("colmap_masks") if isinstance(artifacts, dict) else None
    )
    usable = bool(
        dataset.get("schema_version") == "1.1.0"
        and isinstance(registered, dict)
        and isinstance(people, dict)
        and registered.get("file_count") == people.get("file_count")
        and int(people.get("file_count", 0)) > 0
        and people.get("convention") == "white_is_person"
        and isinstance(generation, dict)
        and generation.get("backend") == "grounding_dino_sam2"
        and generation.get("status") == "verified"
    )
    if required and not usable:
        raise DistributionError(
            "frame_analysis_requires_verified_grounding_dino_sam2_person_masks"
        )
    if not usable:
        return None
    return {
        "status": "verified",
        "backend": "grounding_dino_sam2",
        "person_mask_uri": "dataset/frames/person_masks",
        "person_mask_count": people["file_count"],
        "person_mask_convention": people["convention"],
        "ai_analysis_policy": "neutralize_person_pixels_only",
        "colmap_person_exclusion_masks_applied_to_packaged_database": bool(
            isinstance(colmap_masks, dict)
            and colmap_masks.get("applied_to_packaged_colmap_database") is True
        ),
        "reconstruction_mask_policy": "preserve_masks_used_by_packaged_colmap_database",
    }


def git_revision(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def copy_tree(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(
            "__pycache__",
            "*.pyc",
            "node_modules",
            "dist",
            ".vite",
            "playwright-report",
            "test-results",
            "tests",
            "runtime",
            "distribution_index.json",
        ),
    )


def copy_application_source(
    root: Path,
    staging: Path,
    *,
    include_rosbridge: bool,
) -> None:
    for directory in (
        "apps",
        "generation_pipeline",
        "packages",
        "scene_spec",
        "scripts",
        "services",
        "web_viewer",
    ):
        source = root / directory
        if not source.is_dir():
            raise DistributionError(f"required_source_missing:{directory}")
        copy_tree(source, staging / directory)

    # Historical scene-specific input configuration is not application code
    # and must not leak into a new hand-off.
    (
        staging / "generation_pipeline/config/scenes/room_office_f26_apriltag.json"
    ).unlink(missing_ok=True)

    # No generated or legacy Viewer asset is part of an application binary.
    # SPZ and Scene Packages are supplied explicitly through the app's data
    # boundary (or bundled only by the read-only Viewer distribution).
    shutil.rmtree(staging / "web_viewer/app/public/scene_package", ignore_errors=True)
    shutil.rmtree(staging / "web_viewer/app/public/assets", ignore_errors=True)
    public_scene_package = staging / "web_viewer/app/public/scene_package"
    (public_scene_package / "runtime").mkdir(parents=True)
    (public_scene_package / "distribution_index.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "kind": "spatial_3dgs_viewer_distribution_index",
                "packages": [],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    (staging / "docker").mkdir()
    docker_files = [
        "Dockerfile.generation",
        "Dockerfile.web-viewer",
        "drop_web_privileges.sh",
        "start_application_api.sh",
        "start_spatial_app.sh",
        "start_web_viewer.sh",
    ]
    if include_rosbridge:
        docker_files.extend(("Dockerfile.rosbridge", "healthcheck_rosbridge.py"))
    for name in docker_files:
        source = root / "docker" / name
        if not source.is_file():
            raise DistributionError(f"required_docker_file_missing:{name}")
        shutil.copy2(source, staging / "docker" / name)
    shutil.copy2(root / ".dockerignore", staging / ".dockerignore")

    for relative in (
        "data/scene_repository/assets/source_files",
        "data/scene_repository/deliverables",
        "var/spatial",
        "data/scene_repository/packages/metric_reconstructions",
        "data/scene_repository/packages/scenes",
        "data/scene_repository/registries/site_records",
        "web_viewer/app/public/scene_package/runtime",
    ):
        (staging / relative).mkdir(parents=True, exist_ok=True)
    distribution_state = staging / "state" / "site-records"
    distribution_state.mkdir(parents=True, exist_ok=True)
    (distribution_state / ".distribution-state").write_text(
        "Writable Site Records state. Preserve this directory during application updates.\n",
        encoding="utf-8",
    )

    # The application source is mounted read-only at /workspace while the
    # dependency tree is supplied by a named volume at the nested path below.
    # Docker cannot create that nested mountpoint through a read-only bind
    # mount on a freshly extracted distribution, so keep a real file in the
    # directory to ensure the ZIP recreates it on every host.
    node_modules_mountpoint = staging / "web_viewer/app/node_modules"
    node_modules_mountpoint.mkdir(parents=True, exist_ok=True)
    (node_modules_mountpoint / ".distribution-volume-mountpoint").write_text(
        "This directory is a Docker named-volume mountpoint.\n",
        encoding="utf-8",
    )


def bundle_scene_packages(
    root: Path,
    staging: Path,
    package_ids: list[str],
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_requested: CancelCallback | None = None,
    require_frame_analysis_masks: bool = False,
    scene_content_mode: str = "self-contained",
) -> list[dict[str, Any]]:
    """Copy explicitly selected verified packages into the read-only Viewer runtime."""
    if not package_ids:
        return []
    selected = [verified_package(root, package_id) for package_id in package_ids]
    runtime = staging / "web_viewer/app/public/scene_package/runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    index_entries: list[dict[str, Any]] = []
    manifest_entries: list[dict[str, Any]] = []
    for index, (package, manifest, validation) in enumerate(selected, start=1):
        _require_not_cancelled(cancel_requested)
        package_id = str(manifest["scene_package_id"])
        destination = runtime / package_id
        if destination.exists():
            raise DistributionError(f"bundled_scene_package_collision:{package_id}")
        if package.is_symlink() or any(
            path.is_symlink() for path in package.rglob("*")
        ):
            raise DistributionError(
                f"bundled_scene_package_symlink_not_allowed:{package_id}"
            )
        display_projection = (
            scene_content_mode == "display"
            and manifest.get("schema_version") == "1.3.0"
        )
        package_files = (
            [source for source, _ in display_scene_package_projection_plan(package, manifest)]
            if display_projection
            else [path for path in package.rglob("*") if path.is_file()]
        )
        package_bytes = sum(path.stat().st_size for path in package_files)
        copied_bytes = 0
        reported_bytes = 0
        report_threshold = max(16 * 1024 * 1024, package_bytes // 100)

        def copy_with_progress(
            source: str,
            target: str,
            *,
            total_bytes: int = package_bytes,
            progress_threshold: int = report_threshold,
            package_ordinal: int = index,
        ) -> str:
            nonlocal copied_bytes, reported_bytes
            _require_not_cancelled(cancel_requested)
            copied = shutil.copy2(source, target)
            copied_bytes += Path(source).stat().st_size
            if (
                copied_bytes == total_bytes
                or copied_bytes - reported_bytes >= progress_threshold
            ):
                reported_bytes = copied_bytes
                _report_progress(
                    progress_callback,
                    "scene_packages",
                    copied_bytes,
                    total_bytes,
                    f"Scene Package {package_ordinal}/{len(selected)}: {copied_bytes}/{total_bytes} bytes",
                )
            return copied

        if display_projection:
            destination.mkdir(parents=True)
            destination_manifest = project_display_scene_package(
                package,
                destination,
                manifest,
                copy_function=copy_with_progress,
            )
        else:
            shutil.copytree(package, destination, copy_function=copy_with_progress)
            normalize_bundled_scene_package_metadata(destination)
            destination_manifest = json.loads(
                (destination / "scene_manifest.json").read_text(encoding="utf-8")
            )
        if destination_manifest.get("schema_version") in {"1.2.0", "1.3.0"}:
            validation = validate_scene_package(
                destination, str(destination_manifest.get("scene_id", ""))
            )
            if validation.get("status") != "verified":
                reasons = ",".join(
                    str(item) for item in validation.get("failure_reasons", [])
                )
                raise DistributionError(
                    f"normalized_scene_package_not_verified:{package_id}:{reasons}"
                )
        mask_contract = frame_analysis_mask_contract(
            destination, required=require_frame_analysis_masks
        )
        (destination / "distribution_validation.json").write_text(
            json.dumps(validation, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        entry = viewer_index_entry(destination_manifest)
        index_entries.append(entry)
        manifest_entry = {
            "scene_package_id": package_id,
            "scene_id": destination_manifest.get("scene_id"),
            "reconstruction_id": destination_manifest.get("reconstruction_id"),
            "scene_manifest_sha256": sha256(destination / "scene_manifest.json"),
            "viewer_url": entry["viewer_url"],
            "scene_content_mode": "display" if display_projection else "self-contained",
        }
        if mask_contract is not None:
            manifest_entry["person_mask_contract"] = mask_contract
        manifest_entries.append(manifest_entry)
        if package_bytes == 0:
            _report_progress(
                progress_callback,
                "scene_packages",
                index,
                len(selected),
                f"Scene Package {index}/{len(selected)} を収録しました",
            )
    index_path = staging / "web_viewer/app/public/scene_package/distribution_index.json"
    index_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "kind": "spatial_3dgs_viewer_distribution_index",
                "packages": index_entries,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest_entries


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _copy_selected_rows(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    table: str,
    where_sql: str,
    parameters: tuple[Any, ...],
    *,
    upsert_key: str | None = None,
    replacements: dict[str, Any] | None = None,
) -> int:
    """Copy a filtered set using the schema intersection of two registries."""

    source_columns = [
        str(row[1])
        for row in source.execute(f"PRAGMA table_info({_quoted_identifier(table)})")
    ]
    target_columns = {
        str(row[1])
        for row in target.execute(f"PRAGMA table_info({_quoted_identifier(table)})")
    }
    columns = [column for column in source_columns if column in target_columns]
    if not columns:
        raise DistributionError(f"analysis_overlay_schema_missing:{table}")
    column_sql = ",".join(_quoted_identifier(column) for column in columns)
    rows = list(
        source.execute(
            f"SELECT {column_sql} FROM {_quoted_identifier(table)} WHERE {where_sql}",
            parameters,
        )
    )
    if not rows:
        return 0
    marks = ",".join("?" for _ in columns)
    if upsert_key is not None and upsert_key not in columns:
        raise DistributionError(
            f"analysis_overlay_upsert_key_missing:{table}:{upsert_key}"
        )
    operation = "INSERT OR IGNORE"
    conflict_sql = ""
    if upsert_key is not None:
        operation = "INSERT"
        update_columns = [column for column in columns if column != upsert_key]
        assignments = ",".join(
            f"{_quoted_identifier(column)}=excluded.{_quoted_identifier(column)}"
            for column in update_columns
        )
        conflict_sql = f" ON CONFLICT({_quoted_identifier(upsert_key)}) DO UPDATE SET {assignments}"
    replacement_values = replacements or {}
    target.executemany(
        f"{operation} INTO {_quoted_identifier(table)} ({column_sql}) VALUES ({marks})"
        f"{conflict_sql}",
        [
            tuple(replacement_values.get(column, row[column]) for column in columns)
            for row in rows
        ],
    )
    return len(rows)


def export_active_analysis_overlay(
    root: Path,
    staging: Path,
    package_ids: list[str],
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_requested: CancelCallback | None = None,
) -> dict[str, Any]:
    """Export selected packages' active analysis runs into a writable overlay.

    Scene Packages remain byte-for-byte immutable.  Only the active run for
    each selected SceneVersion and its content-addressed images are copied from
    the operational registry into the distribution-local Site Records store.
    """

    selected_ids = list(dict.fromkeys(str(item) for item in package_ids))
    created_at = datetime.now(timezone.utc).isoformat()
    overlay_manifest: dict[str, Any] = {
        "schema_version": "1.0.0",
        "kind": "spatial_3dgs_analysis_distribution_overlay",
        "created_at": created_at,
        "selection_policy": "active-run-per-selected-scene-version-v1",
        "scene_package_ids": selected_ids,
        "database_uri": "data/scene_repository/registries/site_records/registry.sqlite3",
        "attachments_uri": "data/scene_repository/registries/site_records/attachments/sha256",
        "scene_packages_immutable": True,
        "writable_at_runtime": True,
        "included": False,
        "runs": [],
        "packages_without_active_run": [],
        "attachment_count": 0,
        "attachment_bytes": 0,
    }

    destination_service = FieldMemoryService(staging)
    destination_database = destination_service.initialize_writable_overlay()
    destination_layout = SceneRepositoryLayout.from_root(staging)
    source_layout = SceneRepositoryLayout.from_root(root)
    source_database = source_layout.site_registry

    if source_database.is_file():
        source = sqlite3.connect(f"file:{source_database}?mode=ro", uri=True)
        target = sqlite3.connect(destination_database)
        source.row_factory = sqlite3.Row
        target.row_factory = sqlite3.Row
        attachment_hashes: set[str] = set()
        try:
            source.execute("PRAGMA query_only = ON")
            source.execute("BEGIN")
            if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise DistributionError("analysis_overlay_source_database_invalid")
            source_tables = {
                str(row[0])
                for row in source.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            required_tables = {
                "sites",
                "scene_versions",
                "analysis_runs",
                "frame_analyses",
                "scene_analysis_activations",
                "scene_analysis_activation_events",
            }
            missing_tables = sorted(required_tables - source_tables)
            if missing_tables:
                raise DistributionError(
                    "analysis_overlay_source_schema_missing:" + ",".join(missing_tables)
                )
            target.execute("PRAGMA foreign_keys = ON")
            target.execute("BEGIN IMMEDIATE")
            for package_index, package_id in enumerate(selected_ids, start=1):
                _require_not_cancelled(cancel_requested)
                scene = source.execute(
                    "SELECT scene_version_id, site_id FROM scene_versions "
                    "WHERE scene_package_id = ?",
                    (package_id,),
                ).fetchone()
                if scene is None:
                    overlay_manifest["packages_without_active_run"].append(
                        {
                            "scene_package_id": package_id,
                            "reason": "scene_version_not_registered",
                        }
                    )
                    continue

                scene_version_id = str(scene["scene_version_id"])
                site_id = str(scene["site_id"])
                destination_scene = target.execute(
                    "SELECT scene_version_id, site_id FROM scene_versions "
                    "WHERE scene_package_id = ?",
                    (package_id,),
                ).fetchone()
                if destination_scene is None:
                    _copy_selected_rows(
                        source, target, "sites", "site_id = ?", (site_id,)
                    )
                    _copy_selected_rows(
                        source,
                        target,
                        "scene_versions",
                        "scene_version_id = ?",
                        (scene_version_id,),
                    )
                elif (
                    str(destination_scene["scene_version_id"]) != scene_version_id
                    or str(destination_scene["site_id"]) != site_id
                ):
                    raise DistributionError(
                        f"analysis_overlay_scene_version_mismatch:{package_id}"
                    )

                activation = source.execute(
                    "SELECT analysis_run_id, activated_at FROM scene_analysis_activations "
                    "WHERE scene_version_id = ?",
                    (scene_version_id,),
                ).fetchone()
                if activation is None:
                    overlay_manifest["packages_without_active_run"].append(
                        {
                            "scene_package_id": package_id,
                            "scene_version_id": scene_version_id,
                            "reason": "active_analysis_run_not_found",
                        }
                    )
                    continue

                analysis_run_id = str(activation["analysis_run_id"])
                run = source.execute(
                    "SELECT * FROM analysis_runs WHERE analysis_run_id = ? "
                    "AND scene_version_id = ? AND site_id = ?",
                    (analysis_run_id, scene_version_id, site_id),
                ).fetchone()
                if run is None:
                    raise DistributionError(
                        f"analysis_overlay_active_run_invalid:{package_id}:{analysis_run_id}"
                    )
                _copy_selected_rows(
                    source,
                    target,
                    "analysis_runs",
                    "analysis_run_id = ?",
                    (analysis_run_id,),
                    upsert_key="analysis_run_id",
                )
                contract_count = 0
                if "analysis_run_contracts" in source_tables:
                    contract_count = _copy_selected_rows(
                        source,
                        target,
                        "analysis_run_contracts",
                        "analysis_run_id = ?",
                        (analysis_run_id,),
                        upsert_key="analysis_run_id",
                    )
                target.execute(
                    "DELETE FROM frame_observation_items WHERE observation_set_id IN ("
                    "SELECT sets.observation_set_id FROM frame_observation_sets AS sets "
                    "JOIN frame_analyses AS frames "
                    "ON frames.frame_analysis_id = sets.frame_analysis_id "
                    "WHERE frames.analysis_run_id = ?)",
                    (analysis_run_id,),
                )
                target.execute(
                    "DELETE FROM frame_observation_sets WHERE frame_analysis_id IN ("
                    "SELECT frame_analysis_id FROM frame_analyses WHERE analysis_run_id = ?)",
                    (analysis_run_id,),
                )
                target.execute(
                    "DELETE FROM frame_analysis_review_events WHERE frame_analysis_id IN ("
                    "SELECT frame_analysis_id FROM frame_analyses WHERE analysis_run_id = ?)",
                    (analysis_run_id,),
                )
                target.execute(
                    "DELETE FROM frame_analyses WHERE analysis_run_id = ?",
                    (analysis_run_id,),
                )
                frame_count = _copy_selected_rows(
                    source,
                    target,
                    "frame_analyses",
                    "analysis_run_id = ?",
                    (analysis_run_id,),
                    upsert_key="frame_analysis_id",
                )
                review_count = 0
                if "frame_analysis_review_events" in source_tables:
                    review_count = _copy_selected_rows(
                        source,
                        target,
                        "frame_analysis_review_events",
                        "frame_analysis_id IN (SELECT frame_analysis_id FROM frame_analyses "
                        "WHERE analysis_run_id = ?)",
                        (analysis_run_id,),
                        upsert_key="review_event_id",
                        # A continuation event can reference a review event in
                        # a historical run that is intentionally not exported.
                        # The effective decision remains complete in this
                        # active-run snapshot, so detach that external FK.
                        replacements={"inherited_from_review_event_id": None},
                    )
                observation_set_count = 0
                observation_item_count = 0
                if "frame_observation_sets" in source_tables:
                    observation_set_count = _copy_selected_rows(
                        source,
                        target,
                        "frame_observation_sets",
                        "frame_analysis_id IN (SELECT frame_analysis_id FROM frame_analyses "
                        "WHERE analysis_run_id = ?)",
                        (analysis_run_id,),
                        upsert_key="observation_set_id",
                    )
                if "frame_observation_items" in source_tables:
                    observation_item_count = _copy_selected_rows(
                        source,
                        target,
                        "frame_observation_items",
                        "observation_set_id IN (SELECT sets.observation_set_id "
                        "FROM frame_observation_sets AS sets JOIN frame_analyses AS frames "
                        "ON frames.frame_analysis_id = sets.frame_analysis_id "
                        "WHERE frames.analysis_run_id = ?)",
                        (analysis_run_id,),
                        upsert_key="observation_item_id",
                    )
                _copy_selected_rows(
                    source,
                    target,
                    "scene_analysis_activation_events",
                    "scene_version_id = ? AND analysis_run_id = ?",
                    (scene_version_id, analysis_run_id),
                )
                _copy_selected_rows(
                    source,
                    target,
                    "scene_analysis_activations",
                    "scene_version_id = ?",
                    (scene_version_id,),
                    upsert_key="scene_version_id",
                )
                content_rows = source.execute(
                    "SELECT image_content_sha256 AS content_sha256 FROM frame_analyses "
                    "WHERE analysis_run_id = ? UNION "
                    "SELECT vlm_input_content_sha256 FROM frame_analyses WHERE analysis_run_id = ? UNION "
                    "SELECT thumbnail_content_sha256 FROM frame_analyses WHERE analysis_run_id = ?",
                    (analysis_run_id, analysis_run_id, analysis_run_id),
                )
                attachment_hashes.update(
                    str(row["content_sha256"]).lower() for row in content_rows
                )
                overlay_manifest["runs"].append(
                    {
                        "scene_package_id": package_id,
                        "scene_version_id": scene_version_id,
                        "site_id": site_id,
                        "analysis_run_id": analysis_run_id,
                        "status": str(run["status"]),
                        "frame_count": frame_count,
                        "review_event_count": review_count,
                        "run_contract_count": contract_count,
                        "observation_set_count": observation_set_count,
                        "observation_item_count": observation_item_count,
                        "declared_output_count": int(run["output_count"]),
                        "activated_at": str(activation["activated_at"]),
                    }
                )
                _report_progress(
                    progress_callback,
                    "analysis_database",
                    package_index,
                    len(selected_ids),
                    f"解析run {package_index}/{len(selected_ids)} を抽出しました",
                )
            target.executescript(SEARCH_INDEX_REBUILD_SQL)
            target.commit()
            if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise DistributionError("analysis_overlay_destination_database_invalid")
        except BaseException:
            target.rollback()
            raise
        finally:
            target.close()
            source.close()

        total_bytes = 0
        sorted_attachment_hashes = sorted(attachment_hashes)
        for attachment_index, content_sha256 in enumerate(
            sorted_attachment_hashes, start=1
        ):
            _require_not_cancelled(cancel_requested)
            if len(content_sha256) != 64 or any(
                character not in "0123456789abcdef" for character in content_sha256
            ):
                raise DistributionError(
                    f"analysis_overlay_content_hash_invalid:{content_sha256}"
                )
            source_content = (
                source_layout.site_attachments / content_sha256[:2] / content_sha256
            )
            if not source_content.is_file() or source_content.is_symlink():
                raise DistributionError(
                    f"analysis_overlay_content_missing:{content_sha256}"
                )
            if sha256(source_content) != content_sha256:
                raise DistributionError(
                    f"analysis_overlay_content_hash_mismatch:{content_sha256}"
                )
            destination_content = (
                destination_layout.site_attachments
                / content_sha256[:2]
                / content_sha256
            )
            destination_content.parent.mkdir(parents=True, exist_ok=True)
            if not destination_content.exists():
                shutil.copy2(source_content, destination_content)
            if sha256(destination_content) != content_sha256:
                raise DistributionError(
                    f"analysis_overlay_destination_content_hash_mismatch:{content_sha256}"
                )
            total_bytes += destination_content.stat().st_size
            if (
                attachment_index == len(sorted_attachment_hashes)
                or attachment_index % 10 == 0
            ):
                _report_progress(
                    progress_callback,
                    "analysis_attachments",
                    attachment_index,
                    len(sorted_attachment_hashes),
                    f"解析画像 {attachment_index}/{len(sorted_attachment_hashes)} を収録しました",
                )
        overlay_manifest["attachment_count"] = len(attachment_hashes)
        overlay_manifest["attachment_bytes"] = total_bytes

    else:
        overlay_manifest["packages_without_active_run"] = [
            {
                "scene_package_id": package_id,
                "reason": "source_registry_not_found",
            }
            for package_id in selected_ids
        ]

    overlay_manifest["included"] = bool(overlay_manifest["runs"])
    manifest_path = destination_layout.site_records / "distribution_overlay.json"
    manifest_path.write_text(
        json.dumps(overlay_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return overlay_manifest


def estimate_active_analysis_overlay(
    root: Path, package_ids: list[str]
) -> dict[str, Any]:
    """Return a read-only, content-addressed estimate for the active overlay."""

    selected_ids = list(dict.fromkeys(str(item) for item in package_ids))
    layout = SceneRepositoryLayout.from_root(root)
    result: dict[str, Any] = {
        "scene_package_ids": selected_ids,
        "run_count": 0,
        "frame_count": 0,
        "review_event_count": 0,
        "run_contract_count": 0,
        "observation_set_count": 0,
        "observation_item_count": 0,
        "attachment_count": 0,
        "attachment_bytes": 0,
        "database_estimate_bytes": 0,
        "total_bytes": 0,
        "packages_without_active_run": [],
        "runs": [],
    }
    if not layout.site_registry.is_file():
        result["packages_without_active_run"] = selected_ids
        return result
    connection = sqlite3.connect(f"file:{layout.site_registry}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    attachment_hashes: set[str] = set()
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        available_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for package_id in selected_ids:
            row = connection.execute(
                "SELECT versions.scene_version_id, activations.analysis_run_id "
                "FROM scene_versions AS versions LEFT JOIN scene_analysis_activations AS activations "
                "ON activations.scene_version_id = versions.scene_version_id "
                "WHERE versions.scene_package_id = ?",
                (package_id,),
            ).fetchone()
            if row is None or row["analysis_run_id"] is None:
                result["packages_without_active_run"].append(package_id)
                continue
            analysis_run_id = str(row["analysis_run_id"])
            frame_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM frame_analyses WHERE analysis_run_id = ?",
                    (analysis_run_id,),
                ).fetchone()[0]
            )
            review_event_count = 0
            if "frame_analysis_review_events" in available_tables:
                review_event_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM frame_analysis_review_events WHERE frame_analysis_id IN ("
                        "SELECT frame_analysis_id FROM frame_analyses WHERE analysis_run_id = ?)",
                        (analysis_run_id,),
                    ).fetchone()[0]
                )
            run_contract_count = 0
            if "analysis_run_contracts" in available_tables:
                run_contract_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM analysis_run_contracts WHERE analysis_run_id = ?",
                        (analysis_run_id,),
                    ).fetchone()[0]
                )
            observation_set_count = 0
            if "frame_observation_sets" in available_tables:
                observation_set_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM frame_observation_sets WHERE frame_analysis_id IN ("
                        "SELECT frame_analysis_id FROM frame_analyses WHERE analysis_run_id = ?)",
                        (analysis_run_id,),
                    ).fetchone()[0]
                )
            observation_item_count = 0
            if "frame_observation_items" in available_tables:
                observation_item_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM frame_observation_items WHERE observation_set_id IN ("
                        "SELECT sets.observation_set_id FROM frame_observation_sets AS sets "
                        "JOIN frame_analyses AS frames "
                        "ON frames.frame_analysis_id = sets.frame_analysis_id "
                        "WHERE frames.analysis_run_id = ?)",
                        (analysis_run_id,),
                    ).fetchone()[0]
                )
            hashes = connection.execute(
                "SELECT image_content_sha256 FROM frame_analyses WHERE analysis_run_id = ? UNION "
                "SELECT vlm_input_content_sha256 FROM frame_analyses WHERE analysis_run_id = ? UNION "
                "SELECT thumbnail_content_sha256 FROM frame_analyses WHERE analysis_run_id = ?",
                (analysis_run_id, analysis_run_id, analysis_run_id),
            )
            attachment_hashes.update(str(item[0]).lower() for item in hashes)
            result["runs"].append(
                {
                    "scene_package_id": package_id,
                    "scene_version_id": str(row["scene_version_id"]),
                    "analysis_run_id": analysis_run_id,
                    "frame_count": frame_count,
                    "review_event_count": review_event_count,
                    "run_contract_count": run_contract_count,
                    "observation_set_count": observation_set_count,
                    "observation_item_count": observation_item_count,
                }
            )
            result["frame_count"] += frame_count
            result["review_event_count"] += review_event_count
            result["run_contract_count"] += run_contract_count
            result["observation_set_count"] += observation_set_count
            result["observation_item_count"] += observation_item_count
    finally:
        connection.close()
    attachment_bytes = 0
    for content_sha256 in attachment_hashes:
        path = layout.site_attachments / content_sha256[:2] / content_sha256
        if path.is_file() and not path.is_symlink():
            attachment_bytes += path.stat().st_size
    result["run_count"] = len(result["runs"])
    result["attachment_count"] = len(attachment_hashes)
    result["attachment_bytes"] = attachment_bytes
    result["database_estimate_bytes"] = max(
        2 * 1024 * 1024, int(result["frame_count"]) * 4096
    )
    result["total_bytes"] = attachment_bytes + int(result["database_estimate_bytes"])
    return result


def initialize_empty_analysis_overlay(
    staging: Path, package_ids: list[str]
) -> dict[str, Any]:
    """Create a writable receiver overlay while intentionally exporting no run."""

    service = FieldMemoryService(staging)
    service.initialize_writable_overlay()
    layout = SceneRepositoryLayout.from_root(staging)
    manifest = {
        "schema_version": "1.0.0",
        "kind": "spatial_3dgs_analysis_distribution_overlay",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "selection_policy": "disabled-by-package-recipe-v1",
        "scene_package_ids": list(dict.fromkeys(package_ids)),
        "database_uri": "data/scene_repository/registries/site_records/registry.sqlite3",
        "attachments_uri": "data/scene_repository/registries/site_records/attachments/sha256",
        "scene_packages_immutable": True,
        "writable_at_runtime": True,
        "included": False,
        "runs": [],
        "packages_without_active_run": [],
        "attachment_count": 0,
        "attachment_bytes": 0,
    }
    (layout.site_records / "distribution_overlay.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def relocate_site_records_to_distribution_state(
    staging: Path,
    overlay_manifest: dict[str, Any] | None,
) -> None:
    """Move writable Site Records outside versioned application/data content."""

    runtime_site_records = SceneRepositoryLayout.from_root(staging).site_records
    state_site_records = staging / "state" / "site-records"
    state_site_records.mkdir(parents=True, exist_ok=True)
    for path in (
        sorted(runtime_site_records.iterdir()) if runtime_site_records.is_dir() else []
    ):
        destination = state_site_records / path.name
        if destination.exists():
            if destination.name == ".distribution-state":
                destination.unlink()
            else:
                raise DistributionError(f"distribution_state_collision:{path.name}")
        shutil.move(str(path), str(destination))
    runtime_site_records.mkdir(parents=True, exist_ok=True)
    (runtime_site_records / ".runtime-state-mountpoint").write_text(
        "Docker mounts state/site-records at this path.\n",
        encoding="utf-8",
    )
    if overlay_manifest is not None:
        overlay_manifest["distribution_state_uri"] = "state/site-records"
        (state_site_records / "distribution_overlay.json").write_text(
            json.dumps(overlay_manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def copy_legal(root: Path, staging: Path, public_release: bool) -> str:
    notices = root / "THIRD_PARTY_NOTICES.md"
    if not notices.is_file():
        raise DistributionError("third_party_notices_missing")
    shutil.copy2(notices, staging / notices.name)
    license_path = root / "LICENSE"
    if license_path.is_file() and license_path.stat().st_size > 0:
        shutil.copy2(license_path, staging / license_path.name)
        return "release_ready"
    if public_release:
        raise DistributionError("repository_license_missing:public_release_blocked")
    (staging / "INTERNAL_EVALUATION_ONLY.md").write_text(
        "# Internal evaluation only\n\n"
        "A repository-wide license has not been selected. Do not publish or redistribute this package as a public software release.\n",
        encoding="utf-8",
    )
    return "internal_evaluation_only"


def compose_text(app_id: str, spec: dict[str, Any]) -> str:
    # GPU inference runs in dedicated workers.  The API needs the privileged
    # entrypoint and Docker socket only for features that launch child
    # containers (for example the COLMAP pipeline).
    privileged_app = bool(spec.get("needs_docker_socket"))
    features = spec["feature"]
    feature_values = features if isinstance(features, list) else [str(features)]
    feature_argument = ",".join(feature_values)
    # The browser only reads published Scene Packages. The API receives a
    # writable runtime mount solely when the explicitly selected SPZ import
    # feature needs to publish a newly verified package.
    api_scene_package_mount_suffix = "" if "spz-import" in feature_values else ":ro"
    api_command = (
        '["/usr/local/bin/start_application_api", "python3", '
        '"-m", "services.control_api", "--repo-root", "/workspace", '
        f'"--host", "0.0.0.0", "--port", "8788", "--features", "{feature_argument}"]'
        if privileged_app
        else '["python3", "-m", "services.control_api", "--repo-root", '
        f'"/workspace", "--host", "0.0.0.0", "--port", "8788", "--features", "{feature_argument}"]'
    )
    user_line = (
        "" if privileged_app else '    user: "${LOCAL_UID:-1000}:${LOCAL_GID:-1000}"\n'
    )
    socket_lines = (
        "      - /var/run/docker.sock:/var/run/docker.sock:ro\n"
        if privileged_app
        else ""
    )
    web_command = (
        '["sh", "docker/start_web_viewer.sh"]'
        if app_id == "suite"
        else '["sh", "docker/start_spatial_app.sh"]'
    )
    bundled_scene_packages = list(spec.get("bundled_scene_packages", []))
    default_scene_package_id = (
        str(bundled_scene_packages[0].get("scene_package_id", ""))
        if bundled_scene_packages
        else ""
    )
    if app_id == "suite":
        target_lines = ""
    elif app_id == "custom":
        target_lines = (
            "      SPATIAL_APP_TARGET: custom\n"
            f"      SPATIAL_APP_MODES: {json.dumps(','.join(spec['preview_modes']), ensure_ascii=False)}\n"
            f"      SPATIAL_APP_LABEL: {json.dumps(str(spec['label']), ensure_ascii=False)}\n"
            f"      SPATIAL_VIEWER_VARIANT: {json.dumps(str(spec.get('viewer_variant', 'full')), ensure_ascii=False)}\n"
            f"      SPATIAL_DEFAULT_SCENE_PACKAGE: {json.dumps(default_scene_package_id, ensure_ascii=False)}\n"
        )
    else:
        target_lines = f"      SPATIAL_APP_TARGET: {app_id}\n"
    include_rosbridge = app_id == "suite" or bool(spec.get("include_rosbridge"))
    include_image_feature_worker = bool(spec.get("include_image_feature_worker"))
    include_frame_vlm_worker = bool(spec.get("include_frame_vlm_worker"))
    image_feature_worker_service = (
        ""
        if not include_image_feature_worker
        else """

  image-feature-worker:
    image: spatial-3dgs-system-generation:latest
    build:
      context: .
      dockerfile: docker/Dockerfile.generation
    working_dir: /workspace
    user: "${LOCAL_UID:-1000}:${LOCAL_GID:-1000}"
    command: ["python3", "-m", "services.feature_index_worker", "--repo-root", "/workspace"]
    environment:
      HOME: /tmp
      PYTHONDONTWRITEBYTECODE: "1"
    volumes:
      - ./:/workspace:ro
      - ./data:/workspace/data
      - ./state/site-records:/workspace/data/scene_repository/registries/site_records
      - ./var/spatial:/workspace/var/spatial
    restart: unless-stopped
"""
    )
    frame_vlm_worker_service = (
        ""
        if not include_frame_vlm_worker
        else """

  frame-vlm-worker:
    image: spatial-3dgs-system-generation:latest
    build:
      context: .
      dockerfile: docker/Dockerfile.generation
    working_dir: /workspace
    user: "${LOCAL_UID:-1000}:${LOCAL_GID:-1000}"
    command: ["python3", "-m", "services.frame_vlm_worker", "--repo-root", "/workspace"]
    environment:
      HOME: /tmp
      PYTHONDONTWRITEBYTECODE: "1"
      SPATIAL_LOCAL_VLM_MODEL: "${SPATIAL_LOCAL_VLM_MODEL:-/workspace/models/local-vlm}"
      SPATIAL_VLM_MODEL_STORE: /workspace/models
    volumes:
      - ./:/workspace:ro
      - ./data:/workspace/data
      - ./state/site-records:/workspace/data/scene_repository/registries/site_records
      - ./var/spatial:/workspace/var/spatial
      - ./models:/workspace/models
    gpus: all
    restart: unless-stopped
"""
    )
    rosbridge_service = (
        ""
        if not include_rosbridge
        else """

  rosbridge:
    build:
      context: .
      dockerfile: docker/Dockerfile.rosbridge
    environment:
      ROS_DOMAIN_ID: "${ROS_DOMAIN_ID:-0}"
    ports:
      - "${ROSBRIDGE_BIND_ADDRESS:-127.0.0.1}:${ROSBRIDGE_PORT:-9090}:9090"
    healthcheck:
      test: ["CMD", "python3", "/usr/local/bin/healthcheck_rosbridge.py"]
      interval: 5s
      timeout: 3s
      retries: 20
      start_period: 10s
    restart: unless-stopped
"""
    )
    return f"""services:
  application-api:
    image: spatial-3dgs-system-generation:latest
    build:
      context: .
      dockerfile: docker/Dockerfile.generation
    working_dir: /workspace
{user_line}    command: {api_command}
    environment:
      HOME: /tmp
      LOCAL_UID: "${{LOCAL_UID:-1000}}"
      LOCAL_GID: "${{LOCAL_GID:-1000}}"
      APP_PREVIEW_MIN_FREE_BYTES: "${{APP_PREVIEW_MIN_FREE_BYTES:-5368709120}}"
      APP_PREVIEW_UPLOAD_RETENTION_DAYS: "${{APP_PREVIEW_UPLOAD_RETENTION_DAYS:-30}}"
      APP_PREVIEW_PACKAGE_RETENTION_DAYS: "${{APP_PREVIEW_PACKAGE_RETENTION_DAYS:-14}}"
      APP_PREVIEW_STAGING_RETENTION_HOURS: "${{APP_PREVIEW_STAGING_RETENTION_HOURS:-24}}"
      APP_PREVIEW_MAX_DERIVED_PACKAGES: "${{APP_PREVIEW_MAX_DERIVED_PACKAGES:-20}}"
      SPATIAL_CONTROL_TOKEN: "${{SPATIAL_CONTROL_TOKEN:-}}"
      SPATIAL_LOCAL_VLM_MODEL: /workspace/models/local-vlm
      SPATIAL_VLM_MODEL_STORE: /workspace/models
      SPATIAL_ALLOWED_ORIGINS: "${{SPATIAL_ALLOWED_ORIGINS:-}}"
      SPATIAL_RBAC_REQUIRED: "${{SPATIAL_RBAC_REQUIRED:-0}}"
      PYTHONDONTWRITEBYTECODE: "1"
    volumes:
      - ./:/workspace:ro
      - ./data:/workspace/data
      - ./state/site-records:/workspace/data/scene_repository/registries/site_records
      - ./var/spatial:/workspace/var/spatial
      - ./models:/workspace/models
      - ./web_viewer/app/public/scene_package:/workspace/web_viewer/app/public/scene_package{api_scene_package_mount_suffix}
{socket_lines}    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8788/api/health', timeout=2).read()"]
      interval: 5s
      timeout: 3s
      retries: 20
      start_period: 10s

  app:
    build:
      context: .
      dockerfile: docker/Dockerfile.web-viewer
    working_dir: /workspace
    command: {web_command}
    environment:
{target_lines}      LOCAL_UID: "${{LOCAL_UID:-1000}}"
      LOCAL_GID: "${{LOCAL_GID:-1000}}"
      SPATIAL_CONTROL_API_URL: http://application-api:8788
      SPATIAL_CONTROL_TOKEN: "${{SPATIAL_CONTROL_TOKEN:-}}"
      SPATIAL_RBAC_REQUIRED: "${{SPATIAL_RBAC_REQUIRED:-0}}"
      SPATIAL_ACTOR_ID: "${{SPATIAL_ACTOR_ID:-}}"
      SPATIAL_ACTOR_ROLE: "${{SPATIAL_ACTOR_ROLE:-}}"
      SPATIAL_SITE_ID: "${{SPATIAL_SITE_ID:-}}"
      SCENE_PACKAGE_INDEX_READ_ONLY: "1"
    volumes:
      - ./:/workspace:ro
      - ./web_viewer/app/public/scene_package:/workspace/web_viewer/app/public/scene_package:ro
      - app_node_modules:/workspace/web_viewer/app/node_modules
    ports:
      - "${{APP_BIND_ADDRESS:-127.0.0.1}}:${{APP_PORT:-8080}}:5173"
    depends_on:
      application-api:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "node", "-e", "fetch('http://127.0.0.1:5173').then(r=>{{if(!r.ok)process.exit(1)}}).catch(()=>process.exit(1))"]
      interval: 5s
      timeout: 3s
      retries: 20
      start_period: 10s
{rosbridge_service}
{image_feature_worker_service}
{frame_vlm_worker_service}

volumes:
  app_node_modules:
"""


def setup_text(app_id: str, spec: dict[str, Any]) -> str:
    gpu = (
        "\n- NVIDIA GPUと、コンテナからGPUを利用できるDocker環境"
        "（登録画像AI解析またはCOLMAP実行時）"
        if spec["needs_gpu"]
        else ""
    )
    camera = (
        "\n- HTTPSまたはlocalhostでカメラ権限を許可（AprilTagカメラ確認時）"
        if spec["needs_camera"]
        else ""
    )
    scope_description = (
        "このZIPは全操作画面と実rosbridgeを含む互換用統合パッケージです。"
        if app_id == "suite"
        else (
            "このZIPは登録済みUI・backend部品を検証済みレシピで組み合わせたDockerパッケージです。"
            if app_id == "custom"
            else f"このZIPは`{app_id}`だけを起動する内部評価用Dockerパッケージです。他アプリの画面は表示されず、APIも`{spec['feature']}`機能だけを許可します。"
        )
    )
    bundled_packages = list(spec.get("bundled_scene_packages", []))
    viewer_variant = str(spec.get("viewer_variant", "full"))
    viewer_label = (
        "シンプル3DGS Viewer" if viewer_variant == "simple" else "多機能3DGS Viewer"
    )
    content_packs = list(spec.get("content_packs", []))
    split_content = bool(content_packs)
    if bundled_packages:
        package_lines = "\n".join(
            f"- `{item['scene_package_id']}`: <http://localhost:8080{item['viewer_url']}>"
            for item in bundled_packages
        )
        heading = "必要なScene Content Pack" if split_content else "同梱Scene Package"
        package_description = (
            "Scene Package本体はRuntime Bundleと重複させず、下記のContent Packとして分離しています。"
            if split_content
            else "次の検証済みScene Packageを読み取り専用Viewer資産として同梱しています。"
        )
        install_lines = ""
        if split_content:
            install_lines = """
Runtimeを起動する前に、受領した各Content Packを次のコマンドで導入してください。同じScene Packageが既に同じdigestで導入済みなら再コピーしません。

```bash
python3 scripts/install_content_pack.py /path/to/<sha256>.zip
```
"""
        package_section = f"""
## {heading}

{package_description}
既定Viewerは**{viewer_label}**です。

{package_lines}
{install_lines}

シンプル3DGS Viewerは3DGS表示、AprilTagデバッグ、移動・回転・寸法変更できる目盛り付き3D定規だけを提供し、ROS、動的箱、人物、Proxy、編集機能を読み込みません。多機能3DGS Viewerは既存の全Viewer機能を提供します。
"""
        data_statement = (
            "選択済みScene Packageは別配布のContent Packとして参照します。"
            if split_content
            else "選択済みScene Packageの読み取り専用スナップショットを同梱しています。"
        )
    else:
        package_section = ""
        data_statement = "MRP、Scene Package、SPZ、入力動画は同梱していません。"
    if app_id == "suite" or bool(spec.get("include_rosbridge")):
        rosbridge_setup = (
            "この配布にはrosbridgeを同梱します。9090番に正常なrosbridge WebSocketが既にある場合は"
            "それを再利用し、存在しない場合は同梱サービスを起動します。"
        )
    else:
        rosbridge_setup = (
            "この配布にはrosbridgeを同梱しません。AprilTagカメラ確認と3DGSの静的表示はそのまま利用できます。"
            "動的なAprilTag→Viewer連携を使う場合だけ、外部でrosbridgeを起動し、画面にそのWebSocket URLを指定してください。"
        )
    frame_vlm_setup = ""
    if bool(spec.get("include_frame_vlm_worker")):
        frame_vlm_setup = """

## ローカルVLM

登録画像AI解析画面でGPU VRAM（8 / 12 / 16 / 24 GB以上）を選ぶと、審査済みQwen3-VL 2B / 4B / 8Bの推奨モデルが選ばれます。「このモデルをダウンロード」を押すと、固定revisionのsnapshotを`models/`へ保存し、以後の起動でも再利用します。必要なダウンロード容量は約4.0 / 8.3 / 16.4 GiBです。VRAM値はBF16・画像1枚推論の運用目安で、画像解像度やCUDA環境により変動します。モデル重量はZIPに含まれず、解析画像は外部へ送信されません。ネットワーク制限環境では、同じ固定revisionの完全なsnapshotを`models/qwen3-vl-2b-instruct/`、`models/qwen3-vl-4b-instruct/`、または8B互換の`models/local-vlm/`へ配置できます。
"""
    analysis_overlay = spec.get("analysis_overlay")
    analysis_overlay_setup = ""
    if isinstance(analysis_overlay, dict):
        runs = analysis_overlay.get("runs")
        run_rows = runs if isinstance(runs, list) else []
        if run_rows:
            run_lines = "\n".join(
                "- `{analysis_run_id}` / Scene Package `{scene_package_id}` / "
                "{frame_count}件 / source status `{status}`".format(**item)
                for item in run_rows
            )
            analysis_overlay_setup = f"""

## 同梱済み解析結果

作成時に使用中だった解析runと、その解析画像・VLM入力・サムネイルを配布専用の書き込みoverlayへコピーしています。起動直後から解析レコードを閲覧でき、新しい解析はこのoverlayへ追記されます。Scene Package本体は変更されません。

{run_lines}
"""
        else:
            analysis_overlay_setup = """

## 解析結果overlay

選択したScene Packageに使用中の解析runはありませんでした。配布先で新しく解析すると、結果はScene Package外の配布専用overlayへ保存されます。
"""
    gpu_setup = ""
    if bool(spec.get("needs_gpu")):
        gpu_setup = """

## GPUを使う前の確認

`nvidia-smi`がUbuntu上で動くだけでは不十分です。次のコマンドは、**このアプリが使用するDocker daemonからGPUを見られること**に加え、固定済みPyTorchが検出GPUとバイナリ互換のCUDAコードを持ち、実際にBF16 CUDA演算を実行できることまで確認します。RTX 40シリーズ（compute capability 8.9）は互換な8.xコードと実演算で判定します。初回は検査対象imageをbuildするため時間がかかります。

```bash
./scripts/start_distribution.sh --check-gpu
```

`generation environment: VERIFIED`まで表示されたら通常起動してください。`failed to discover GPU vendor from CDI`、`could not select device driver`、`unknown or invalid runtime name: nvidia`のいずれかが出る場合、アプリではなくhostのDocker GPU連携が未設定です。`cuda.architecture`や`no kernel image is available`が出る場合は、配布imageのPyTorchがそのGPU世代に対応していないため、対応版へ更新してください。

### Windows + WSL2 + Docker Desktop

1. Windows側のNVIDIA driverを更新します。
2. 管理者PowerShellで`wsl --update`、`wsl --shutdown`を実行します。
3. Docker Desktopの`Settings > General > Use the WSL 2 based engine`を有効にします。
4. `Resources > WSL Integration`で、このUbuntu distributionを有効にしてDocker Desktopを再起動します。
5. Ubuntuを開き直し、上記`--check-gpu`を再実行します。

Docker Desktopでは検査コマンドへ`--runtime=nvidia`を追加しません。Docker DesktopとUbuntu内Docker Engineを混在させず、`docker context show`が示す実際のdaemonを設定してください。

### Ubuntu内のDocker Engineを使う場合

Docker DesktopではなくUbuntu/WSL内で起動したDocker Engineを使う場合だけ、NVIDIA公式手順で同じUbuntuへNVIDIA Container Toolkitを導入し、そのdaemonを設定します。

```bash
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
./scripts/start_distribution.sh --check-gpu
```

Toolkitが未導入なら、先にNVIDIA公式のapt手順を実施してください: <https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html>

### GPU設定前に閲覧だけ行う

次の起動ではVLM workerを除外します。同梱3Dシーンと既存解析レコードは閲覧できますが、新しいAI解析は実行できません。

```bash
./scripts/start_distribution.sh --view-only
```
"""
    return f"""# {spec["label"]} 配布版

{scope_description} {data_statement}
{package_section}

## 必要条件

- Docker Desktop / Docker Engine
- Docker Compose v2{gpu}{camera}

## 起動

ZIPを展開したフォルダで次の1コマンドを実行します。

```bash
./scripts/start_distribution.sh
```

Windows PowerShellからWSL内で実行する場合は、展開先を指定します。

```powershell
wsl.exe -d Ubuntu-24.04 --cd /path/to/extracted/package -- bash scripts/start_distribution.sh
```

起動後は <http://localhost:8080/> を開きます。配布版APIはDockerネットワーク内だけで使用し、hostの8788番には公開しません。{rosbridge_setup} 8080番を変更する場合は`APP_PORT=8081 ./scripts/start_distribution.sh`のように指定します。

初回起動時に制御token・port・UID/GIDを権限600の`.env`へ保存します。通常停止後は再buildせず、次のコマンドで同じコンテナを再開できます。

```bash
docker compose stop
docker compose start
```

`docker compose down`でコンテナを削除した場合も、既存imageと保存済み`.env`を使って再buildせず復帰できます。

```bash
docker compose up -d --no-build
```
{gpu_setup}
{frame_vlm_setup}
{analysis_overlay_setup}

## 解析DBの更新・バックアップ

解析DBと添付画像は`state/site-records/`、導入済みの審査済みVLMは`models/`に保存されます。どちらもアプリ本体やScene Packageとは分離されています。更新時にこれらを手作業で上書きしないでください。

```bash
python3 scripts/manage_distribution_overlay.py status
python3 scripts/manage_distribution_overlay.py backup
python3 scripts/transfer_distribution_state.py --from /path/to/old-distribution --to .
```

上記の更新コマンドは解析DB・添付画像に加え、新版でも同じ固定revisionが許可されている導入済みVLMを検証して引き継ぎます。

復元は停止中に`python3 scripts/manage_distribution_overlay.py restore --input backups/<backup>.zip`を実行します。復元前の現状も自動バックアップされます。

## 診断パック

サポートへ状態を渡す場合は、Scene本体・環境変数・tokenを含めない診断ZIPを生成します。ログは既定で含めず、明示指定時も既知のsecret・メールアドレス・IPアドレスを伏せます。

```bash
python3 scripts/create_diagnostic_bundle.py
# ログ末尾も必要な場合だけ
python3 scripts/create_diagnostic_bundle.py --include-logs
```

## 停止

```bash
docker compose down
```

## データ境界

- 読取: {", ".join(spec["reads"]) or "canonicalデータなし"}
- 書込: {", ".join(spec["writes"]) or "canonicalデータなし"}

`app_package_manifest.json`が機械可読な権限・依存契約、`checksums.sha256`が配布内容の整合性確認用です。受領側で既存データを使う場合だけ、展開先の`data/`へ明示的に配置してください。
"""


def write_checksums(staging: Path) -> None:
    lines = []
    for path in sorted(staging.rglob("*")):
        if path.is_file() and path.name != "checksums.sha256":
            lines.append(f"{sha256(path)}  {path.relative_to(staging).as_posix()}")
    (staging / "checksums.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_local_vlm_model_placeholder(staging: Path) -> None:
    models_root = staging / "models"
    models_root.mkdir(parents=True, exist_ok=True)
    summary = [
        "# Reviewed local VLM model store",
        "",
        "Model weights are not bundled in this ZIP. Select 8/12/16/24 GB VRAM in the registered-frame analysis screen, then download the recommended pinned snapshot.",
        "",
    ]
    for model in REVIEWED_VLMS.values():
        model_mount = models_root / model.storage_directory
        model_mount.mkdir(parents=True, exist_ok=True)
        (model_mount / "README.md").write_text(
            "# Local VLM model\n\n"
            f"This directory is reserved for `{model.model_id}` at revision "
            f"`{model.revision}` ({model.license_id}). Use the registered-frame "
            "analysis screen to download this pinned snapshot, or place the complete "
            "snapshot in this directory for an offline installation. Inference uses "
            "local files only. Do not substitute another model or revision without a "
            "new review and acceptance record. Model weights are not bundled in this ZIP.\n",
            encoding="utf-8",
        )
        summary.append(
            f"- `{model.storage_directory}/`: {model.model_id} ({model.parameter_scale}, "
            f"recommended VRAM {model.recommended_vram_gib} GB, download about "
            f"{model.approximate_download_gib} GiB)"
        )
    (models_root / "README.md").write_text("\n".join(summary) + "\n", encoding="utf-8")


def local_vlm_provenance() -> dict[str, Any]:
    return {
        "model_id": DEFAULT_VLM.model_id,
        "revision": DEFAULT_VLM.revision,
        "license": DEFAULT_VLM.license_id,
        "parameter_scale": DEFAULT_VLM.parameter_scale,
        "weights_included": False,
    }


def reviewed_vlm_catalog_provenance() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "default_model_id": DEFAULT_VLM.model_id,
        "weights_included": False,
        "models": [
            {
                "model_id": model.model_id,
                "revision": model.revision,
                "license": model.license_id,
                "parameter_scale": model.parameter_scale,
                "storage_directory": model.storage_directory,
                "approximate_download_gib": model.approximate_download_gib,
                "minimum_vram_gib": model.minimum_vram_gib,
                "recommended_vram_gib": model.recommended_vram_gib,
            }
            for model in REVIEWED_VLMS.values()
        ],
        "vram_recommendations": [
            {
                "vram_gib": item.vram_gib,
                "model_id": item.model_id,
                "label": item.label,
                "note": item.note,
            }
            for item in VRAM_RECOMMENDATIONS
        ],
    }


def create_app_distribution(
    root: Path,
    app_id: str,
    output: Path,
    *,
    scene_package_ids: list[str] | None = None,
    public_release: bool = False,
) -> dict[str, Any]:
    if app_id not in APP_SPECS or app_id == "viewer":
        raise DistributionError(f"standalone_app_not_supported:{app_id}")
    selected_scene_packages = list(dict.fromkeys(scene_package_ids or []))
    if app_id == "frame-analysis" and not selected_scene_packages:
        raise DistributionError("frame_analysis_requires_at_least_one_scene_package")
    if selected_scene_packages and app_id != "frame-analysis":
        raise DistributionError(f"scene_package_option_not_supported:{app_id}")
    spec = dict(APP_SPECS[app_id])
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f".{output.name}.{os.getpid()}.partial")
    partial.unlink(missing_ok=True)
    distribution_scope = ""
    bundle_name = f"{app_id}-{APP_VERSION}"
    try:
        with tempfile.TemporaryDirectory(
            prefix="spatial-app-distribution-"
        ) as temporary:
            staging = Path(temporary) / bundle_name
            staging.mkdir()
            copy_application_source(
                root,
                staging,
                include_rosbridge=app_id == "suite"
                or bool(spec.get("include_rosbridge")),
            )
            if spec.get("include_frame_vlm_worker"):
                write_local_vlm_model_placeholder(staging)
            bundled_scene_packages = bundle_scene_packages(
                root,
                staging,
                selected_scene_packages,
                require_frame_analysis_masks=app_id == "frame-analysis",
                scene_content_mode="self-contained",
            )
            spec["bundled_scene_packages"] = bundled_scene_packages
            analysis_overlay: dict[str, Any] | None = None
            if app_id == "frame-analysis":
                analysis_overlay = export_active_analysis_overlay(
                    root,
                    staging,
                    selected_scene_packages,
                )
                spec["analysis_overlay"] = analysis_overlay
            relocate_site_records_to_distribution_state(staging, analysis_overlay)
            distribution_scope = copy_legal(root, staging, public_release)
            (staging / "docker-compose.yml").write_text(
                compose_text(app_id, spec), encoding="utf-8"
            )
            (staging / "SETUP.md").write_text(
                setup_text(app_id, spec), encoding="utf-8"
            )
            manifest = {
                "schema_version": "1.0.0",
                "kind": "spatial_3dgs_application_distribution",
                "application_id": app_id,
                "application_version": APP_VERSION,
                "source_revision": git_revision(root),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "distribution_scope": distribution_scope,
                "entrypoint": "http://localhost:8080/",
                "api_feature_allowlist": spec["feature"]
                if isinstance(spec["feature"], list)
                else [spec["feature"]],
                "requirements": {
                    "gpu": spec["needs_gpu"],
                    "camera": spec["needs_camera"],
                    "rosbridge": spec["supports_rosbridge"],
                },
                "data_access": {"read": spec["reads"], "write": spec["writes"]},
                "canonical_data_included": bool(bundled_scene_packages),
                "bundled_scene_packages": bundled_scene_packages,
            }
            if analysis_overlay is not None:
                manifest["analysis_overlay"] = analysis_overlay
            if spec.get("include_frame_vlm_worker"):
                manifest["model_provenance"] = local_vlm_provenance()
                manifest["reviewed_model_catalog"] = reviewed_vlm_catalog_provenance()
            (staging / "app_package_manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            write_checksums(staging)
            with zipfile.ZipFile(partial, "w", allowZip64=True) as archive:
                for path in sorted(staging.rglob("*")):
                    if not path.is_file():
                        continue
                    compression = (
                        zipfile.ZIP_STORED
                        if path.suffix.lower() in ALREADY_COMPRESSED_SUFFIXES
                        else zipfile.ZIP_DEFLATED
                    )
                    archive.write(
                        path,
                        path.relative_to(staging.parent).as_posix(),
                        compress_type=compression,
                    )
        os.replace(partial, output)
    finally:
        partial.unlink(missing_ok=True)
    return {
        "status": "verified",
        "application_id": app_id,
        "output": str(output),
        "size_bytes": output.stat().st_size,
        "sha256": sha256(output),
        "distribution_scope": distribution_scope,
        "scene_package_ids": selected_scene_packages,
        "analysis_overlay": analysis_overlay,
    }


def create_custom_app_distribution(
    root: Path,
    recipe_value: Any,
    output: Path,
    *,
    public_release: bool = False,
    progress_callback: ProgressCallback | None = None,
    cancel_requested: CancelCallback | None = None,
) -> dict[str, Any]:
    recipe = normalize_package_recipe(recipe_value)
    if recipe["distribution_mode"] == "connected":
        raise ValueError(
            "connected_distribution_requires_repository_gateway_not_yet_available"
        )
    provenance = build_provenance(root, recipe)
    created_at = datetime.now(timezone.utc)
    build_id = created_at.strftime("build-%Y%m%dT%H%M%S%fZ")
    profile_id = recipe.get("product_profile_id")
    release_id = (
        f"{profile_id}-{recipe['product_profile_version']}-{build_id}"
        if isinstance(profile_id, str)
        else f"experimental-{recipe['package_id']}-{build_id}"
    )
    spec: dict[str, Any] = {
        "label": recipe["label"],
        "feature": recipe["api_features"],
        "preview_modes": recipe["preview_modes"],
        "needs_gpu": recipe["needs_gpu"],
        "needs_docker_socket": recipe["needs_docker_socket"],
        "needs_camera": recipe["needs_camera"],
        "supports_rosbridge": recipe["include_rosbridge"],
        "include_rosbridge": recipe["include_rosbridge"],
        "viewer_variant": recipe["viewer_variant"],
        "include_image_feature_worker": "image-feature-worker"
        in recipe["backend_components"],
        "include_frame_vlm_worker": "frame-vlm-worker" in recipe["backend_components"],
        "reads": recipe["reads"],
        "writes": recipe["writes"],
    }
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f".{output.name}.{os.getpid()}.partial")
    partial.unlink(missing_ok=True)
    distribution_scope = ""
    content_packs: list[dict[str, Any]] = []
    bundle_name = f"{recipe['package_id']}-{APP_VERSION}"
    try:
        with tempfile.TemporaryDirectory(
            prefix="spatial-custom-app-distribution-"
        ) as temporary:
            _require_not_cancelled(cancel_requested)
            _report_progress(
                progress_callback,
                "application_source",
                0,
                1,
                "アプリ本体を収録しています",
            )
            staging = Path(temporary) / bundle_name
            staging.mkdir()
            copy_application_source(
                root,
                staging,
                include_rosbridge=recipe["include_rosbridge"],
            )
            _report_progress(
                progress_callback,
                "application_source",
                1,
                1,
                "アプリ本体を収録しました",
            )
            if spec.get("include_frame_vlm_worker"):
                write_local_vlm_model_placeholder(staging)
            bundled_scene_packages = bundle_scene_packages(
                root,
                staging,
                recipe["scene_package_ids"],
                progress_callback=progress_callback,
                cancel_requested=cancel_requested,
                require_frame_analysis_masks="frame-analysis" in recipe["ui_modules"],
                scene_content_mode=recipe["scene_content_mode"],
            )
            spec["bundled_scene_packages"] = bundled_scene_packages
            if bundled_scene_packages:
                spec["reads"] = [*spec["reads"], "bundled verified Scene Packages"]
            analysis_overlay: dict[str, Any] | None = None
            if "frame-analysis" in recipe["ui_modules"]:
                if recipe["include_active_analysis"]:
                    analysis_overlay = export_active_analysis_overlay(
                        root,
                        staging,
                        recipe["scene_package_ids"],
                        progress_callback=progress_callback,
                        cancel_requested=cancel_requested,
                    )
                else:
                    analysis_overlay = initialize_empty_analysis_overlay(
                        staging,
                        recipe["scene_package_ids"],
                    )
                spec["analysis_overlay"] = analysis_overlay
            if recipe["distribution_mode"] == "split":
                repository_layout = SceneRepositoryLayout.from_root(root)
                repository_layout.content_packs.mkdir(parents=True, exist_ok=True)
                runtime_root = staging / "web_viewer/app/public/scene_package/runtime"
                for package_index, package_entry in enumerate(
                    bundled_scene_packages, start=1
                ):
                    package_id = str(package_entry["scene_package_id"])
                    _report_progress(
                        progress_callback,
                        "content_packs",
                        package_index - 1,
                        len(bundled_scene_packages),
                        f"Scene Content Pack {package_index}/{len(bundled_scene_packages)} を作成しています",
                    )
                    content_pack = create_scene_content_pack(
                        runtime_root / package_id,
                        repository_layout.content_packs,
                        scene_package_id=package_id,
                        metadata={
                            "scene_id": package_entry.get("scene_id"),
                            "reconstruction_id": package_entry.get("reconstruction_id"),
                        },
                    )
                    content_path = Path(content_pack.pop("path"))
                    content_pack["output_path"] = (
                        content_path.relative_to(root).as_posix()
                        if content_path.is_relative_to(root)
                        else str(content_path)
                    )
                    content_pack["download_url"] = (
                        f"/api/content-packs/{content_pack['sha256']}/download"
                    )
                    content_packs.append(content_pack)
                    shutil.rmtree(runtime_root / package_id)
                    _report_progress(
                        progress_callback,
                        "content_packs",
                        package_index,
                        len(bundled_scene_packages),
                        f"Scene Content Pack {package_index}/{len(bundled_scene_packages)} を作成しました",
                    )
                spec["content_packs"] = content_packs
            relocate_site_records_to_distribution_state(staging, analysis_overlay)
            _require_not_cancelled(cancel_requested)
            _report_progress(
                progress_callback,
                "metadata",
                0,
                1,
                "起動設定と検証情報を作成しています",
            )
            distribution_scope = copy_legal(root, staging, public_release)
            (staging / "docker-compose.yml").write_text(
                compose_text("custom", spec), encoding="utf-8"
            )
            (staging / "SETUP.md").write_text(
                setup_text("custom", spec), encoding="utf-8"
            )
            manifest = {
                "schema_version": "1.0.0",
                "kind": "spatial_3dgs_composed_application_distribution",
                "application_id": recipe["package_id"],
                "application_version": APP_VERSION,
                "source_revision": git_revision(root),
                "created_at": created_at.isoformat(),
                "distribution_scope": distribution_scope,
                "entrypoint": "http://localhost:8080/",
                "composition": {
                    "ui_modules": recipe["ui_modules"],
                    "backend_components": recipe["backend_components"],
                    "viewer_variant": recipe["viewer_variant"],
                },
                "api_feature_allowlist": recipe["api_features"],
                "requirements": {
                    "gpu": recipe["needs_gpu"],
                    "camera": recipe["needs_camera"],
                    "rosbridge": recipe["include_rosbridge"],
                },
                "data_access": {"read": spec["reads"], "write": recipe["writes"]},
                "canonical_data_included": bool(bundled_scene_packages)
                and not content_packs,
                "bundled_scene_packages": bundled_scene_packages,
                "content_delivery": {
                    "mode": recipe["distribution_mode"],
                    "content_packs": content_packs,
                },
                "product_release": {
                    "release_id": release_id,
                    "profile_id": profile_id,
                    "profile_version": recipe.get("product_profile_version"),
                    "runtime_version": APP_VERSION,
                    "build_id": build_id,
                    "lifecycle": recipe["release_lifecycle"],
                    "support_level": recipe["support_level"],
                    "distribution_mode": recipe["distribution_mode"],
                },
                "build_provenance": provenance,
            }
            if analysis_overlay is not None:
                manifest["analysis_overlay"] = analysis_overlay
            if spec.get("include_frame_vlm_worker"):
                manifest["model_provenance"] = local_vlm_provenance()
                manifest["reviewed_model_catalog"] = reviewed_vlm_catalog_provenance()
            (staging / "package_recipe.json").write_text(
                json.dumps(recipe, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            (staging / "app_package_manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            write_checksums(staging)
            _report_progress(
                progress_callback, "metadata", 1, 1, "起動設定と検証情報を作成しました"
            )
            archive_files = [
                path for path in sorted(staging.rglob("*")) if path.is_file()
            ]
            with zipfile.ZipFile(partial, "w", allowZip64=True) as archive:
                for archive_index, path in enumerate(archive_files, start=1):
                    _require_not_cancelled(cancel_requested)
                    compression = (
                        zipfile.ZIP_STORED
                        if path.suffix.lower() in ALREADY_COMPRESSED_SUFFIXES
                        else zipfile.ZIP_DEFLATED
                    )
                    archive.write(
                        path,
                        path.relative_to(staging.parent).as_posix(),
                        compress_type=compression,
                    )
                    report_interval = max(1, len(archive_files) // 200)
                    if (
                        archive_index == len(archive_files)
                        or archive_index % report_interval == 0
                    ):
                        _report_progress(
                            progress_callback,
                            "zip",
                            archive_index,
                            len(archive_files),
                            f"ZIP {archive_index}/{len(archive_files)} ファイル",
                        )
            _require_not_cancelled(cancel_requested)
        os.replace(partial, output)
    finally:
        partial.unlink(missing_ok=True)
    try:
        runtime_output_path = output.relative_to(root).as_posix()
        release_root = SceneRepositoryLayout.from_root(root).product_releases
    except ValueError:
        runtime_output_path = str(output)
        release_root = output.parent / "releases"
    runtime_artifact = {
        "kind": "runtime_bundle",
        "filename": output.name,
        "path": runtime_output_path,
        "size_bytes": output.stat().st_size,
        "sha256": sha256(output),
    }
    release_record = {
        "schema_version": "1.0.0",
        "kind": "spatial_product_release",
        "release_id": release_id,
        "product": {
            "profile_id": profile_id,
            "profile_version": recipe.get("product_profile_version"),
            "runtime_version": APP_VERSION,
            "build_id": build_id,
        },
        "lifecycle": recipe["release_lifecycle"],
        "created_at": created_at.isoformat(),
        "provenance": provenance,
        "inputs": {
            "scene_packages": [
                {
                    "scene_package_id": item["scene_package_id"],
                    "scene_manifest_sha256": item["scene_manifest_sha256"],
                }
                for item in bundled_scene_packages
            ],
            "analysis_overlay": (
                {
                    "manifest_sha256": canonical_json_sha256(analysis_overlay),
                    "included": bool(analysis_overlay.get("included")),
                    "run_ids": [
                        item.get("analysis_run_id")
                        for item in analysis_overlay.get("runs", [])
                        if isinstance(item, dict)
                        and isinstance(item.get("analysis_run_id"), str)
                    ],
                }
                if analysis_overlay is not None
                else None
            ),
            "model_catalog": (
                {
                    "sha256": canonical_json_sha256(reviewed_vlm_catalog_provenance()),
                    "weights_included": False,
                }
                if spec.get("include_frame_vlm_worker")
                else None
            ),
        },
        "artifacts": [
            runtime_artifact,
            *[
                {
                    "kind": "scene_content_pack",
                    "filename": item["filename"],
                    "path": item["output_path"],
                    "size_bytes": item["size_bytes"],
                    "sha256": item["sha256"],
                    "scene_package_id": item["scene_package_id"],
                }
                for item in content_packs
            ],
        ],
    }
    release_path = (
        release_root / str(profile_id or "experimental") / f"{release_id}.json"
    )
    release_path.parent.mkdir(parents=True, exist_ok=True)
    release_partial = release_path.with_suffix(".json.partial")
    release_partial.write_text(
        json.dumps(release_record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(release_partial, release_path)
    return {
        "status": "verified",
        "application_id": recipe["package_id"],
        "output": str(output),
        "size_bytes": output.stat().st_size,
        "sha256": runtime_artifact["sha256"],
        "distribution_scope": distribution_scope,
        "recipe": recipe,
        "analysis_overlay": analysis_overlay,
        "release_id": release_id,
        "build_id": build_id,
        "build_provenance": provenance,
        "content_packs": content_packs,
        "release_record": (
            release_path.relative_to(root).as_posix()
            if release_path.is_relative_to(root)
            else str(release_path)
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a Docker ZIP for one Spatial 3DGS application."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--app", choices=sorted(APP_SPECS))
    source.add_argument(
        "--recipe",
        type=Path,
        help="Validated JSON recipe for a composed Web/API package",
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--scene-package",
        action="append",
        dest="scene_packages",
        default=[],
        help="Verified Scene Package ID (required by viewer and frame-analysis)",
    )
    parser.add_argument("--public-release", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.output.suffix.lower() != ".zip":
        raise SystemExit("--output must end in .zip")
    try:
        if args.recipe is not None:
            if args.scene_packages:
                raise DistributionError("scene_package_option_is_viewer_only")
            try:
                recipe = json.loads(args.recipe.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise DistributionError(f"package_recipe_unreadable:{error}") from error
            result = create_custom_app_distribution(
                ROOT,
                recipe,
                args.output,
                public_release=args.public_release,
            )
        elif args.app == "viewer":
            if not args.scene_packages:
                raise DistributionError("viewer_requires_at_least_one_scene_package")
            result = create_viewer_distribution(
                ROOT,
                list(args.scene_packages),
                args.output,
                public_release=args.public_release,
            )
            result["application_id"] = "viewer"
        else:
            if args.scene_packages and args.app != "frame-analysis":
                raise DistributionError(
                    f"scene_package_option_not_supported:{args.app}"
                )
            result = create_app_distribution(
                ROOT,
                args.app,
                args.output,
                scene_package_ids=list(args.scene_packages),
                public_release=args.public_release,
            )
    except DistributionError as error:
        raise SystemExit(f"app_distribution_failed:{error}") from error
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
