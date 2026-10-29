from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from packages.scene_repository import SceneRepositoryLayout
if __package__:
    from .quaternion_contract import require_unit_quaternion_xyzw
else:
    from quaternion_contract import require_unit_quaternion_xyzw


SCHEMA_VERSION = "1.0.0"
PACKAGE_KIND = "metric_reconstruction"
SOURCE_VERIFICATION_MODES = frozenset({"none", "presence", "hash"})
REQUIRED_ROOT = {
    "schema_version",
    "package_kind",
    "scene_id",
    "reconstruction_id",
    "source",
    "artifacts",
    "coordinate",
    "created_at",
    "tool_versions",
    "provenance",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(item for item in path.rglob("*") if item.is_file())


def _directory_hash(path: Path) -> str:
    digest = hashlib.sha256()
    for item in _files(path):
        digest.update(item.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_file(item).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _lfs_input_directory_hash(path: Path) -> str:
    """Hash only immutable LFS dataset inputs.

    LichtFeld Studio writes checkpoints, logs, and model exports into the
    selected dataset's top-level ``output/`` directory.  Those external
    training results are not App A inputs and must not invalidate the exact
    RGBA images and COLMAP model recorded by an MRP.
    """
    digest = hashlib.sha256()
    for item in _files(path):
        relative = item.relative_to(path)
        if relative.parts and relative.parts[0] == "output":
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_file(item).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _is_absolute_uri(value: str) -> bool:
    return value.startswith("/") or (len(value) >= 2 and value[1] == ":")


def require_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"invalid {label} sha256")
    return value


def relative_path(package: Path, uri: object, label: str, *, directory: bool = False) -> Path:
    if not isinstance(uri, str) or not uri or _is_absolute_uri(uri) or ".." in Path(uri).parts:
        raise ValueError(f"invalid {label} URI")
    candidate = (package / uri).resolve()
    if candidate != package.resolve() and package.resolve() not in candidate.parents:
        raise ValueError(f"{label} escapes package")
    exists = candidate.is_dir() if directory else candidate.is_file()
    if not exists:
        kind = "directory" if directory else "file"
        raise ValueError(f"missing {label} {kind}: {uri}")
    return candidate


def source_uri(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or _is_absolute_uri(value) or ".." in Path(value).parts:
        raise ValueError(f"invalid {label} source URI")
    return value


def source_path(source_root: Path, value: object, label: str) -> Path:
    """Resolve current and historical repository URIs without mutating MRP."""

    uri = source_uri(value, label)
    root = source_root.resolve()
    candidate = SceneRepositoryLayout.from_root(root).resolve_uri(uri)
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"{label} source escapes root")
    return candidate


def _validate_lfs_source(source: Path, manifest: dict[str, Any]) -> None:
    if manifest.get("stage") != "lfs_input" or manifest.get("status") != "verified":
        raise ValueError("lfs_input_not_verified")
    images = source / "images"
    sparse = source / "sparse" / "0"
    if not images.is_dir() or not sparse.is_dir():
        raise ValueError("lfs_input_contents_missing")
    names = [item.relative_to(images).as_posix() for item in sorted(images.rglob("*")) if item.is_file()]
    image_manifest = manifest.get("images") if isinstance(manifest.get("images"), dict) else {}
    expected = image_manifest.get("files")
    if not isinstance(expected, list) or sorted(expected) != sorted(names) or image_manifest.get("file_count") != len(names):
        raise ValueError("lfs_image_manifest_mismatch")
    if any(Path(name).suffix.lower() != ".png" for name in names):
        raise ValueError("lfs_alpha_images_must_be_png")
    for name in names:
        try:
            with Image.open(images / name) as image:
                if image.mode != "RGBA":
                    raise ValueError(f"lfs_image_not_rgba:{name}")
                image.load()
        except (OSError, UnidentifiedImageError) as error:
            raise ValueError(f"lfs_image_decode_failed:{name}") from error
    for required in ("cameras.bin", "images.bin", "points3D.bin"):
        if not (sparse / required).is_file():
            raise ValueError(f"lfs_sparse_missing:{required}")
    mask = manifest.get("mask") if isinstance(manifest.get("mask"), dict) else {}
    stats = mask.get("alpha_stats") if isinstance(mask.get("alpha_stats"), dict) else {}
    if stats.get("alpha_present_count") != len(names) or stats.get("output_image_decode_success_count") != len(names):
        raise ValueError("lfs_alpha_stats_mismatch")
    if mask.get("route") != "alpha" or mask.get("semantics") not in {
        "alpha=255 retained, alpha=0 predicted person",
        # Backward compatibility for already published immutable packages.
        "alpha=255 retained/valid, alpha=0 excluded/person",
    }:
        raise ValueError("lfs_alpha_semantics_missing")


def _validate_lfs_source_presence(source: Path, manifest: dict[str, Any]) -> None:
    """Check usability without decoding or hashing the large LFS dataset."""

    if manifest.get("stage") != "lfs_input" or manifest.get("status") != "verified":
        raise ValueError("lfs_input_not_verified")
    images = source / "images"
    sparse = source / "sparse" / "0"
    if not images.is_dir() or not sparse.is_dir():
        raise ValueError("lfs_input_contents_missing")
    for required in ("cameras.bin", "images.bin", "points3D.bin"):
        if not (sparse / required).is_file():
            raise ValueError(f"lfs_sparse_missing:{required}")
    expected_count = manifest.get("images", {}).get("file_count")
    if not isinstance(expected_count, int) or expected_count <= 0:
        raise ValueError("lfs_image_manifest_mismatch")
    actual_count = sum(1 for item in images.rglob("*") if item.is_file())
    if actual_count != expected_count:
        raise ValueError("lfs_image_manifest_mismatch")


def _source_reference(
    package: Path,
    relative: str,
    label: str,
    source_root: Path | None,
    source_verification: str,
    files: dict[str, dict[str, Any]],
) -> None:
    reference_path = relative_path(package, f"{relative}/.source.json", f"{label}_source")
    reference = read_json(reference_path)
    uri = source_uri(reference.get("source_uri"), label)
    expected = require_hash(reference.get("source_sha256"), f"{label}_source")
    if source_root is not None and source_verification != "none":
        source = source_path(source_root, uri, label)
        if not source.exists():
            raise ValueError(f"missing {label} source: {uri}")
        if source_verification == "hash":
            if source.is_file():
                actual = sha256_file(source)
            elif label == "lfs_input":
                actual = _lfs_input_directory_hash(source)
            else:
                actual = _directory_hash(source)
            if actual != expected:
                raise ValueError(f"{label}_source_hash_mismatch")
    files[str(reference_path.relative_to(package))] = {"size": reference_path.stat().st_size, "sha256": sha256_file(reference_path)}


def _validate_set_manifest(
    package: Path,
    relative: str,
    label: str,
    source_root: Path | None,
    source_verification: str,
    files: dict[str, dict[str, Any]],
) -> None:
    manifest_path = relative_path(package, f"{relative}/manifest.json", f"{label}_manifest")
    manifest = read_json(manifest_path)
    source_uri(manifest.get("source_uri"), label)
    names = manifest.get("files")
    if not isinstance(names, list) or not names or any(not isinstance(name, str) or Path(name).is_absolute() or ".." in Path(name).parts for name in names):
        raise ValueError(f"invalid {label} file manifest")
    if len(names) != len(set(names)):
        raise ValueError(f"duplicate {label} mapping")
    if manifest.get("file_count") != len(names):
        raise ValueError(f"{label}_file_count_mismatch")
    expected_source_hash = require_hash(manifest.get("source_sha256"), f"{label}_manifest_source")
    if source_root is not None and source_verification != "none":
        source = source_path(source_root, manifest["source_uri"], label)
        if not source.is_dir():
            raise ValueError(f"missing {label} source: {manifest['source_uri']}")
        actual_names = [item.relative_to(source).as_posix() for item in _files(source)]
        if sorted(names) != sorted(actual_names):
            raise ValueError(f"{label}_file_manifest_mismatch")
        if (
            source_verification == "hash"
            and _directory_hash(source) != expected_source_hash
        ):
            raise ValueError(f"{label}_manifest_source_hash_mismatch")
    files[str(manifest_path.relative_to(package))] = {"size": manifest_path.stat().st_size, "sha256": sha256_file(manifest_path)}
    _source_reference(
        package, relative, label, source_root, source_verification, files
    )


def _quality_report(
    package: Path,
    quality: dict[str, Any],
    name: str,
    source_root: Path | None,
    files: dict[str, dict[str, Any]],
) -> None:
    path = relative_path(package, quality.get(f"{name}_uri"), f"{name}_quality")
    report = read_json(path)
    if report.get("status") not in {"verified", "reused"}:
        raise ValueError(f"{name}_quality_not_verified")
    if name == "metric":
        triangulation = report.get("quality")
        quality_gate = report.get("quality_gate")
        if isinstance(quality_gate, dict):
            if quality_gate.get("status") != "verified":
                raise ValueError("metric_triangulation_quality_gate_not_verified")
            if not isinstance(triangulation, dict) or triangulation.get("status") != "verified":
                raise ValueError("metric_triangulation_quality_not_verified")
        else:
            # Metric packages made before the explicit gate remain readable
            # when they contain the original diagnostic evidence.  They are
            # not eligible for dynamic fixed-tag tracking on re-import; that
            # decision is made by the Scene Package importer.
            required_legacy_metrics = {
                "side_cv",
                "diagonal_ratio",
                "normalized_ray_residual",
            }
            if not isinstance(triangulation, dict) or not required_legacy_metrics.issubset(triangulation):
                raise ValueError("metric_quality_gate_missing_or_evidence_incomplete")
    expected_hash = require_hash(quality.get(f"{name}_sha256"), f"{name}_quality")
    if sha256_file(path) != expected_hash:
        raise ValueError(f"{name}_quality_hash_mismatch")
    files[str(path.relative_to(package))] = {"size": path.stat().st_size, "sha256": expected_hash}


def validate_metric_reconstruction_package(
    package: Path,
    expected_scene_id: str | None = None,
    expected_reconstruction_id: str | None = None,
    source_root: Path | None = None,
    source_verification: str | None = None,
) -> dict[str, Any]:
    package = package.resolve()
    source_root = source_root.resolve() if source_root is not None else None
    if source_verification is None:
        # Preserve the previous API for explicit offline audits. Operational
        # callers select ``presence`` so ordinary validation never re-hashes
        # a complete reconstruction workspace.
        source_verification = "hash" if source_root is not None else "none"
    failures: list[str] = []
    warnings: list[str] = []
    files: dict[str, dict[str, Any]] = {}
    try:
        if source_verification not in SOURCE_VERIFICATION_MODES:
            raise ValueError("source_verification_invalid")
        if source_verification != "none" and source_root is None:
            raise ValueError("source_root_required_for_source_verification")
        manifest = read_json(package / "manifest.json")
        missing = sorted(REQUIRED_ROOT - set(manifest))
        failures.extend(f"manifest_missing_{key}" for key in missing)
        if manifest.get("schema_version") != SCHEMA_VERSION:
            failures.append("unsupported_metric_reconstruction_schema")
        if manifest.get("package_kind") != PACKAGE_KIND:
            failures.append("invalid_package_kind")
        if expected_scene_id is not None and manifest.get("scene_id") != expected_scene_id:
            failures.append("scene_id_mismatch")
        if expected_reconstruction_id is not None and manifest.get("reconstruction_id") != expected_reconstruction_id:
            failures.append("reconstruction_id_mismatch")
        source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
        for key in ("input_video_hash", "config_hash"):
            require_hash(source.get(key), key)
        for key in ("image_set_id", "mask_set_id"):
            if not isinstance(source.get(key), str) or not source[key]:
                raise ValueError(f"missing_source_{key}")
        coordinate = manifest.get("coordinate") if isinstance(manifest.get("coordinate"), dict) else {}
        if coordinate.get("stage") not in {"horizontalized_colmap", "world_meter"}:
            raise ValueError("invalid_coordinate_stage")
        if coordinate.get("unit") != "meter" or coordinate.get("up_axis") != "Y":
            raise ValueError("metric_package_coordinate_mismatch")
        provenance = manifest.get("provenance") if isinstance(manifest.get("provenance"), dict) else {}
        source_uri(provenance.get("source_workspace_uri"), "workspace")
        artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), dict) else {}
        for name in ("images", "masks"):
            entry = artifacts[name]
            directory = relative_path(package, entry.get("uri"), name, directory=True)
            expected_hash = require_hash(entry.get("sha256"), name)
            manifest_path = relative_path(package, entry.get("manifest_uri"), f"{name}_manifest")
            if sha256_file(manifest_path) != expected_hash:
                raise ValueError(f"{name}_manifest_hash_mismatch")
            if not any(directory.iterdir()):
                raise ValueError(f"{name}_directory_empty")
            _validate_set_manifest(
                package,
                str(directory.relative_to(package)),
                name,
                source_root,
                source_verification,
                files,
            )
        rig_config = relative_path(package, artifacts["rig_config_uri"], "rig_config")
        rig_hash = require_hash(artifacts.get("rig_config_sha256"), "rig_config")
        if sha256_file(rig_config) != rig_hash:
            raise ValueError("rig_config_hash_mismatch")
        files[str(rig_config.relative_to(package))] = {"size": rig_config.stat().st_size, "sha256": rig_hash}
        sfm = artifacts["sfm"]
        relative_path(package, sfm.get("sparse_uri"), "sfm_sparse", directory=True)
        relative_path(package, sfm.get("text_uri"), "sfm_text", directory=True)
        model_hash = require_hash(sfm.get("model_sha256"), "sfm_model")
        _source_reference(
            package,
            str(Path(sfm["text_uri"])),
            "sfm_text",
            source_root,
            source_verification,
            files,
        )
        _source_reference(
            package,
            str(Path(sfm["sparse_uri"])),
            "sfm_sparse",
            source_root,
            source_verification,
            files,
        )
        if source_root is not None and source_verification == "hash":
            text_source = read_json(package / sfm["text_uri"] / ".source.json")
            text_source_path = source_path(
                source_root, text_source["source_uri"], "sfm_text"
            )
            if _directory_hash(text_source_path) != model_hash:
                raise ValueError("sfm_model_hash_mismatch")
        for name in ("world_alignment", "camera_trajectory"):
            entry = artifacts[name]
            path = relative_path(package, entry.get("uri"), name)
            expected_hash = require_hash(entry.get("sha256"), name)
            if sha256_file(path) != expected_hash:
                raise ValueError(f"{name}_hash_mismatch")
            if name == "world_alignment":
                alignment = read_json(path)
                generated_to_world = alignment.get("generated_to_world")
                if not isinstance(generated_to_world, dict):
                    raise ValueError("world_alignment.generated_to_world:missing")
                # Persisted MRP inputs must satisfy the same strict xyzw unit
                # quaternion contract as Scene Packages. Do not rely on a
                # downstream math library to normalize this external value.
                require_unit_quaternion_xyzw(
                    generated_to_world.get("rotation_quaternion_xyzw"),
                    "world_alignment.generated_to_world.rotation_quaternion_xyzw",
                )
            files[str(path.relative_to(package))] = {"size": path.stat().st_size, "sha256": expected_hash}
        quality = artifacts["quality"]
        for name in ("input", "mask", "sfm", "metric"):
            _quality_report(package, quality, name, source_root, files)
        lfs = artifacts["lfs_input"]
        relative_path(package, lfs.get("uri"), "lfs_input", directory=True)
        lfs_manifest = relative_path(package, lfs.get("manifest_uri"), "lfs_input_manifest")
        lfs_hash = require_hash(lfs.get("sha256"), "lfs_input")
        if sha256_file(lfs_manifest) != lfs_hash:
            raise ValueError("lfs_input_manifest_hash_mismatch")
        lfs_report = read_json(lfs_manifest)
        _source_reference(
            package,
            "lfs_input",
            "lfs_input",
            source_root,
            source_verification,
            files,
        )
        if source_root is not None and source_verification != "none":
            lfs_source_ref = read_json(package / "lfs_input/.source.json")
            lfs_source = source_path(
                source_root, lfs_source_ref.get("source_uri"), "lfs_input"
            )
            if source_verification == "hash":
                _validate_lfs_source(lfs_source, lfs_report)
            else:
                _validate_lfs_source_presence(lfs_source, lfs_report)
        files[str(lfs_manifest.relative_to(package))] = {"size": lfs_manifest.stat().st_size, "sha256": lfs_hash}
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as error:
        failures.append(str(error))
    return {
        "schema_version": SCHEMA_VERSION,
        "stage": "metric_reconstruction_package_validation",
        "status": "verified" if not failures else "failed",
        "scene_id": locals().get("manifest", {}).get("scene_id"),
        "reconstruction_id": locals().get("manifest", {}).get("reconstruction_id"),
        "package": str(package),
        "manifest": "manifest.json",
        "asset_count": len(files),
        "assets": files,
        "failure_reasons": failures,
        "warnings": warnings,
        "verification_scope": {
            "package_contract": True,
            "source_verification": source_verification,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a Metric Reconstruction Package.")
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--scene-id")
    parser.add_argument("--reconstruction-id")
    parser.add_argument("--source-root", type=Path)
    parser.add_argument(
        "--source-verification",
        choices=sorted(SOURCE_VERIFICATION_MODES),
        help="none=MRP内のみ、presence=参照先の存在確認、hash=参照先を全量再hash",
    )
    args = parser.parse_args()
    try:
        result = validate_metric_reconstruction_package(
            args.package,
            args.scene_id,
            args.reconstruction_id,
            args.source_root,
            args.source_verification,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        result = {"schema_version": SCHEMA_VERSION, "stage": "metric_reconstruction_package_validation", "status": "failed", "failure_reasons": [str(error)], "warnings": []}
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "verified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
