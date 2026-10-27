from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageOps, UnidentifiedImageError

if __package__:
    from .scene_objects import validate_scene_objects
    from .quaternion_contract import QUATERNION_UNIT_NORM_TOLERANCE
else:
    from scene_objects import validate_scene_objects
    from quaternion_contract import QUATERNION_UNIT_NORM_TOLERANCE


LEGACY_REQUIRED_MANIFEST_KEYS = (
    "schema_version", "scene_id", "unit", "coordinate_system", "assets", "configs", "annotations"
)
MODERN_REQUIRED_MANIFEST_KEYS = (
    "schema_version", "scene_id", "reconstruction_id", "unit", "coordinate_system",
    "metric_reconstruction", "assets", "configs", "annotations", "capabilities", "provenance",
)
IMMUTABLE_REQUIRED_MANIFEST_KEYS = (
    "schema_version", "project_id", "scene_id", "scene_package_id", "reconstruction_id", "unit",
    "coordinate_system", "dynamic_overlay", "metric_reconstruction", "assets", "configs",
    "annotations", "licenses", "capabilities", "provenance",
)
SELF_CONTAINED_REQUIRED_MANIFEST_KEYS = (*IMMUTABLE_REQUIRED_MANIFEST_KEYS, "dataset")
SUPPORTED_SCHEMAS = {"1.0.0", "1.1.0", "1.2.0", "1.3.0"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _asset_path(package: Path, uri: str, label: str) -> Path:
    if not isinstance(uri, str) or not uri or uri.startswith("/") or ".." in Path(uri).parts:
        raise ValueError(f"invalid {label} URI")
    candidate = (package / uri).resolve()
    if package.resolve() not in candidate.parents:
        raise ValueError(f"{label} escapes package")
    if not candidate.is_file():
        raise ValueError(f"missing {label}: {uri}")
    return candidate


def _asset_directory(package: Path, uri: str, label: str) -> Path:
    if not isinstance(uri, str) or not uri or uri.startswith("/") or ".." in Path(uri).parts:
        raise ValueError(f"invalid {label} URI")
    candidate = (package / uri).resolve()
    if package.resolve() not in candidate.parents:
        raise ValueError(f"{label} escapes package")
    if not candidate.is_dir():
        raise ValueError(f"missing {label}: {uri}")
    if candidate.is_symlink() or any(path.is_symlink() for path in candidate.rglob("*")):
        raise ValueError(f"{label} symlink not allowed")
    return candidate


def _directory_file_count(path: Path) -> int:
    return sum(1 for item in path.rglob("*") if item.is_file())


def _directory_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        digest.update(item.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256(item).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _sqlite_quick_check(path: Path, label: str, failures: list[str]) -> None:
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
        if result is None or result[0] != "ok":
            failures.append(f"{label}_sqlite_quick_check_failed")
    except sqlite3.Error:
        failures.append(f"{label}_sqlite_invalid")


def _validate_self_contained_dataset(
    package: Path,
    manifest: dict[str, Any],
    files: dict[str, dict[str, Any]],
    failures: list[str],
) -> None:
    try:
        reference = manifest["dataset"]
        if not isinstance(reference, dict):
            raise ValueError("scene_dataset_reference_invalid")
        dataset_path = _asset_path(package, reference["manifest_uri"], "scene_dataset_manifest")
        expected_hash = _require_sha256(reference.get("manifest_sha256"), "scene_dataset_manifest")
        actual_hash = _sha256(dataset_path)
        files[reference["manifest_uri"]] = {"size": dataset_path.stat().st_size, "sha256": actual_hash}
        if actual_hash != expected_hash:
            failures.append("scene_dataset_manifest_hash_mismatch")
        dataset = _read_json(dataset_path)
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as error:
        failures.append(str(error))
        return

    dataset_version = dataset.get("schema_version")
    if dataset_version not in {"1.0.0", "1.1.0"} or dataset.get("kind") != "spatial_scene_dataset":
        failures.append("scene_dataset_manifest_invalid")
    if dataset.get("status") != "verified":
        failures.append("scene_dataset_not_verified")
    for key in ("scene_id", "reconstruction_id", "scene_package_id"):
        if dataset.get(key) != manifest.get(key):
            failures.append(f"scene_dataset_{key}_mismatch")
    interval = dataset.get("frame_interval_sec")
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) or not math.isfinite(float(interval)) or float(interval) <= 0:
        failures.append("scene_dataset_frame_interval_invalid")

    artifacts = dataset.get("artifacts")
    if not isinstance(artifacts, dict):
        failures.append("scene_dataset_artifacts_missing")
        return
    def dataset_file(uri: object, label: str) -> Path:
        if not isinstance(uri, str):
            raise ValueError(f"scene_dataset_{label}_uri_invalid")
        relative = (Path(reference["manifest_uri"]).parent / uri).as_posix()
        return _asset_path(package, relative, f"scene_dataset_{label}")

    def dataset_directory(uri: object, label: str) -> Path:
        if not isinstance(uri, str):
            raise ValueError(f"scene_dataset_{label}_uri_invalid")
        relative = (Path(reference["manifest_uri"]).parent / uri).as_posix()
        return _asset_directory(package, relative, f"scene_dataset_{label}")

    try:
        metric = artifacts["metric_reconstruction"]
        metric_manifest = dataset_file(metric["manifest_uri"], "metric_reconstruction_manifest")
        metric_hash = _sha256(metric_manifest)
        if metric_hash != _require_sha256(metric.get("manifest_sha256"), "scene_dataset_metric_manifest"):
            failures.append("scene_dataset_metric_manifest_hash_mismatch")
        files[str(metric_manifest.relative_to(package)).replace("\\", "/")] = {
            "size": metric_manifest.stat().st_size,
            "sha256": metric_hash,
        }

        frames = artifacts["registered_frames"]
        frame_root = dataset_directory(frames["uri"], "registered_frames")
        frame_count = _directory_file_count(frame_root)
        if frame_count != frames.get("file_count") or frame_count <= 0:
            failures.append("scene_dataset_registered_frame_count_mismatch")
        dataset_file(frames["manifest_uri"], "registered_frame_manifest")
        if dataset_version == "1.1.0" and _directory_sha256(frame_root) != _require_sha256(
            frames.get("source_sha256"), "scene_dataset_registered_frames"
        ):
            failures.append("scene_dataset_registered_frame_hash_mismatch")

        masks = artifacts["person_masks"]
        mask_root = dataset_directory(masks["uri"], "person_masks")
        mask_count = _directory_file_count(mask_root)
        if mask_count != masks.get("file_count") or mask_count != frame_count:
            failures.append("scene_dataset_person_mask_count_mismatch")
        if dataset_version == "1.0.0":
            dataset_file(masks["manifest_uri"], "person_mask_manifest")
        elif masks.get("convention") != "white_is_person":
            failures.append("scene_dataset_person_mask_convention_invalid")
        if dataset_version == "1.1.0" and _directory_sha256(mask_root) != _require_sha256(
            masks.get("source_sha256"), "scene_dataset_person_masks"
        ):
            failures.append("scene_dataset_person_mask_hash_mismatch")

        if dataset_version == "1.1.0":
            colmap_masks = artifacts["colmap_masks"]
            colmap_mask_root = dataset_directory(colmap_masks["uri"], "colmap_masks")
            colmap_mask_count = _directory_file_count(colmap_mask_root)
            if colmap_mask_count != colmap_masks.get("file_count") or colmap_mask_count != frame_count:
                failures.append("scene_dataset_colmap_mask_count_mismatch")
            if colmap_masks.get("convention") != "white_is_retained":
                failures.append("scene_dataset_colmap_mask_convention_invalid")
            if _directory_sha256(colmap_mask_root) != _require_sha256(
                colmap_masks.get("source_sha256"), "scene_dataset_colmap_masks"
            ):
                failures.append("scene_dataset_colmap_mask_hash_mismatch")
            person_paths = {
                path.relative_to(mask_root): path
                for path in mask_root.rglob("*") if path.is_file()
            }
            colmap_paths = {
                path.relative_to(colmap_mask_root): path
                for path in colmap_mask_root.rglob("*") if path.is_file()
            }
            frame_paths = {
                path.relative_to(frame_root): path
                for path in frame_root.rglob("*") if path.is_file()
            }
            mask_image_keys = {
                relative: (
                    relative
                    if relative in frame_paths
                    else Path(relative.as_posix()[:-4])
                )
                for relative in person_paths
                if relative.suffix.lower() == ".png"
            }
            if (
                set(mask_image_keys.values()) != frame_paths.keys()
                or len(mask_image_keys) != len(person_paths)
                or len(set(mask_image_keys.values())) != len(mask_image_keys)
            ):
                failures.append("scene_dataset_mask_image_mapping_mismatch")
            if person_paths.keys() != colmap_paths.keys():
                failures.append("scene_dataset_mask_pair_mapping_mismatch")
            else:
                for relative, person_path in person_paths.items():
                    image_relative = mask_image_keys.get(relative)
                    try:
                        with Image.open(person_path) as opened:
                            person = opened.convert("L")
                            person_size = opened.size
                        with Image.open(colmap_paths[relative]) as opened:
                            colmap_mask = opened.convert("L")
                            colmap_size = opened.size
                        source_image = frame_paths.get(image_relative) if image_relative else None
                        if source_image is None:
                            continue
                        with Image.open(source_image) as opened:
                            source_size = opened.size
                    except (OSError, UnidentifiedImageError):
                        failures.append(f"scene_dataset_mask_pair_invalid:{relative.as_posix()}")
                        break
                    if person_size != colmap_size or person_size != source_size:
                        failures.append(f"scene_dataset_mask_pair_dimensions_mismatch:{relative.as_posix()}")
                        break
                    if sum(person.histogram()[1:255]) != 0:
                        failures.append(f"scene_dataset_person_mask_not_binary:{relative.as_posix()}")
                        break
                    if ImageChops.difference(
                        colmap_mask, ImageOps.invert(person)
                    ).getbbox() is not None:
                        failures.append(f"scene_dataset_colmap_mask_not_inverse:{relative.as_posix()}")
                        break
            reconstruction_masks = artifacts["reconstruction_masks"]
            reconstruction_mask_root = dataset_directory(
                reconstruction_masks["uri"], "reconstruction_masks"
            )
            reconstruction_mask_count = _directory_file_count(reconstruction_mask_root)
            if (
                reconstruction_mask_count != reconstruction_masks.get("file_count")
                or reconstruction_mask_count != frame_count
            ):
                failures.append("scene_dataset_reconstruction_mask_count_mismatch")
            if reconstruction_masks.get("convention") != "mask_set_used_by_packaged_colmap_database":
                failures.append("scene_dataset_reconstruction_mask_convention_invalid")
            if _directory_sha256(reconstruction_mask_root) != _require_sha256(
                reconstruction_masks.get("source_sha256"), "scene_dataset_reconstruction_masks"
            ):
                failures.append("scene_dataset_reconstruction_mask_hash_mismatch")
            dataset_file(reconstruction_masks["manifest_uri"], "reconstruction_mask_manifest")
            mask_generation = artifacts["mask_generation"]
            mask_report_path = dataset_file(mask_generation["report_uri"], "mask_generation_report")
            mask_report_hash = _sha256(mask_report_path)
            if mask_report_hash != _require_sha256(
                mask_generation.get("report_sha256"), "scene_dataset_mask_generation_report"
            ):
                failures.append("scene_dataset_mask_generation_report_hash_mismatch")
            mask_report = _read_json(mask_report_path)
            if mask_report.get("status") != "verified" or mask_generation.get("status") != "verified":
                failures.append("scene_dataset_mask_generation_not_verified")
            if mask_report.get("person_mask_count") != mask_count:
                failures.append("scene_dataset_mask_generation_person_count_mismatch")
            if mask_report.get("colmap_mask_count") != colmap_mask_count:
                failures.append("scene_dataset_mask_generation_colmap_count_mismatch")
            if mask_report.get("backend") == "grounding_dino_sam2":
                model = mask_report.get("model")
                configuration = mask_report.get("configuration")
                if not isinstance(configuration, dict):
                    failures.append("scene_dataset_mask_generation_configuration_missing")
                else:
                    quality_gate = configuration.get("quality_gate")
                    configured_model = configuration.get("model")
                    if (
                        configuration.get("backend") != "grounding_dino_sam2"
                        or not isinstance(configured_model, dict)
                        or not isinstance(model, dict)
                        or any(
                            configured_model.get(field) != model.get(field)
                            for field in (
                                "detector", "detector_revision", "segmenter",
                                "segmenter_revision", "runtime_package",
                            )
                        )
                        or not isinstance(configuration.get("box_threshold"), (int, float))
                        or not isinstance(configuration.get("text_threshold"), (int, float))
                        or not isinstance(quality_gate, dict)
                        or not isinstance(
                            quality_gate.get("maximum_person_coverage_ratio"), (int, float)
                        )
                    ):
                        failures.append("scene_dataset_mask_generation_configuration_invalid")
                if not isinstance(model, dict):
                    failures.append("scene_dataset_mask_generation_model_missing")
                else:
                    for field in ("detector_revision", "segmenter_revision"):
                        revision = model.get(field)
                        if (
                            not isinstance(revision, str)
                            or len(revision) != 40
                            or any(character not in "0123456789abcdef" for character in revision)
                        ):
                            failures.append(f"scene_dataset_mask_generation_{field}_invalid")

        colmap = artifacts["colmap"]
        database = dataset_file(colmap["database_uri"], "colmap_database")
        database_hash = _sha256(database)
        if database_hash != _require_sha256(colmap.get("database_sha256"), "scene_dataset_colmap_database"):
            failures.append("scene_dataset_colmap_database_hash_mismatch")
        _sqlite_quick_check(database, "scene_dataset_colmap_database", failures)
        files[str(database.relative_to(package)).replace("\\", "/")] = {
            "size": database.stat().st_size,
            "sha256": database_hash,
        }
        text_root = dataset_directory(colmap["text_uri"], "colmap_text")
        sparse_root = dataset_directory(colmap["sparse_uri"], "colmap_sparse")
        if _directory_file_count(text_root) != colmap.get("text_file_count"):
            failures.append("scene_dataset_colmap_text_count_mismatch")
        if _directory_file_count(sparse_root) != colmap.get("sparse_file_count"):
            failures.append("scene_dataset_colmap_sparse_count_mismatch")
        for name in ("cameras.txt", "images.txt", "points3D.txt"):
            if not (text_root / name).is_file():
                failures.append(f"scene_dataset_colmap_text_missing_{name}")

        memory = artifacts["field_memory"]
        memory_db = dataset_file(memory["database_uri"], "field_memory_database")
        memory_hash = _sha256(memory_db)
        if memory_hash != _require_sha256(memory.get("database_sha256"), "scene_dataset_field_memory_database"):
            failures.append("scene_dataset_field_memory_database_hash_mismatch")
        _sqlite_quick_check(memory_db, "scene_dataset_field_memory_database", failures)
        content_manifest_path = dataset_file(memory["content_manifest_uri"], "field_memory_content_manifest")
        content_manifest_hash = _sha256(content_manifest_path)
        if content_manifest_hash != _require_sha256(
            memory.get("content_manifest_sha256"), "scene_dataset_field_memory_content_manifest"
        ):
            failures.append("scene_dataset_field_memory_content_manifest_hash_mismatch")
        content_manifest = _read_json(content_manifest_path)
        content_files = content_manifest.get("files")
        if not isinstance(content_files, list) or len(content_files) != memory.get("content_file_count"):
            failures.append("scene_dataset_field_memory_content_count_mismatch")
        else:
            content_root = dataset_directory(memory["content_uri"], "field_memory_content") if content_files else None
            for entry in content_files:
                digest = entry.get("sha256") if isinstance(entry, dict) else None
                if not isinstance(digest, str) or len(digest) != 64:
                    failures.append("scene_dataset_field_memory_content_entry_invalid")
                    break
                path = content_root / digest[:2] / digest if content_root is not None else None
                if path is None or not path.is_file() or path.stat().st_size != entry.get("size"):
                    failures.append(f"scene_dataset_field_memory_content_missing:{digest}")
                    break
        files[str(memory_db.relative_to(package)).replace("\\", "/")] = {
            "size": memory_db.stat().st_size,
            "sha256": memory_hash,
        }
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as error:
        failures.append(str(error))


def _require_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"invalid {label} sha256")
    return value


def _finite_vector(value: object, length: int, label: str, failures: list[str]) -> bool:
    if not isinstance(value, list) or len(value) != length:
        failures.append(f"{label}_shape_invalid")
        return False
    if not all(isinstance(item, (int, float)) and math.isfinite(float(item)) for item in value):
        failures.append(f"{label}_non_finite")
        return False
    return True


def _validate_transform(value: object, label: str, failures: list[str]) -> None:
    if not isinstance(value, dict):
        failures.append(f"{label}_invalid")
        return
    translation_ok = _finite_vector(value.get("translation_m"), 3, f"{label}_translation", failures)
    rotation_ok = _finite_vector(value.get("rotation_xyzw"), 4, f"{label}_rotation", failures)
    if rotation_ok:
        norm = math.sqrt(sum(float(item) ** 2 for item in value["rotation_xyzw"]))
        if norm <= 1e-12:
            failures.append(f"{label}_zero_quaternion")
        elif abs(norm - 1.0) > QUATERNION_UNIT_NORM_TOLERANCE:
            failures.append(f"{label}_quaternion_not_normalized")
    if not translation_ok or not rotation_ok:
        return


def _validate_fixed_tag_map(value: dict[str, Any], failures: list[str]) -> None:
    if value.get("schema_version") != "1.0.0":
        failures.append("fixed_tag_map_unsupported_schema")
    if value.get("coordinate_frame") != "gs_world":
        failures.append("fixed_tag_map_coordinate_frame_mismatch")
    if value.get("unit") != "meter":
        failures.append("fixed_tag_map_not_meter")
    if value.get("rotation_order") != "xyzw":
        failures.append("fixed_tag_map_rotation_order_mismatch")
    frames = value.get("frames")
    if not isinstance(frames, dict) or not frames:
        failures.append("fixed_tag_map_frames_missing")
        return
    for frame_id, pose in frames.items():
        if not isinstance(frame_id, str) or not frame_id:
            failures.append("fixed_tag_map_frame_id_invalid")
            continue
        _validate_transform(pose, f"fixed_tag_map_{frame_id}", failures)


def _validate_dynamic_objects(
    package: Path,
    value: dict[str, Any],
    fixed_tag_map: dict[str, Any] | None,
    files: dict[str, dict[str, Any]],
    failures: list[str],
    *,
    manifest_schema_version: str,
) -> None:
    if value.get("schema_version") != "1.0.0":
        failures.append("dynamic_objects_unsupported_schema")
    if value.get("coordinate_frame") != "gs_world":
        failures.append("dynamic_objects_coordinate_frame_mismatch")
    if value.get("unit") != "meter":
        failures.append("dynamic_objects_not_meter")
    if value.get("rotation_order") != "xyzw":
        failures.append("dynamic_objects_rotation_order_mismatch")
    objects = value.get("objects")
    if not isinstance(objects, list):
        failures.append("dynamic_objects_list_missing")
        return
    seen_ids: set[str] = set()
    fixed_frames = set((fixed_tag_map or {}).get("frames", {}).keys())
    for index, item in enumerate(objects):
        prefix = f"dynamic_object_{index}"
        if not isinstance(item, dict):
            failures.append(f"{prefix}_invalid")
            continue
        if manifest_schema_version in {"1.2.0", "1.3.0"} and "timeout_ms" in item:
            failures.append(f"{prefix}_timeout_ms_not_allowed_in_v1_2")
        object_id = item.get("id")
        if not isinstance(object_id, str) or not re.fullmatch(r"box_0*[1-9][0-9]*", object_id) or len(object_id) > 64:
            failures.append(f"{prefix}_id_invalid")
        elif object_id in seen_ids:
            failures.append(f"duplicate_dynamic_object_{object_id}")
        else:
            seen_ids.add(object_id)
        tag_frame = item.get("box_tag_frame")
        if tag_frame is not None and (not isinstance(tag_frame, str) or not tag_frame):
            failures.append(f"{prefix}_tag_frame_invalid")
        elif isinstance(tag_frame, str) and tag_frame in fixed_frames:
            failures.append(f"{prefix}_tag_frame_must_be_dynamic")
        if "box_tag_to_object" in item:
            _validate_transform(item.get("box_tag_to_object"), f"{prefix}_box_tag_to_object", failures)
        if "object_to_render" in item:
            _validate_transform(item.get("object_to_render"), f"{prefix}_object_to_render", failures)
        model = item.get("model")
        if model is None:
            pass
        elif not isinstance(model, dict) or model.get("format") not in {"glb", "cube"}:
            failures.append(f"{prefix}_model_invalid")
        elif model.get("format") == "cube":
            dimensions = model.get("dimensions_m")
            named_dimensions = isinstance(dimensions, dict) and all(
                isinstance(dimensions.get(field), (int, float))
                and math.isfinite(float(dimensions[field]))
                and float(dimensions[field]) > 0
                for field in ("width_m", "depth_m", "height_m")
            )
            # Legacy arrays are accepted only at the read boundary as
            # [width, depth, height]. New Scene Packages store named values.
            legacy_dimensions = (
                isinstance(dimensions, list)
                and len(dimensions) == 3
                and all(isinstance(item, (int, float)) and math.isfinite(float(item)) and float(item) > 0 for item in dimensions)
            )
            if not named_dimensions and not legacy_dimensions:
                failures.append(f"{prefix}_cube_dimensions_invalid")
        else:
            try:
                model_path = _asset_path(package, model["uri"], f"{prefix}_model")
                actual_hash = _sha256(model_path)
                expected_hash = _require_sha256(model.get("sha256"), f"{prefix}_model")
                files[model["uri"]] = {"size": model_path.stat().st_size, "sha256": actual_hash}
                if model_path.read_bytes()[:4] != b"glTF":
                    failures.append(f"{prefix}_model_invalid_glb_header")
                if actual_hash != expected_hash:
                    failures.append(f"{prefix}_model_hash_mismatch")
            except (KeyError, TypeError, ValueError, OSError) as error:
                failures.append(str(error))
        for field in ("timeout_ms", "max_translation_jump_m", "max_rotation_jump_deg"):
            if field not in item:
                continue
            limit = item[field]
            if not isinstance(limit, (int, float)) or not math.isfinite(float(limit)) or float(limit) <= 0:
                failures.append(f"{prefix}_{field}_invalid")


def _validate_modern_manifest(
    package: Path,
    manifest: dict[str, Any],
    files: dict[str, dict[str, Any]],
    failures: list[str],
) -> None:
    reconstruction_id = manifest.get("reconstruction_id")
    if not isinstance(reconstruction_id, str) or not reconstruction_id:
        failures.append("missing_reconstruction_id")

    capabilities = manifest.get("capabilities") if isinstance(manifest.get("capabilities"), dict) else {}
    required_capabilities = ("display", "measurement", "surface_placement", "fov", "occlusion")
    for capability in required_capabilities:
        if not isinstance(capabilities.get(capability), bool):
            failures.append(f"invalid_capability_{capability}")
    if capabilities.get("display") is not True:
        failures.append("display_capability_not_enabled")

    try:
        metric = manifest["metric_reconstruction"]
        if not isinstance(metric, dict):
            raise ValueError("invalid_metric_reconstruction_reference")
        reference = _asset_path(package, metric["manifest_uri"], "metric_reconstruction_manifest")
        expected_hash = _require_sha256(metric["manifest_sha256"], "metric_reconstruction_manifest")
        actual_hash = _sha256(reference)
        if actual_hash != expected_hash:
            failures.append("metric_reconstruction_manifest_hash_mismatch")
        linked = _read_json(reference)
        if linked.get("schema_version") != "1.0.0" or linked.get("package_kind") != "metric_reconstruction":
            failures.append("invalid_metric_reconstruction_manifest")
        if linked.get("scene_id") != manifest.get("scene_id"):
            failures.append("metric_reconstruction_scene_id_mismatch")
        if linked.get("reconstruction_id") != reconstruction_id:
            failures.append("metric_reconstruction_id_mismatch")
        files[metric["manifest_uri"]] = {"size": reference.stat().st_size, "sha256": actual_hash}
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as error:
        failures.append(str(error))

    provenance = manifest.get("provenance") if isinstance(manifest.get("provenance"), dict) else {}
    gaussian = provenance.get("gaussian") if isinstance(provenance.get("gaussian"), dict) else {}
    for key in ("source_format", "runtime_format"):
        if gaussian.get(key) not in {"spz", "ply", "rad"}:
            failures.append(f"invalid_gaussian_{key}")
    for key in ("source_sha256", "runtime_sha256"):
        try:
            _require_sha256(gaussian.get(key), f"gaussian_{key}")
        except ValueError as error:
            failures.append(str(error))
    if provenance.get("coordinate_stage") not in {"raw_lfs", "horizontalized_lfs", "viewer_ready", "world_meter"}:
        failures.append("invalid_gaussian_coordinate_stage")


def _validate_axis_contract(value: object, label: str, failures: list[str]) -> None:
    if not isinstance(value, dict):
        failures.append(f"{label}_missing")
        return
    if value.get("handedness") != "right":
        failures.append(f"{label}_handedness_invalid")
    for axis in ("x", "y", "z"):
        if not isinstance(value.get(axis), str) or not value[axis]:
            failures.append(f"{label}_{axis}_invalid")


def _validate_immutable_manifest(manifest: dict[str, Any], failures: list[str]) -> None:
    for identifier in ("project_id", "scene_id", "scene_package_id"):
        value = manifest.get(identifier)
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
            failures.append(f"invalid_{identifier}")

    coordinate = manifest.get("coordinate_system")
    if not isinstance(coordinate, dict):
        failures.append("coordinate_system_invalid")
    else:
        _validate_axis_contract(coordinate.get("ros"), "coordinate_ros", failures)
        _validate_axis_contract(coordinate.get("viewer"), "coordinate_viewer", failures)
        transform = coordinate.get("T_viewer_ros")
        if not isinstance(transform, dict):
            failures.append("coordinate_T_viewer_ros_missing")
        else:
            translation_ok = _finite_vector(transform.get("translation"), 3, "coordinate_T_viewer_ros_translation", failures)
            rotation_ok = _finite_vector(transform.get("rotation_xyzw"), 4, "coordinate_T_viewer_ros_rotation", failures)
            if rotation_ok:
                norm = math.sqrt(sum(float(item) ** 2 for item in transform["rotation_xyzw"]))
                if norm <= 1e-12:
                    failures.append("coordinate_T_viewer_ros_zero_quaternion")
                elif abs(norm - 1.0) > QUATERNION_UNIT_NORM_TOLERANCE:
                    failures.append("coordinate_T_viewer_ros_quaternion_not_normalized")
            if not translation_ok or not rotation_ok:
                failures.append("coordinate_T_viewer_ros_invalid")

    overlay = manifest.get("dynamic_overlay")
    freshness = overlay.get("freshness") if isinstance(overlay, dict) else None
    if not isinstance(overlay, dict) or overlay.get("topic") != "/box/relative_transform":
        failures.append("dynamic_overlay_topic_invalid")
    if not isinstance(freshness, dict):
        failures.append("dynamic_overlay_freshness_missing")
    else:
        timeout = freshness.get("timeout_ms")
        if not isinstance(timeout, (int, float)) or not math.isfinite(float(timeout)) or float(timeout) <= 0:
            failures.append("dynamic_overlay_timeout_invalid")
        if freshness.get("on_timeout") != "hide":
            failures.append("dynamic_overlay_on_timeout_invalid")
        if freshness.get("ordering") != "ros_stamp":
            failures.append("dynamic_overlay_ordering_invalid")


def validate_scene_package(package: Path, expected_scene_id: str | None = None) -> dict[str, Any]:
    package = package.resolve()
    manifest_path = package / "scene_manifest.json"
    manifest = _read_json(manifest_path)
    failures: list[str] = []
    warnings: list[str] = []
    schema_version = manifest.get("schema_version")
    required_keys = (
        SELF_CONTAINED_REQUIRED_MANIFEST_KEYS if schema_version == "1.3.0"
        else IMMUTABLE_REQUIRED_MANIFEST_KEYS if schema_version == "1.2.0"
        else MODERN_REQUIRED_MANIFEST_KEYS if schema_version == "1.1.0"
        else LEGACY_REQUIRED_MANIFEST_KEYS
    )
    for key in required_keys:
        if key not in manifest:
            failures.append(f"manifest_missing_{key}")
    if schema_version not in SUPPORTED_SCHEMAS:
        failures.append("unsupported_manifest_schema")
    if expected_scene_id and manifest.get("scene_id") != expected_scene_id:
        failures.append("scene_id_mismatch")
    if manifest.get("unit") != "meter":
        failures.append("manifest_not_meter")
    if manifest.get("coordinate_system", {}).get("up_axis") != "Y":
        failures.append("manifest_not_y_up")

    files: dict[str, dict[str, Any]] = {}
    assets = manifest.get("assets", {})
    splats = assets.get("splats", []) if isinstance(assets, dict) else []
    if not isinstance(splats, list) or not splats:
        failures.append("missing_splat_asset")
    else:
        for index, asset in enumerate(splats):
            try:
                path = _asset_path(package, asset["uri"], f"splat_{index}")
                actual_hash = _sha256(path)
                files[asset["uri"]] = {"size": path.stat().st_size, "sha256": actual_hash}
                if schema_version in {"1.1.0", "1.2.0", "1.3.0"}:
                    if asset.get("source_format") not in {"spz", "ply", "rad"}:
                        failures.append(f"invalid_splat_{index}_source_format")
                    if asset.get("runtime_format") not in {"spz", "ply", "rad"}:
                        failures.append(f"invalid_splat_{index}_runtime_format")
                    if asset.get("coordinate_stage") not in {"raw_lfs", "horizontalized_lfs", "viewer_ready", "world_meter"}:
                        failures.append(f"invalid_splat_{index}_coordinate_stage")
                    expected_hash = _require_sha256(asset.get("sha256"), f"splat_{index}")
                    if actual_hash != expected_hash:
                        failures.append(f"splat_{index}_hash_mismatch")
            except (KeyError, ValueError) as error:
                failures.append(str(error))

    proxy = assets.get("proxy_mesh") if isinstance(assets, dict) else None
    quality: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    if proxy is None:
        warnings.append("proxy_mesh_not_registered")
        if schema_version in {"1.1.0", "1.2.0", "1.3.0"}:
            capabilities = manifest.get("capabilities", {})
            for capability in ("measurement", "surface_placement", "fov", "occlusion"):
                if capabilities.get(capability) is not False:
                    failures.append(f"proxyless_capability_not_disabled_{capability}")
    else:
        try:
            glb = _asset_path(package, proxy["uri"], "proxy_glb")
            metadata_path = _asset_path(package, proxy["metadata_uri"], "proxy_metadata")
            quality_path = _asset_path(package, proxy["quality_uri"], "proxy_quality")
            glb_hash = _sha256(glb)
            metadata_hash = _sha256(metadata_path)
            quality_hash = _sha256(quality_path)
            files[proxy["uri"]] = {"size": glb.stat().st_size, "sha256": glb_hash}
            files[proxy["metadata_uri"]] = {
                "size": metadata_path.stat().st_size,
                "sha256": metadata_hash,
            }
            files[proxy["quality_uri"]] = {
                "size": quality_path.stat().st_size,
                "sha256": quality_hash,
            }
            proxy_hash_fields = {
                "sha256": glb_hash,
                "metadata_sha256": metadata_hash,
                "quality_sha256": quality_hash,
            }
            for field, actual_hash in proxy_hash_fields.items():
                expected_hash = proxy.get(field)
                if schema_version == "1.3.0" and expected_hash is None:
                    failures.append(f"proxy_{field}_missing")
                elif expected_hash is not None and _require_sha256(expected_hash, f"proxy_{field}") != actual_hash:
                    failures.append(f"proxy_{field}_mismatch")
            if glb.read_bytes()[:4] != b"glTF":
                failures.append("invalid_proxy_glb_header")
            metadata = _read_json(metadata_path)
            quality = _read_json(quality_path)
            if quality.get("status") != "verified":
                failures.append("proxy_quality_not_verified")
            if quality.get("proxy_kind") != "surface_mesh":
                failures.append("unsupported_proxy_kind")
            if metadata.get("unit") != "meter" or metadata.get("coordinate_system", {}).get("up_axis") != "Y":
                failures.append("proxy_metadata_coordinate_mismatch")
            mesh = metadata.get("mesh", {})
            if mesh.get("vertex_count") != quality.get("vertex_count") or mesh.get("triangle_count") != quality.get("triangle_count"):
                failures.append("proxy_metadata_quality_count_mismatch")
            if not glb.stat().st_size:
                failures.append("empty_proxy_glb")
            if schema_version in {"1.1.0", "1.2.0", "1.3.0"}:
                capabilities = manifest.get("capabilities", {})
                for capability in ("measurement", "surface_placement", "fov", "occlusion"):
                    if capabilities.get(capability) is not True:
                        failures.append(f"proxy_capability_not_enabled_{capability}")
        except (KeyError, ValueError) as error:
            failures.append(str(error))

    configs = manifest.get("configs", {})
    for key, uri in configs.items() if isinstance(configs, dict) else []:
        try:
            path = _asset_path(package, uri, f"config_{key}")
            files[uri] = {"size": path.stat().st_size, "sha256": _sha256(path)}
        except (ValueError, TypeError) as error:
            failures.append(str(error))
    annotations = manifest.get("annotations", {})
    annotation_data: dict[str, dict[str, Any]] = {}
    for key, uri in annotations.items() if isinstance(annotations, dict) else []:
        try:
            path = _asset_path(package, uri, f"annotation_{key}")
            files[uri] = {"size": path.stat().st_size, "sha256": _sha256(path)}
            if key == "scene_objects":
                object_data = _read_json(path)
                object_validation = validate_scene_objects(object_data, expected_scene_id=manifest.get("scene_id"))
                if object_validation.get("status") != "verified":
                    failures.extend(f"scene_objects:{reason}" for reason in object_validation.get("failure_reasons", []))
            elif key in {"fixed_tag_map", "dynamic_objects"}:
                annotation_data[key] = _read_json(path)
        except (ValueError, TypeError) as error:
            failures.append(str(error))

    if "fixed_tag_map" in annotation_data:
        _validate_fixed_tag_map(annotation_data["fixed_tag_map"], failures)
    if "dynamic_objects" in annotation_data:
        _validate_dynamic_objects(
            package,
            annotation_data["dynamic_objects"],
            annotation_data.get("fixed_tag_map"),
            files,
            failures,
            manifest_schema_version=schema_version,
        )

    alignment_uri = configs.get("world_alignment") if isinstance(configs, dict) else None
    if alignment_uri:
        try:
            alignment = _read_json(_asset_path(package, alignment_uri, "world_alignment"))
            if alignment.get("status") != "verified" or alignment.get("unit") != "meter":
                failures.append("world_alignment_not_verified")
        except (ValueError, TypeError) as error:
            failures.append(str(error))

    if schema_version in {"1.1.0", "1.2.0", "1.3.0"}:
        _validate_modern_manifest(package, manifest, files, failures)
    if schema_version in {"1.2.0", "1.3.0"}:
        _validate_immutable_manifest(manifest, failures)
    if schema_version == "1.3.0":
        _validate_self_contained_dataset(package, manifest, files, failures)

    status = "verified" if not failures else "failed"
    return {
        "schema_version": schema_version if schema_version in SUPPORTED_SCHEMAS else "1.0.0",
        "stage": "scene_package_validation",
        "status": status,
        "scene_id": manifest.get("scene_id"),
        "package": str(package),
        "manifest": "scene_manifest.json",
        "asset_count": len(files),
        "assets": files,
        "failure_reasons": failures,
        "warnings": warnings,
    }


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a Scene Package and its asset references.")
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--scene-id")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = validate_scene_package(args.package, args.scene_id)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        result = {"schema_version": "1.0.0", "stage": "scene_package_validation", "status": "failed", "failure_reasons": [str(error)], "warnings": []}
    if args.output:
        atomic_json(args.output, result)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "verified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
