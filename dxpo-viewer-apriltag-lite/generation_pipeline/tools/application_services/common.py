"""Shared compatibility imports and helpers for the preview application services.

This module is the single boundary around the legacy flat tool namespace.
Feature services import from here while the public PreviewService remains a
backward-compatible facade.
"""

from __future__ import annotations

import argparse
from collections import OrderedDict
import copy
from contextlib import contextmanager
import errno
import fcntl
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

TOOLS = Path(__file__).resolve().parent.parent
REPO_ROOT = TOOLS.parents[1]

from generation_pipeline.tools.scene_config import SceneConfigError, load_scene_config
from generation_pipeline.tools.validate_metric_reconstruction_package import (
    validate_metric_reconstruction_package,
)
from generation_pipeline.tools.validate_scene_package import validate_scene_package
from generation_pipeline.tools.scene_dataset import (
    estimate_dataset_bytes,
    make_metric_manifest_portable,
    materialize_scene_dataset,
    publish_scene_package_tree,
    related_scene_package_ids,
    self_contained_package_id,
    source_fingerprint,
)
from generation_pipeline.tools.quaternion_contract import require_unit_quaternion_xyzw
from generation_pipeline.tools.application_preview_uploads import (
    list_uploaded_videos,
    store_uploaded_video,
)
from generation_pipeline.tools.application_preview_routes import dispatch_preview_api
from generation_pipeline.tools.application_preview_features import (
    ApplicationFeatureDisabled,
    parse_enabled_features,
    require_feature,
)
from generation_pipeline.tools.apriltag_family_contract import (
    apriltag_family_catalog,
    require_supported_apriltag_family,
)
from generation_pipeline.tools.apriltag_live_detection import (
    AprilTagLiveDetector,
    MAX_LIVE_FRAME_BYTES,
)
from generation_pipeline.tools.application_package_recipe import (
    package_catalog,
    normalize_package_recipe,
)
from scripts.create_app_distribution import (
    DistributionCancelled,
    create_custom_app_distribution,
    display_scene_package_projection_size,
    estimate_active_analysis_overlay,
)
from packages.pipeline_core import (
    PROJECT_NAME_RE,
    StorageLifecycle,
    project_name_from_workspace_uri,
)
from packages.scene_package_integrity import scene_package_reference_lock
from packages.scene_repository import (
    SceneRepositoryLayout,
    catalog_status,
    rebuild_catalog,
)


SCENE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SCENE_PACKAGE_DATE_PREFIX_RE = re.compile(r"^(?P<date>(?:19|20)\d{6})(?:[_-]|$)")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
WINDOWS_WSL_UNC_PATH_RE = re.compile(r"^\\\\wsl(?:\.localhost)?\\[^\\]+\\(?P<path>.*)$", re.IGNORECASE)
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv"}
GAUSSIAN_SUFFIXES = {".spz", ".ply", ".rad"}
VIRTUAL_RIG_LAYOUT_CONFIGS = {
    "coverage_12": "generation_pipeline/config/virtual_rig.default.json",
    "cube_6": "generation_pipeline/config/virtual_rig.cube6.json",
}
APPROVED_PERSON_MASK_CONFIGS = frozenset({
    "generation_pipeline/config/person_mask.default.json",
})
IMPORT_LOCKS_DIRECTORY = "locks"
# Spark's SPZ decoder maps the LichtFeld coordinate convention [x, y, z] to
# the viewer convention [x, -y, -z].  This is a fixed decoder contract, not a
# scene-specific alignment.  Every annotation or camera originating from the
# horizontalized COLMAP/LichtFeld coordinates must cross this boundary before
# it is displayed beside an SPZ asset.
SPARK_SPZ_DECODER_ROTATION_XYZW = (1.0, 0.0, 0.0, 0.0)
SPARK_SPZ_COORDINATE_CONTRACT = "spark_spz_decoder_rx180"
# PLY and RAD do not carry a trustworthy application-level relationship to
# the Metric Reconstruction Package.  Importers must therefore record which
# of these explicit, reviewed transforms was used instead of guessing from a
# filename.  LichtFeld/Spark exports normally use the Rx180 contract; identity
# is retained for already viewer-oriented assets.
GAUSSIAN_COORDINATE_CONTRACTS = {
    SPARK_SPZ_COORDINATE_CONTRACT: SPARK_SPZ_DECODER_ROTATION_XYZW,
    "spark_gaussian_decoder_rx180": SPARK_SPZ_DECODER_ROTATION_XYZW,
    "viewer_native_identity": (0.0, 0.0, 0.0, 1.0),
}
STOP_AFTER_STAGES = {
    "input_validation",
    "preprocessing",
    "person_mask",
    "sfm",
    "gsplat",
    "metric",
    "lfs_export",
    "metric_reconstruction_package",
    "dense",
    "proxy",
    "scene_package",
    "viewer",
}
DEFAULT_MIN_FREE_BYTES = 5 * 1024 * 1024 * 1024
MAX_REPOSITORY_SIZE_CACHE_ENTRIES = 512
MAX_SCENE_PACKAGE_SIZE_CACHE_ENTRIES = 512
SCENE_DATA_APP_MODULES = frozenset({
    "field-memory", "spatial-search", "frame-analysis", "evidence-ai", "change-review",
})


def scene_package_date_metadata(
    manifest_path: Path,
    manifest: dict[str, Any],
    dataset_manifest: dict[str, Any],
    metric_manifest: dict[str, Any],
) -> dict[str, str | None]:
    """Resolve explicit package dates while keeping legacy fallback provenance visible."""

    registered_at = manifest.get("created_at")
    if not isinstance(registered_at, str) or not registered_at:
        registered_at = datetime.fromtimestamp(
            manifest_path.stat().st_mtime,
            timezone.utc,
        ).isoformat()

    captured_at = manifest.get("captured_at")
    if not isinstance(captured_at, str) or not captured_at:
        captured_at = dataset_manifest.get("captured_at")
    if isinstance(captured_at, str) and captured_at:
        return {
            "data_date": captured_at,
            "data_date_source": "captured_at",
            "registered_at": registered_at,
        }

    for identifier in (
        manifest.get("project_name"),
        manifest.get("project_id"),
        manifest.get("reconstruction_id"),
        manifest.get("scene_package_id"),
    ):
        if not isinstance(identifier, str):
            continue
        match = SCENE_PACKAGE_DATE_PREFIX_RE.match(identifier)
        if match is None:
            continue
        try:
            identifier_date = datetime.strptime(match.group("date"), "%Y%m%d").date().isoformat()
        except ValueError:
            continue
        return {
            "data_date": identifier_date,
            "data_date_source": "identifier_date",
            "registered_at": registered_at,
        }

    for source, value in (
        ("metric_reconstruction_created_at", metric_manifest.get("created_at")),
        ("scene_package_created_at", manifest.get("created_at")),
        ("dataset_created_at", dataset_manifest.get("created_at")),
    ):
        if isinstance(value, str) and value:
            return {
                "data_date": value,
                "data_date_source": source,
                "registered_at": registered_at,
            }

    return {
        "data_date": registered_at,
        "data_date_source": "registered_at",
        "registered_at": registered_at,
    }


DISPLAY_STAGES = (
    ("input_validation", "input validation"),
    ("panorama_extraction", "panorama extraction"),
    ("virtual_images", "virtual image generation"),
    ("person_masks", "person mask generation"),
    ("fixed_rig_sfm", "fixed-rig SfM"),
    ("apriltag_metric_alignment", "水平化・AprilTag実寸scale"),
    ("camera_trajectory", "水平化済みカメラ軌跡"),
    ("lichtfeld_export", "LichtFeld Studio export"),
    ("metric_reconstruction_package", "Metric Reconstruction Package"),
)
SOURCE_STAGE = {
    "input_validation": "input_validation",
    "panorama_extraction": "preprocessing",
    "virtual_images": "preprocessing",
    "person_masks": "person_mask",
    "fixed_rig_sfm": "sfm",
    "apriltag_metric_alignment": "metric",
    "camera_trajectory": "metric",
    "lichtfeld_export": "lfs_export",
    "metric_reconstruction_package": "metric_reconstruction_package",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def safe_relative(value: str, label: str) -> str:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label} must be repository-relative")
    return path.as_posix()


def scene_package_registry_validation(
    package: Path,
    manifest: Any,
    *,
    registered: bool,
    published: bool,
) -> dict[str, Any]:
    """Perform the bounded checks needed to render the package registry.

    Canonical and runtime packages reach their registered locations only after
    ``validate_scene_package`` succeeds.  Re-running that deep validator while
    listing packages hashes every registered frame/mask and opens every mask
    image; self-contained datasets can therefore turn a simple registry read
    into minutes of work.  The registry trusts that immutable publication gate
    and only checks identity plus the files required to enter the Viewer.
    Full integrity validation remains mandatory during import/publication and
    in the explicit validation tools.
    """

    failures: list[str] = []
    warnings: list[str] = []
    if not isinstance(manifest, dict):
        failures.append("scene_manifest_invalid")
        manifest = {}
    schema_version = manifest.get("schema_version")
    if schema_version not in {"1.0.0", "1.1.0", "1.2.0", "1.3.0"}:
        failures.append("unsupported_manifest_schema")
    for key in ("scene_id", "scene_package_id", "reconstruction_id"):
        value = manifest.get(key)
        if not isinstance(value, str) or SCENE_ID_RE.fullmatch(value) is None:
            failures.append(f"manifest_{key}_invalid")
    if manifest.get("unit") != "meter":
        failures.append("manifest_not_meter")
    coordinate_system = manifest.get("coordinate_system")
    if not isinstance(coordinate_system, dict) or coordinate_system.get("up_axis") != "Y":
        failures.append("manifest_not_y_up")

    assets = manifest.get("assets")
    splats = assets.get("splats") if isinstance(assets, dict) else None
    if not isinstance(splats, list) or not splats:
        failures.append("missing_splat_asset")
    else:
        for index, splat in enumerate(splats):
            try:
                if not isinstance(splat, dict):
                    raise ValueError(f"splat_{index}_invalid")
                relative = safe_relative(str(splat.get("uri", "")), f"splat_{index}")
                if not (package / relative).is_file():
                    failures.append(f"missing_splat_{index}")
            except ValueError as error:
                failures.append(str(error))

    if schema_version == "1.3.0":
        dataset = manifest.get("dataset")
        try:
            if not isinstance(dataset, dict):
                raise ValueError("scene_dataset_reference_invalid")
            dataset_uri = safe_relative(str(dataset.get("manifest_uri", "")), "scene_dataset_manifest")
            dataset_manifest = read_json(package / dataset_uri, {})
            if not isinstance(dataset_manifest, dict) or dataset_manifest.get("status") != "verified":
                failures.append("scene_dataset_not_verified")
        except ValueError as error:
            failures.append(str(error))

    if not registered:
        warnings.append("scene_package_not_registered")
    if not published:
        warnings.append("scene_package_not_published")
    status = "verified" if registered and published and not failures else "failed" if failures else "not_validated"
    return {
        "schema_version": schema_version if isinstance(schema_version, str) else "1.0.0",
        "stage": "scene_package_registry_validation",
        "status": status,
        "scene_id": manifest.get("scene_id"),
        "package": str(package),
        "manifest": "scene_manifest.json",
        "validation_mode": "registration_attestation",
        "failure_reasons": failures,
        "warnings": warnings,
    }


def json_hashable(value: Any) -> Any:
    """Return a JSON-friendly value for errors and test responses."""
    return value if isinstance(value, (dict, list, str, int, float, bool, type(None))) else str(value)


def scene_package_dynamic_tracking_compatibility(manifest: Any) -> dict[str, Any]:
    """Describe whether a verified Scene Package can drive the ROS overlay.

    v1.0/v1.1 packages remain valid read-only Viewer inputs, but they do not
    carry the coordinate-boundary and freshness contracts required by the
    AprilTag-to-Viewer bridge.  Keep this check independent from schema
    validation so package composition fails closed even when a legacy package
    is otherwise a valid Scene Package.
    """
    reasons: list[str] = []
    if not isinstance(manifest, dict):
        reasons.append("scene_manifest_missing")
        return {"compatible": False, "failure_reasons": reasons}

    schema_version = manifest.get("schema_version")
    version_parts = schema_version.split(".") if isinstance(schema_version, str) else []
    compatible_schema = (
        len(version_parts) == 3
        and version_parts[0] == "1"
        and version_parts[1].isdigit()
        and int(version_parts[1]) >= 2
    )
    if not compatible_schema:
        reasons.append("scene_package_schema_v1_2_required")

    coordinate_system = manifest.get("coordinate_system")
    if not isinstance(coordinate_system, dict) or not isinstance(
        coordinate_system.get("T_viewer_ros"), dict
    ):
        reasons.append("scene_package_T_viewer_ros_missing")
    if not isinstance(manifest.get("dynamic_overlay"), dict):
        reasons.append("scene_package_dynamic_overlay_missing")
    return {"compatible": not reasons, "failure_reasons": reasons}


def scene_package_application_compatibility(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Describe which package applications can consume one package safely."""

    verified = entry.get("status") == "verified"
    dataset = entry.get("dataset") if isinstance(entry.get("dataset"), dict) else {}
    dataset_verified = dataset.get("status") == "verified"
    field_memory_ready = dataset_verified and dataset.get("field_memory") is True
    frame_count = dataset.get("frame_count")
    person_mask_count = dataset.get("mask_count")
    colmap_mask_count = dataset.get("colmap_mask_count")
    frame_analysis_reasons: list[str] = []
    if not verified:
        frame_analysis_reasons.append("scene_package_not_verified")
    if dataset.get("schema_version") != "1.1.0":
        frame_analysis_reasons.append("scene_dataset_schema_v1_1_required")
    if not dataset_verified:
        frame_analysis_reasons.append("scene_dataset_not_verified")
    if not (
        isinstance(frame_count, int)
        and frame_count > 0
        and person_mask_count == frame_count
        and colmap_mask_count == frame_count
    ):
        frame_analysis_reasons.append("registered_frame_mask_counts_inconsistent")
    if not field_memory_ready:
        frame_analysis_reasons.append("embedded_field_memory_missing")

    field_reasons: list[str] = []
    if not verified:
        field_reasons.append("scene_package_not_verified")
    if not field_memory_ready:
        field_reasons.append("embedded_field_memory_missing")

    viewer_reasons = [] if verified else ["scene_package_not_verified"]
    return {
        "viewer": {"compatible": not viewer_reasons, "failure_reasons": viewer_reasons},
        "scene-manager": {"compatible": not viewer_reasons, "failure_reasons": viewer_reasons},
        "field-memory": {"compatible": not field_reasons, "failure_reasons": field_reasons},
        "site-insights": {"compatible": not field_reasons, "failure_reasons": field_reasons},
        "frame-analysis": {
            "compatible": not frame_analysis_reasons,
            "failure_reasons": frame_analysis_reasons,
        },
        "simple-viewer": {"compatible": not viewer_reasons, "failure_reasons": viewer_reasons},
    }


class DeprecatedScenePackageApiError(ValueError):
    """Raised by endpoints from the removed mutable root-package workflow."""


class ImmutableScenePackageError(ValueError):
    """Raised when a request attempts to mutate an existing immutable package."""
