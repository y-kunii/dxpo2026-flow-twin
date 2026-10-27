from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

if __package__:
    from .apriltag_family_contract import require_supported_apriltag_family
else:
    from apriltag_family_contract import require_supported_apriltag_family

SCHEMA_VERSION = "1.0.0"
PROFILES = {"fast", "quality", "auto"}
REQUIRED_SECTIONS = {
    "virtual_rig",
    "sfm",
    "person_mask",
    "metric_marker",
    "execution",
    "reuse",
}


class SceneConfigError(ValueError):
    """Raised when a scene configuration violates the shared contract."""


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SceneConfigError(f"{name} must be an object")
    return value


def _require_string(mapping: dict[str, Any], key: str, name: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SceneConfigError(f"{name}.{key} must be a non-empty string")
    return value


def _require_path(mapping: dict[str, Any], key: str, name: str) -> str:
    value = _require_string(mapping, key, name)
    if value.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", value):
        raise SceneConfigError(f"{name}.{key} must be repository-relative")
    return value


def validate_scene_config(
    config: dict[str, Any],
    *,
    repo_root: Path | None = None,
    check_paths: bool = False,
) -> dict[str, Any]:
    if config.get("schema_version") != SCHEMA_VERSION:
        raise SceneConfigError(f"schema_version must be {SCHEMA_VERSION}")
    scene_id = config.get("scene_id")
    if (
        not isinstance(scene_id, str)
        or len(scene_id) > 64
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", scene_id)
    ):
        raise SceneConfigError("scene_id must contain only letters, digits, '.', '_' or '-'")
    for identifier, maximum in (("scene_template_id", 64), ("reconstruction_id", 128)):
        value = config.get(identifier)
        if value is not None and (
            not isinstance(value, str)
            or len(value) > maximum
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value)
        ):
            raise SceneConfigError(
                f"{identifier} must contain only letters, digits, '.', '_' or '-'"
            )
    input_projection = config.get("input_projection", "equirectangular_360")
    if input_projection not in {"equirectangular_360", "perspective"}:
        raise SceneConfigError(
            "input_projection must be equirectangular_360 or perspective"
        )
    for key in ("input_video", "workspace"):
        _require_path(config, key, "scene")
    for key in ("scene_package_output", "viewer_publish_output"):
        if key in config:
            _require_path(config, key, "scene")
    preprocessing = _require_mapping(config.get("preprocessing", {}), "preprocessing")
    if "frame_interval_sec" in preprocessing and (not isinstance(preprocessing["frame_interval_sec"], (int, float)) or preprocessing["frame_interval_sec"] <= 0):
        raise SceneConfigError("preprocessing.frame_interval_sec must be positive")
    if "panoramas" in preprocessing:
        _require_path(preprocessing, "panoramas", "preprocessing")
    person_mask = _require_mapping(config.get("person_mask", {}), "person_mask")
    for key in ("config", "static_masks", "person_masks", "colmap_masks", "report", "model_asset"):
        if key in person_mask:
            _require_path(person_mask, key, "person_mask")

    missing = sorted(REQUIRED_SECTIONS - set(config))
    if missing:
        raise SceneConfigError(f"missing sections: {', '.join(missing)}")

    virtual_rig = _require_mapping(config["virtual_rig"], "virtual_rig")
    _require_path(virtual_rig, "config", "virtual_rig")
    _require_string(virtual_rig, "profile", "virtual_rig")

    sfm = _require_mapping(config["sfm"], "sfm")
    for key in ("model", "manifest", "images", "masks", "rig_config"):
        _require_path(sfm, key, "sfm")
    if "sparse_model" in sfm:
        _require_path(sfm, "sparse_model", "sfm")
    if sfm.get("profile") not in {"fixed_rig", "perspective", "external"}:
        raise SceneConfigError("sfm.profile must be fixed_rig, perspective or external")

    person_mask = _require_mapping(config["person_mask"], "person_mask")
    for key in ("config", "person_masks", "colmap_masks", "report"):
        _require_path(person_mask, key, "person_mask")
    if "docker_image" in person_mask:
        _require_string(person_mask, "docker_image", "person_mask")

    gsplat = config.get("gsplat")
    if gsplat is not None:
        gsplat = _require_mapping(gsplat, "gsplat")
        if gsplat.get("profile") not in {"external", "reuse"}:
            raise SceneConfigError("gsplat.profile must be external or reuse")
        _require_path(gsplat, "spz", "gsplat")
        _require_path(gsplat, "manifest", "gsplat")

    marker = _require_mapping(config["metric_marker"], "metric_marker")
    for key in ("family", "accepted_selection_policy"):
        _require_string(marker, key, "metric_marker")
    try:
        require_supported_apriltag_family(marker["family"])
    except ValueError as error:
        raise SceneConfigError(f"metric_marker.family is unsupported: {marker['family']}") from error
    if not isinstance(marker.get("id"), int) or marker["id"] < 0:
        raise SceneConfigError("metric_marker.id must be a non-negative integer")
    if not isinstance(marker.get("size_m"), (int, float)) or marker["size_m"] <= 0:
        raise SceneConfigError("metric_marker.size_m must be positive")
    _require_path(marker, "world_alignment", "metric_marker")
    _require_path(marker, "corners_world", "metric_marker")
    if "quality" in marker:
        _require_path(marker, "quality", "metric_marker")
    if "enabled" in marker and not isinstance(marker["enabled"], bool):
        raise SceneConfigError("metric_marker.enabled must be boolean")
    quality_gate = marker.get("quality_gate")
    if quality_gate is not None:
        if not isinstance(quality_gate, dict):
            raise SceneConfigError("metric_marker.quality_gate must be an object")
        expected_quality_gate = {
            "min_observation_count_per_corner",
            "max_side_cv",
            "max_diagonal_ratio",
            "max_normalized_ray_residual",
        }
        if set(quality_gate) != expected_quality_gate:
            raise SceneConfigError("metric_marker.quality_gate keys are invalid")
        for key, value in quality_gate.items():
            if not isinstance(value, (int, float)) or value < 0:
                raise SceneConfigError(f"metric_marker.quality_gate.{key} must be non-negative")
        if quality_gate["min_observation_count_per_corner"] < 1:
            raise SceneConfigError("metric_marker.quality_gate.min_observation_count_per_corner must be at least one")
        if quality_gate["max_diagonal_ratio"] < 1:
            raise SceneConfigError("metric_marker.quality_gate.max_diagonal_ratio must be at least one")

    dense = config.get("dense")
    if dense is not None:
        dense = _require_mapping(dense, "dense")
        for key in ("workspace", "config", "world_alignment"):
            _require_path(dense, key, "dense")
        if dense.get("quality") not in {"dense_quality.json", ""}:
            _require_path(dense, "quality", "dense")
        if dense.get("profile") not in {"full", "reuse"}:
            raise SceneConfigError("dense.profile must be full or reuse")

    proxy = config.get("proxy")
    if proxy is not None:
        proxy = _require_mapping(proxy, "proxy")
        _require_string(proxy, "mode", "proxy")
        _require_path(proxy, "config", "proxy")
        if proxy["mode"] != "surface_mesh":
            raise SceneConfigError("proxy.mode must be surface_mesh for the current contract")

    execution = _require_mapping(config["execution"], "execution")
    profile = execution.get("profile")
    if profile not in PROFILES:
        raise SceneConfigError("execution.profile must be fast, quality or auto")
    for key in ("resume", "dry_run"):
        if key in execution and not isinstance(execution[key], bool):
            raise SceneConfigError(f"execution.{key} must be boolean")
    profiles = execution.get("profiles", {})
    if not isinstance(profiles, dict):
        raise SceneConfigError("execution.profiles must be an object")
    for profile_name, settings in profiles.items():
        if profile_name not in PROFILES or not isinstance(settings, dict):
            raise SceneConfigError("execution.profiles keys must be fast, quality or auto objects")

    reuse = _require_mapping(config["reuse"], "reuse")
    for key in ("person_mask", "sfm"):
        if not isinstance(reuse.get(key), bool):
            raise SceneConfigError(f"reuse.{key} must be boolean")
    for key in ("spz", "dense", "proxy"):
        if key in reuse and not isinstance(reuse[key], bool):
            raise SceneConfigError(f"reuse.{key} must be boolean")

    build = config.get("build")
    if build is not None:
        build = _require_mapping(build, "build")
        _require_path(build, "working_directory", "build")
        command = build.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
            raise SceneConfigError("build.command must be a non-empty string array")

    if repo_root is not None and check_paths:
        root = repo_root.resolve()
        paths = {
            "input_video": config["input_video"],
            "workspace": config["workspace"],
            "virtual_rig.config": virtual_rig["config"],
            "sfm.model": sfm["model"],
            "sfm.manifest": sfm["manifest"],
            "sfm.images": sfm["images"],
            "sfm.rig_config": sfm["rig_config"],
            "person_mask.config": person_mask["config"],
            "metric_marker.world_alignment": marker["world_alignment"],
            "metric_marker.corners_world": marker["corners_world"],
        }
        for key in ("scene_package_output", "viewer_publish_output"):
            if key in config:
                paths[key] = config[key]
        if isinstance(gsplat, dict):
            paths["gsplat.spz"] = gsplat["spz"]
            paths["gsplat.manifest"] = gsplat["manifest"]
        if isinstance(dense, dict):
            paths["dense.workspace"] = dense["workspace"]
            paths["dense.config"] = dense["config"]
            paths["dense.world_alignment"] = dense["world_alignment"]
        if isinstance(proxy, dict):
            paths["proxy.config"] = proxy["config"]
        if person_mask:
            for key in ("config", "static_masks", "person_masks", "colmap_masks", "report", "model_asset"):
                if key in person_mask:
                    paths[f"person_mask.{key}"] = person_mask[key]
        missing_paths = []
        for name, value in paths.items():
            candidate = root / value
            if name == "scene_package_output":
                if not candidate.parent.exists():
                    missing_paths.append(name)
            elif not candidate.exists():
                missing_paths.append(name)
        if missing_paths:
            raise SceneConfigError("missing configured paths: " + ", ".join(missing_paths))

    return config


def load_scene_config(path: Path, *, repo_root: Path | None = None, check_paths: bool = False) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SceneConfigError(f"cannot read scene config: {path}: {error}") from error
    if not isinstance(config, dict):
        raise SceneConfigError("scene config root must be an object")
    return validate_scene_config(config, repo_root=repo_root, check_paths=check_paths)
