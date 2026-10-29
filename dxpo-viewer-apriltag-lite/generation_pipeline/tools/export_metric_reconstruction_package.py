from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if not __package__ and str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from packages.scene_repository import SceneRepositoryLayout
if __package__:
    from .validate_metric_reconstruction_package import validate_metric_reconstruction_package
else:
    from validate_metric_reconstruction_package import validate_metric_reconstruction_package


SCHEMA_VERSION = "1.0.0"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{label}_unreadable:{path}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"{label}_must_be_object:{path}")
    return value


def load_export_config(path: Path) -> dict[str, Any]:
    """Read the existing scene config without coupling export to later-stage validators."""
    config = read_json(path, "scene_config")
    for key in ("schema_version", "scene_id", "workspace", "sfm", "person_mask", "metric_marker"):
        if key not in config:
            raise RuntimeError(f"scene_config_missing:{key}")
    if config.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("unsupported_scene_config_schema")
    return config


def _files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise RuntimeError(f"source_missing:{path}")
    return sorted(item for item in path.rglob("*") if item.is_file())


def _relative_uri(path: Path, root: Path, label: str) -> str:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError as error:
        raise RuntimeError(f"{label}_outside_repo:{path}") from error
    if not relative.parts or ".." in relative.parts:
        raise RuntimeError(f"{label}_invalid_relative_uri:{path}")
    return relative.as_posix()


def _directory_hash(path: Path) -> str:
    digest = hashlib.sha256()
    for item in _files(path):
        relative = item.relative_to(path).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_file(item).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _source_hash(path: Path) -> str:
    return sha256_file(path) if path.is_file() else _directory_hash(path)


def _source_reference(path: Path, root: Path, *, kind: str) -> dict[str, Any]:
    files = _files(path)
    return {
        "schema_version": SCHEMA_VERSION,
        "stage": f"{kind}_reference",
        "status": "verified",
        "source_uri": _relative_uri(path, root, kind),
        "file_count": len(files),
        "source_sha256": _source_hash(path),
    }


def _write_source_reference(package: Path, relative: str, source: Path, root: Path, kind: str) -> None:
    atomic_json(package / relative / ".source.json", _source_reference(source, root, kind=kind))


def _file_names(path: Path) -> list[str]:
    return [item.relative_to(path).as_posix() for item in _files(path)]


def _canonical_stem(name: str) -> str:
    value = Path(name)
    while value.suffix:
        value = value.with_suffix("")
    return value.as_posix()


def _stem_names(path: Path) -> tuple[set[str], int, int]:
    names = _file_names(path)
    stems = [_canonical_stem(name) for name in names]
    return set(stems), len(stems) - len(set(stems)), sum(max(0, len(Path(name).suffixes) - 1) for name in names)


def _suffixes_by_stem(path: Path) -> dict[str, str]:
    return {_canonical_stem(name): Path(name).suffix.lower() for name in _file_names(path)}


def _require_status(report: dict[str, Any], label: str) -> None:
    if report.get("status") != "verified":
        raise RuntimeError(f"{label}_quality_not_verified")


def _colmap_image_names(model: Path) -> list[str]:
    """Read the registered names from a COLMAP text model.

    The image records are deliberately parsed here instead of inferred from a
    directory listing: COLMAP's registered image set is the contract passed to
    LichtFeld Studio.
    """
    path = model / "images.txt"
    if not path.is_file():
        raise RuntimeError(f"colmap_images_text_missing:{path}")
    names: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        # Image records have 10 header fields; the following line contains
        # point observations and has a variable number of fields.
        if len(fields) == 10:
            names.append(" ".join(fields[9:]))
    if not names or len(names) != len(set(names)):
        raise RuntimeError("colmap_registered_image_names_invalid")
    return names


def _image_mask_pairs(images: Path, masks: Path, registered_names: list[str]) -> list[tuple[str, Path, Path]]:
    image_names = _file_names(images)
    mask_names = _file_names(masks)
    if sorted(image_names) != sorted(registered_names):
        raise RuntimeError("colmap_image_names_do_not_match_source_images")
    if len(mask_names) != len(set(mask_names)):
        raise RuntimeError("duplicate_mask_names")
    by_stem: dict[str, list[str]] = {}
    for name in mask_names:
        by_stem.setdefault(_canonical_stem(name), []).append(name)
    pairs: list[tuple[str, Path, Path]] = []
    for name in registered_names:
        candidates = by_stem.get(_canonical_stem(name), [])
        if len(candidates) != 1:
            raise RuntimeError(f"image_mask_mapping_invalid:{name}")
        image = images / name
        mask = masks / candidates[0]
        if not image.is_file() or not mask.is_file():
            raise RuntimeError(f"image_mask_source_missing:{name}")
        pairs.append((name, image, mask))
    return pairs


def _copy_sparse_model(source: Path, destination: Path) -> None:
    required = ("cameras.bin", "images.bin", "points3D.bin")
    if not source.is_dir() or any(not (source / name).is_file() for name in required):
        raise RuntimeError(f"colmap_sparse_model_incomplete:{source}")
    shutil.copytree(source, destination, dirs_exist_ok=True)


def _resolve_lfs_sources(inputs: dict[str, Any]) -> tuple[Path, Path]:
    workspace = inputs["workspace"]
    model_candidates = [
        inputs["model"],
        workspace / "colmap_sequential" / "text",
        inputs["model"].parent / "text",
    ]
    registered = set(_file_names(inputs["images"]))
    for candidate in model_candidates:
        if candidate.is_dir() and (candidate / "images.txt").is_file():
            try:
                names = _colmap_image_names(candidate)
            except RuntimeError:
                continue
            if set(names) == registered and len(names) == len(registered):
                # A text model can be horizontalized independently of COLMAP's
                # binary sparse model.  Never pair that transformed text with
                # the original binary model: LichtFeld Studio reads the binary
                # poses and would otherwise receive a tilted trajectory.
                sparse = inputs["sparse_model"]
                if candidate.name == "text_horizontalized" and not inputs["sparse_model_explicit"]:
                    raise RuntimeError("horizontalized_sparse_model_missing")
                return candidate, sparse
    raise RuntimeError("lfs_colmap_model_image_set_unverified")


def export_lfs_dataset(inputs: dict[str, Any], reconstruction_id: str) -> dict[str, Any]:
    """Create and validate the canonical alpha-masked LFS dataset atomically."""
    output = inputs["workspace"] / "lfs_ready"
    model, sparse = _resolve_lfs_sources(inputs)
    registered_names = _colmap_image_names(model)
    pairs = _image_mask_pairs(inputs["images"], inputs["masks"], registered_names)
    staging = Path(tempfile.mkdtemp(prefix=".lfs_ready.staging.", dir=output.parent))
    staged = staging / "lfs_ready"
    alpha_stats = {
        "alpha_present_count": 0,
        "alpha_missing_count": 0,
        "fully_opaque_image_count": 0,
        "fully_transparent_image_count": 0,
        "masked_pixel_count": 0,
        "total_pixel_count": 0,
        "output_image_decode_success_count": 0,
        "source_mask_count": len(pairs),
        "source_mask_pixel_match_ratio": 0.0,
        "inverted_mask_suspected": False,
    }
    alpha_match_count = 0
    try:
        (staged / "images").mkdir(parents=True)
        _copy_sparse_model(sparse, staged / "sparse" / "0")
        shutil.copytree(model, staged / "colmap_text")
        image_manifest_names: list[str] = []
        for name, image_path, mask_path in pairs:
            try:
                with Image.open(image_path) as source_image, Image.open(mask_path) as source_mask:
                    image = source_image.convert("RGB")
                    mask = source_mask.convert("L")
            except (OSError, UnidentifiedImageError) as error:
                raise RuntimeError(f"image_or_mask_decode_failed:{name}") from error
            if image.size != mask.size:
                raise RuntimeError(f"image_mask_dimension_mismatch:{name}")
            alpha = mask
            output_path = staged / "images" / name
            output_path.parent.mkdir(parents=True, exist_ok=True)
            rgba = Image.merge("RGBA", (*image.split(), alpha))
            if output_path.suffix.lower() != ".png":
                raise RuntimeError(f"lfs_alpha_requires_png_name:{name}")
            rgba.save(output_path, format="PNG")
            values = list(alpha.getdata())
            with Image.open(output_path) as check:
                if check.mode != "RGBA" or check.size != image.size:
                    raise RuntimeError(f"lfs_rgba_validation_failed:{name}")
                output_alpha = list(check.getchannel("A").getdata())
                alpha_match_count += sum(
                    left == right for left, right in zip(values, output_alpha, strict=True)
                )
            alpha_stats["alpha_present_count"] += 1
            alpha_stats["output_image_decode_success_count"] += 1
            alpha_stats["masked_pixel_count"] += sum(value < 128 for value in values)
            alpha_stats["total_pixel_count"] += len(values)
            if min(values) == 255:
                alpha_stats["fully_opaque_image_count"] += 1
            if max(values) == 0:
                alpha_stats["fully_transparent_image_count"] += 1
            image_manifest_names.append(name)
        alpha_stats["masked_pixel_ratio"] = alpha_stats["masked_pixel_count"] / max(1, alpha_stats["total_pixel_count"])
        alpha_stats["source_mask_pixel_match_ratio"] = alpha_match_count / max(1, alpha_stats["total_pixel_count"])
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "stage": "lfs_input",
            "status": "verified",
            "dataset_kind": "lichtfeld_studio_input",
            "source_reconstruction_id": reconstruction_id,
            "images": {
                "uri": "images",
                "file_count": len(image_manifest_names),
                "files": image_manifest_names,
                "source_sha256": _source_hash(inputs["images"]),
            },
            "sparse": {
                "uri": "sparse/0",
                "required_files": ["cameras.bin", "images.bin", "points3D.bin"],
                "source_uri": inputs["sparse_model_uri"],
                "source_sha256": _source_hash(sparse),
            },
            "colmap_text": {"uri": "colmap_text", "registered_image_count": len(registered_names)},
            "mask": {
                "route": "alpha",
                "semantics": "alpha=255 retained, alpha=0 predicted person",
                "source_uri": "source masks are referenced by App A provenance",
                "source_mask_count": len(pairs),
                "source_sha256": _source_hash(inputs["masks"]),
                "alpha_stats": alpha_stats,
                "requires_lfs_options": {"mask_mode": "enabled", "use_alpha_as_mask": True},
            },
            "coordinate": {
                "stage": "horizontalized_colmap",
                "unit": "meter" if inputs["metric_scale_available"] else "colmap_unit",
                "up_axis": "Y",
                "transform_applied": "horizontal_rotation_only",
                "metric_scale_available": inputs["metric_scale_available"],
            },
            "provenance": {"image_source": "App A virtual images", "mask_source": "App A person masks", "model_source": "COLMAP fixed-rig registered model"},
            "failure_reasons": [],
            "warnings": ["LichtFeld Studio alpha-mask training acceptance is a separate external stage."],
        }
        atomic_json(staged / "manifest.json", manifest)
        atomic_json(staged / "export_report.json", {"schema_version": SCHEMA_VERSION, "stage": "lfs_export", "status": "verified", "manifest": "manifest.json", "alpha_stats": alpha_stats, "failure_reasons": [], "warnings": manifest["warnings"]})
        # Validate the staged output before touching an existing canonical one.
        _validate_lfs_dataset(staged, expected_count=len(pairs), expected_names=registered_names)
        backup = output.parent / f".lfs_ready.previous.{os.getpid()}"
        if output.exists():
            os.replace(output, backup)
        os.replace(staged, output)
        if backup.exists():
            _remove_path(backup)
        return manifest | {"path": str(output)}
    except Exception:
        # The canonical output is untouched until every validation above passes.
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _validate_lfs_dataset(path: Path, *, expected_count: int | None = None, expected_names: list[str] | None = None) -> dict[str, Any]:
    manifest = read_json(path / "manifest.json", "lfs_manifest")
    if manifest.get("status") != "verified" or manifest.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("lfs_manifest_not_verified")
    images = path / "images"
    sparse = path / "sparse" / "0"
    names = _file_names(images)
    if expected_count is not None and len(names) != expected_count:
        raise RuntimeError("lfs_image_count_mismatch")
    if expected_names is not None and sorted(names) != sorted(expected_names):
        raise RuntimeError("lfs_registered_image_name_mismatch")
    for required in ("cameras.bin", "images.bin", "points3D.bin"):
        if not (sparse / required).is_file():
            raise RuntimeError(f"lfs_sparse_missing:{required}")
    for name in names:
        with Image.open(images / name) as image:
            if image.mode != "RGBA":
                raise RuntimeError(f"lfs_image_not_rgba:{name}")
    return manifest


def _finite_number(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or not float("-inf") < float(value) < float("inf"):
        raise RuntimeError(f"{label}_not_finite")
    return float(value)


def _model_hash(path: Path) -> str:
    return _directory_hash(path)


def _write_image_manifest(package: Path, relative: str, source: Path, root: Path, kind: str) -> None:
    names = _file_names(source)
    atomic_json(
        package / relative / "manifest.json",
        {
            "schema_version": SCHEMA_VERSION,
            "stage": f"{kind}_set",
            "status": "verified",
            "source_uri": _relative_uri(source, root, kind),
            "file_count": len(names),
            "files": names,
            "source_sha256": _source_hash(source),
        },
    )
    _write_source_reference(package, relative, source, root, kind)


def _write_quality_reports(
    package: Path,
    input_manifest: dict[str, Any],
    mask_quality: dict[str, Any],
    sfm_quality: dict[str, Any],
    metric_quality: dict[str, Any],
) -> None:
    reports = {
        "input_quality.json": input_manifest,
        "mask_quality.json": mask_quality,
        "sfm_quality.json": sfm_quality,
        "metric_quality.json": metric_quality,
    }
    for name, report in reports.items():
        atomic_json(package / "quality" / name, report)


def _image_mapping(images: Path, lfs_images: Path) -> dict[str, int]:
    image_stems, image_duplicates, image_normalized = _stem_names(images)
    lfs_stems, lfs_duplicates, lfs_normalized = _stem_names(lfs_images)
    image_suffixes = _suffixes_by_stem(images)
    lfs_suffixes = _suffixes_by_stem(lfs_images)
    extension_changes = sum(image_suffixes[name] != lfs_suffixes[name] for name in image_stems & lfs_stems)
    return {
        "expected_image_count": len(image_stems),
        "matched_image_count": len(image_stems & lfs_stems),
        "unmatched_image_count": len(image_stems - lfs_stems) + len(lfs_stems - image_stems),
        "duplicate_mapping_count": image_duplicates + lfs_duplicates,
        "mask_match_count": 0,
        "normalization_count": image_normalized + lfs_normalized + extension_changes,
    }


def _build_lfs_manifest(lfs_source: Path, images: Path, masks: Path, root: Path, reconstruction_id: str) -> dict[str, Any]:
    """Return the manifest of the actual generated dataset, with package-local refs."""
    manifest = _validate_lfs_dataset(lfs_source)
    manifest = dict(manifest)
    manifest["source_uri"] = _relative_uri(lfs_source, root, "lfs_input")
    manifest["source_sha256"] = _source_hash(lfs_source)
    manifest["source_reconstruction_id"] = reconstruction_id
    manifest["required_contents"] = {"images_uri": _relative_uri(lfs_source / "images", root, "lfs_images"), "sparse_uri": _relative_uri(lfs_source / "sparse", root, "lfs_sparse")}
    source_names = _file_names(images)
    lfs_names = _file_names(lfs_source / "images")
    mapping = _image_mapping(images, lfs_source / "images")
    _pairs = _image_mask_pairs(images, masks, source_names)
    _, mask_duplicates, mask_normalized = _stem_names(masks)
    mapping["mask_match_count"] = len(_pairs)
    mapping["duplicate_mapping_count"] += mask_duplicates
    mapping["normalization_count"] += mask_normalized
    mapping["registered_image_count"] = len(source_names)
    mapping["unmatched_image_count"] = len(set(source_names) ^ set(lfs_names))
    if mapping["unmatched_image_count"] or mapping["duplicate_mapping_count"]:
        raise RuntimeError(f"lfs_input_image_mapping_invalid:{mapping}")
    manifest["image_name_mapping"] = mapping
    return manifest


def _validate_lfs_inputs(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Validate artifacts common to both LFS export and metric packaging."""
    workspace = root / config["workspace"]
    input_manifest = read_json(workspace / "input_manifest.json", "input_quality")
    _require_status(input_manifest, "input")
    mask_settings = config.get("person_mask")
    if not isinstance(mask_settings, dict):
        raise RuntimeError("person_mask_config_missing")
    mask_quality = read_json(root / mask_settings["report"], "mask_quality")
    _require_status(mask_quality, "mask")
    mask_config_path = root / mask_settings.get("config", "")
    if mask_config_path.is_file():
        mask_config = read_json(mask_config_path, "person_mask_config")
        expected_backend = str(mask_config.get("backend", ""))
        if expected_backend and mask_quality.get("backend") != expected_backend:
            raise RuntimeError(f"person_mask_backend_mismatch:{mask_quality.get('backend')}:{expected_backend}")
    images = root / config["sfm"]["images"]
    masks = root / config["sfm"]["masks"]
    rig = root / config["sfm"]["rig_config"]
    model = root / config["sfm"]["model"]
    sparse_model_config = config["sfm"].get("sparse_model")
    sparse_model_explicit = isinstance(sparse_model_config, str) and bool(sparse_model_config)
    sparse_model = (
        root / str(sparse_model_config)
        if sparse_model_explicit
        else workspace / "colmap_sequential" / "sparse" / "0"
    )
    sfm_manifest = read_json(root / config["sfm"]["manifest"], "sfm_quality")
    expected_mask_source = Path(str(config["sfm"].get("masks", ""))).as_posix()
    recorded_mask_source = Path(str(sfm_manifest.get("input_masks", ""))).as_posix()
    if not recorded_mask_source:
        raise RuntimeError("sfm_mask_source_missing")
    if recorded_mask_source != expected_mask_source:
        raise RuntimeError(f"sfm_mask_source_mismatch:{recorded_mask_source}:{expected_mask_source}")
    trajectory = root / config["sfm"].get(
        "trajectory", str(workspace / "colmap_sequential" / "trajectory_diagnostic.aligned.json")
    )
    # lfs_ready is generated from the verified App A artifacts below; an old
    # directory is never treated as evidence merely because it exists.
    lfs_source = workspace / "lfs_ready"
    for path, label in (
        (images, "images"), (masks, "masks"), (rig, "rig_config"),
        (model, "sfm_model"), (sparse_model, "sfm_sparse_model"),
        (trajectory, "camera_trajectory"),
    ):
        if not path.exists():
            raise RuntimeError(f"{label}_missing:{path}")
    image_names = _file_names(images)
    mask_names = _file_names(masks)
    if len(image_names) == 0 or len(image_names) != len(mask_names):
        raise RuntimeError("image_mask_count_mismatch")
    if set(image_names) != set(mask_names):
        image_stems, _, _ = _stem_names(images)
        mask_stems, _, _ = _stem_names(masks)
        if image_stems != mask_stems:
            raise RuntimeError("image_mask_name_mismatch")
    metrics = sfm_manifest.get("colmap", {})
    registered = int(metrics.get("registered_images", 0) or 0)
    points = int(metrics.get("points", 0) or 0)
    observations = int(metrics.get("observations", 0) or 0)
    reprojection = _finite_number(metrics.get("mean_reprojection_error_px"), "sfm_reprojection_error")
    if registered < len(image_names) or points <= 0 or observations <= 0:
        raise RuntimeError("sfm_quality_not_verified")
    sfm_quality = {
        "schema_version": SCHEMA_VERSION,
        "stage": "sfm_quality",
        "status": "verified",
        "metrics": {
            "input_image_count": len(image_names),
            "registered_image_count": registered,
            "registration_ratio": registered / len(image_names),
            "sparse_point_count": points,
            "observation_count": observations,
            "mean_reprojection_error_px": reprojection,
            "colmap_version": metrics.get("version", "unknown"),
        },
        "source_manifest_uri": _relative_uri(root / config["sfm"]["manifest"], root, "sfm_manifest"),
        "failure_reasons": [],
        "warnings": [],
    }
    return {
        "workspace": workspace,
        "input_manifest": input_manifest,
        "mask_quality": mask_quality,
        "images": images,
        "masks": masks,
        "rig": rig,
        "model": model,
        "sparse_model": sparse_model,
        "sparse_model_explicit": sparse_model_explicit,
        "sparse_model_uri": _relative_uri(sparse_model, root, "sfm_sparse_model"),
        "sfm_manifest": sfm_manifest,
        "sfm_quality": sfm_quality,
        "trajectory": trajectory,
        "lfs_source": lfs_source,
        "metric_scale_available": bool(config.get("metric_marker", {}).get("enabled", True)),
    }


def _validate_inputs(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Validate the additional scale evidence required by a Metric Package."""
    inputs = _validate_lfs_inputs(root, config)
    if not inputs["metric_scale_available"]:
        raise RuntimeError("metric_reconstruction_requires_apriltag")
    alignment_path = root / config["metric_marker"]["world_alignment"]
    corners_path = root / config["metric_marker"]["corners_world"]
    quality_reference = config["metric_marker"].get("quality")
    if not isinstance(quality_reference, str) or not quality_reference:
        raise RuntimeError("apriltag_triangulation_quality_missing")
    triangulation_quality_path = root / quality_reference
    alignment = read_json(alignment_path, "metric_quality")
    corners = read_json(corners_path, "apriltag_corners")
    triangulation_quality = read_json(triangulation_quality_path, "apriltag_triangulation_quality")
    quality_gate = triangulation_quality.get("quality_gate")
    if (
        triangulation_quality.get("status") != "verified"
        or not isinstance(quality_gate, dict)
        or quality_gate.get("status") != "verified"
    ):
        raise RuntimeError("apriltag_triangulation_quality_not_verified")
    if alignment.get("status") != "verified" or alignment.get("unit") != "meter":
        raise RuntimeError("world_alignment_not_verified")
    if len(corners.get("corners_world_m", [])) != 4:
        raise RuntimeError("apriltag_world_corners_missing")
    transform = alignment.get("generated_to_world")
    if not isinstance(transform, dict):
        raise RuntimeError("world_transform_missing")
    _finite_number(transform.get("scale"), "world_scale")
    if float(transform["scale"]) <= 0:
        raise RuntimeError("world_scale_invalid")
    if not isinstance(transform.get("rotation_quaternion_xyzw"), list) or len(transform["rotation_quaternion_xyzw"]) != 4:
        raise RuntimeError("world_rotation_invalid")
    if not isinstance(transform.get("translation_m"), list) or len(transform["translation_m"]) != 3:
        raise RuntimeError("world_translation_invalid")
    inputs["alignment_path"] = alignment_path
    inputs["triangulation_quality_path"] = triangulation_quality_path
    inputs["metric_quality"] = {
        "schema_version": SCHEMA_VERSION,
        "stage": "metric_quality",
        "status": "verified",
        "world_alignment_uri": _relative_uri(alignment_path, root, "world_alignment"),
        "corners_world_uri": _relative_uri(corners_path, root, "corners_world"),
        "generated_to_world": transform,
        "quality": triangulation_quality,
        "quality_gate": quality_gate,
        "failure_reasons": [],
        "warnings": [],
    }
    return inputs


def export_lfs_input(root: Path, scene_config: Path) -> dict[str, Any]:
    """Export only the horizontalized, alpha-masked LichtFeld input.

    This intentionally does not claim metric scale.  It is used when an
    AprilTag is disabled, and is also the first half of a metric export.
    """
    root = root.resolve()
    config = load_export_config(scene_config.resolve())
    inputs = _validate_lfs_inputs(root, config)
    reconstruction_id = config.get("reconstruction_id", f"{config['scene_id']}-fixed-rig")
    return _export_or_reuse_lfs_dataset(inputs, reconstruction_id)


def _export_or_reuse_lfs_dataset(inputs: dict[str, Any], reconstruction_id: str) -> dict[str, Any]:
    """Reuse only an LFS directory proven to use the current binary model."""
    output = inputs["workspace"] / "lfs_ready"
    try:
        registered_names = _colmap_image_names(inputs["model"])
        existing = _validate_lfs_dataset(
            output, expected_count=len(registered_names), expected_names=registered_names
        )
        existing_sparse = existing.get("sparse", {})
        existing_images = existing.get("images", {})
        existing_mask = existing.get("mask", {})
        coordinate = existing.get("coordinate", {})
        if (
            isinstance(existing_sparse, dict)
            and existing_sparse.get("source_uri") == inputs["sparse_model_uri"]
                and existing_sparse.get("source_sha256") == _source_hash(inputs["sparse_model"])
                and isinstance(existing_images, dict)
                and existing_images.get("source_sha256") == _source_hash(inputs["images"])
                and isinstance(existing_mask, dict)
                and existing_mask.get("source_sha256") == _source_hash(inputs["masks"])
            and isinstance(coordinate, dict)
            and coordinate.get("metric_scale_available") is inputs["metric_scale_available"]
        ):
            return existing | {"path": str(output), "reused": True}
    except RuntimeError:
        pass
    return export_lfs_dataset(inputs, reconstruction_id) | {"reused": False}


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def export_package(root: Path, scene_config: Path, output: Path | None = None) -> dict[str, Any]:
    root = root.resolve()
    scene_config = scene_config.resolve()
    config = load_export_config(scene_config)
    scene_id = config["scene_id"]
    reconstruction_id = config.get("reconstruction_id", f"{scene_id}-fixed-rig")
    output = (
        output
        or SceneRepositoryLayout.from_root(root).metric_reconstructions / scene_id / reconstruction_id
    ).resolve()
    inputs = _validate_inputs(root, config)
    # Generate the canonical LFS dataset before considering a package reusable.
    # This invalidates legacy reference-only packages whose stem comparison was
    # never backed by actual RGBA files.
    lfs_manifest = _export_or_reuse_lfs_dataset(inputs, reconstruction_id)
    inputs["lfs_source"] = Path(lfs_manifest["path"])
    existing_invalid = False
    if output.exists():
        try:
            previous = validate_metric_reconstruction_package(output, scene_id, reconstruction_id, source_root=root)
        except (OSError, ValueError, json.JSONDecodeError):
            previous = {"status": "failed"}
        if previous.get("status") == "verified":
            previous_lfs = read_json(output / "lfs_input/manifest.json", "previous_lfs_manifest")
            current_lfs = _build_lfs_manifest(inputs["lfs_source"], inputs["images"], inputs["masks"], root, reconstruction_id)
            current_mapping = current_lfs.get("image_name_mapping")
            current_sparse = current_lfs.get("sparse", {})
            previous_sparse = previous_lfs.get("sparse", {}) if isinstance(previous_lfs, dict) else {}
            if (
                isinstance(previous_lfs, dict)
                and previous_lfs.get("image_name_mapping") == current_mapping
                and isinstance(current_sparse, dict)
                and isinstance(previous_sparse, dict)
                and previous_sparse.get("source_sha256") == current_sparse.get("source_sha256")
            ):
                return previous | {"reused": True}
        existing_invalid = True

    staging_parent = output.parent
    staging_parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if existing_invalid:
        backup = staging_parent / f".{output.name}.invalid.{os.getpid()}"
        os.replace(output, backup)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging.", dir=staging_parent))
    package = staging / output.name
    try:
        for relative in ("images", "masks", "colmap/sparse", "colmap/text", "quality", "lfs_input", "config"):
            (package / relative).mkdir(parents=True, exist_ok=True)
        _write_image_manifest(package, "images", inputs["images"], root, "images")
        _write_image_manifest(package, "masks", inputs["masks"], root, "masks")
        _write_source_reference(package, "colmap/sparse", inputs["sparse_model"], root, "colmap_sparse")
        _write_source_reference(package, "colmap/text", inputs["model"], root, "colmap_text")
        shutil.copy2(inputs["rig"], package / "rig_config.json")
        shutil.copy2(inputs["alignment_path"], package / "config/world_alignment.json")
        shutil.copy2(inputs["trajectory"], package / "camera_trajectory.json")
        _write_quality_reports(package, inputs["input_manifest"], inputs["mask_quality"], inputs["sfm_quality"], inputs["metric_quality"])
        lfs_manifest = _build_lfs_manifest(inputs["lfs_source"], inputs["images"], inputs["masks"], root, reconstruction_id)
        atomic_json(package / "lfs_input/manifest.json", lfs_manifest)
        _write_source_reference(package, "lfs_input", inputs["lfs_source"], root, "lfs_input")

        image_names = _file_names(inputs["images"])
        mask_names = _file_names(inputs["masks"])
        input_video_hash = inputs["input_manifest"].get("sha256")
        if not isinstance(input_video_hash, str) or len(input_video_hash) != 64:
            raise RuntimeError("input_video_hash_missing")
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "package_kind": "metric_reconstruction",
            "scene_id": scene_id,
            "reconstruction_id": reconstruction_id,
            "source": {
                "input_video_hash": input_video_hash,
                "config_hash": sha256_file(scene_config),
                "image_set_id": sha256_text("\n".join(image_names)),
                "mask_set_id": sha256_text("\n".join(mask_names)),
            },
            "artifacts": {
                "images": {"uri": "images", "manifest_uri": "images/manifest.json", "sha256": sha256_file(package / "images/manifest.json")},
                "masks": {"uri": "masks", "manifest_uri": "masks/manifest.json", "sha256": sha256_file(package / "masks/manifest.json")},
                "rig_config_uri": "rig_config.json",
                "rig_config_sha256": sha256_file(package / "rig_config.json"),
                "sfm": {
                    "sparse_uri": "colmap/sparse",
                    "text_uri": "colmap/text",
                    "model_sha256": _model_hash(inputs["model"]),
                    "sparse_model_sha256": _source_hash(inputs["sparse_model"]),
                },
                "world_alignment": {"uri": "config/world_alignment.json", "sha256": sha256_file(package / "config/world_alignment.json")},
                "camera_trajectory": {"uri": "camera_trajectory.json", "sha256": sha256_file(package / "camera_trajectory.json")},
                "quality": {
                    "input_uri": "quality/input_quality.json", "input_sha256": sha256_file(package / "quality/input_quality.json"),
                    "mask_uri": "quality/mask_quality.json", "mask_sha256": sha256_file(package / "quality/mask_quality.json"),
                    "sfm_uri": "quality/sfm_quality.json", "sfm_sha256": sha256_file(package / "quality/sfm_quality.json"),
                    "metric_uri": "quality/metric_quality.json", "metric_sha256": sha256_file(package / "quality/metric_quality.json"),
                },
                "lfs_input": {"uri": "lfs_input", "manifest_uri": "lfs_input/manifest.json", "sha256": sha256_file(package / "lfs_input/manifest.json")},
            },
            "coordinate": {"stage": "horizontalized_colmap", "unit": "meter", "up_axis": "Y"},
            "created_at": datetime.now(timezone.utc).isoformat(),
            "tool_versions": {
                "exporter": "metric_reconstruction_package_exporter/1.0.0",
                "colmap": inputs["sfm_manifest"].get("colmap", {}).get("version", "unknown"),
                "person_mask": inputs["mask_quality"].get("model", {}).get("runtime_version", "unknown"),
            },
            "provenance": {
                "source_workspace_uri": _relative_uri(inputs["workspace"], root, "workspace"),
                "input_quality_uri": "quality/input_quality.json",
                "mask_quality_uri": "quality/mask_quality.json",
                "sfm_quality_uri": "quality/sfm_quality.json",
                "metric_quality_uri": "quality/metric_quality.json",
                "external_lfs_stage": True,
            },
        }
        atomic_json(package / "manifest.json", manifest)
        validation = validate_metric_reconstruction_package(package, scene_id, reconstruction_id, source_root=root)
        if validation["status"] != "verified":
            raise RuntimeError("metric_reconstruction_validation_failed:" + ",".join(validation["failure_reasons"]))
        if output.exists():
            _remove_path(output)
        os.replace(package, output)
        validation["package"] = str(output)
        return validation | {"reused": False}
    except Exception:
        # Do not leave a partially written canonical package behind.
        _remove_path(package)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if backup is not None:
            _remove_path(backup)


def main() -> int:
    parser = argparse.ArgumentParser(description="Export a Metric Reconstruction Package from verified App A artifacts.")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--scene-config", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = export_package(args.repo_root, args.scene_config, args.output)
    except (OSError, ValueError, KeyError, json.JSONDecodeError, RuntimeError) as error:
        result = {
            "schema_version": SCHEMA_VERSION,
            "stage": "metric_reconstruction_package_validation",
            "status": "failed",
            "failure_reasons": [str(error)],
            "warnings": [],
        }
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "verified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
