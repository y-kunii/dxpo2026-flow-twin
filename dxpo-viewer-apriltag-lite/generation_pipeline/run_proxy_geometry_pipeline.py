from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))

from pipeline_runtime import StageRecord, artifact_record, atomic_json, run_command, sha256_file, utc_now
from export_metric_reconstruction_package import export_lfs_input, export_package
from scene_config import SceneConfigError, load_scene_config
from docker_runtime import PINNED_COLMAP_IMAGE, docker_workspace_arguments
from apriltag_family_contract import require_supported_apriltag_family
from packages.scene_repository import SceneRepositoryLayout


class StageUnverified(RuntimeError):
    """A completed check that must fail closed without claiming a tool crash."""

    def __init__(self, reasons: list[str]):
        super().__init__(";".join(reasons))
        self.reasons = reasons


INPUT_PROJECTIONS = {"equirectangular_360", "perspective"}


def input_projection(config: dict[str, Any]) -> str:
    """Return the explicit video projection, preserving legacy 360 configs."""
    value = str(config.get("input_projection", "equirectangular_360"))
    if value not in INPUT_PROJECTIONS:
        raise RuntimeError(f"input_projection_invalid:{value}")
    return value


def reconstruction_stop_after(config: dict[str, Any]) -> str:
    """Return the formal application boundary for a reconstruction config."""
    marker = config.get("metric_marker", {})
    marker_enabled = bool(marker.get("enabled", True)) if isinstance(marker, dict) else True
    return "metric_reconstruction_package" if marker_enabled else "lfs_export"


def validate_video_projection(config: dict[str, Any], width: int, height: int) -> str:
    """Validate projection-specific dimensions without guessing the input type."""
    projection = input_projection(config)
    if width <= 0 or height <= 0:
        raise RuntimeError(f"input_video_dimensions_invalid:{width}x{height}")
    if projection == "equirectangular_360" and width != height * 2:
        raise RuntimeError(f"input_not_equirectangular_2_to_1:{width}x{height}")
    return projection


def write_full_validity_masks(image_dir: Path, mask_dir: Path) -> int:
    """Create white static masks for perspective frames using COLMAP mask keys."""
    images = sorted(
        path for path in image_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in {".bmp", ".jpeg", ".jpg", ".png", ".webp"}
    )
    if not images:
        raise RuntimeError("perspective_frame_extraction_produced_no_images")
    for image_path in images:
        relative = image_path.relative_to(image_dir)
        output = mask_dir / (relative.as_posix() + ".png")
        output.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(image_path) as source:
            Image.new("L", source.size, 255).save(output)
    return len(images)


def write_perspective_camera_contract(rig_path: Path, manifest_path: Path, frame_count: int) -> None:
    """Persist the single-camera provenance required by LFS/MRP exporters."""
    atomic_json(rig_path, [{
        "cameras": [{
            "image_prefix": "",
            "ref_sensor": True,
            "camera_model_name": "SIMPLE_RADIAL",
        }],
    }])
    atomic_json(manifest_path, {
        "schema_version": "1.0.0",
        "input_projection": "perspective",
        "frame_count": frame_count,
        "virtual_cameras_per_panorama": 1,
        "panorama_count": frame_count,
        "camera_model": "SIMPLE_RADIAL",
        "camera_params": "estimated_by_colmap",
    })


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def legacy_atomic_json(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, indent=2) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, path)
    except PermissionError:
        temporary = Path(tempfile.mkstemp(prefix="proxy_quality.", suffix=".json")[1])
        temporary.write_text(payload, encoding="utf-8")
        subprocess.run(["sudo", "-n", "cp", str(temporary), str(path)], check=True)
        subprocess.run(["sudo", "-n", "chmod", "0644", str(path)], check=True)
        temporary.unlink(missing_ok=True)


def container_path(path: Path, root: Path) -> str:
    return "/workspace/" + path.resolve().relative_to(root.resolve()).as_posix()


def docker_host_user_arguments() -> list[str]:
    """Write bind-mounted artifacts as the invoking host user, never root."""
    return ["--user", f"{os.getuid()}:{os.getgid()}"]


def docker_model_cache_arguments(root: Path) -> list[str]:
    """Give model-loading containers a writable, ignored cache on the host.

    ``docker run --user`` deliberately prevents root-owned pipeline artifacts.
    Images such as the Grounding DINO + SAM runtime otherwise inherit ``HOME=/``
    and attempt to download models into ``/.cache``.  Keep that cache under the
    existing generated-work boundary instead.
    """
    layout = SceneRepositoryLayout.from_root(root)
    home = layout.container_home
    cache = layout.cache / "models"
    for path in (home, cache, cache / "huggingface", cache / "torch"):
        path.mkdir(parents=True, exist_ok=True)
    return [
        "-e", "HOME=/workspace/var/spatial/home",
        "-e", "XDG_CACHE_HOME=/workspace/var/spatial/cache/models",
        "-e", "HF_HOME=/workspace/var/spatial/cache/models/huggingface",
        "-e", "HF_HUB_CACHE=/workspace/var/spatial/cache/models/huggingface/hub",
        "-e", "TORCH_HOME=/workspace/var/spatial/cache/models/torch",
    ]


def colmap_camera_params_from_render_manifest(render_manifest: Path) -> str:
    """Read the calibrated virtual-camera intrinsics emitted by the renderer.

    The virtual rig is rendered with a known SIMPLE_PINHOLE calibration.  The
    COLMAP feature extractor must receive exactly that calibration; otherwise
    COLMAP creates default per-folder cameras and the subsequently fixed rig
    has inconsistent intrinsics.  Keep this as a manifest-derived value so
    that a virtual-rig configuration change remains a single source of truth.
    """
    try:
        manifest = json.loads(render_manifest.read_text(encoding="utf-8"))
        model = str(manifest["camera_model"])
        params = str(manifest["camera_params"])
        values = [float(value) for value in params.split(",")]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"render_manifest_camera_params_invalid:{error}") from error
    if model != "SIMPLE_PINHOLE" or len(values) != 3 or any(not math.isfinite(value) or value <= 0 for value in values):
        raise RuntimeError("render_manifest_camera_params_invalid")
    return ",".join(f"{value:.12g}" for value in values)


def virtual_rig_render_command(
    *,
    root: Path,
    config: dict[str, Any],
    panoramas: Path,
    images: Path,
    masks: Path,
    rig: Path,
    render_path: Path,
) -> list[str]:
    """Build a renderer command using host OpenCV or the generation image."""
    script = root / "generation_pipeline/tools/render_virtual_rig.py"
    arguments = [
        "--input_dir", panoramas,
        "--image_dir", images,
        "--mask_dir", masks,
        "--rig_config", rig,
        "--manifest", render_path,
        "--config", root / config["virtual_rig"]["config"],
    ]
    if importlib.util.find_spec("cv2") is not None:
        return [sys.executable, str(script), *[str(item) for item in arguments]]
    if shutil.which("docker") is None:
        raise RuntimeError("virtual_rig_renderer_unavailable:install_opencv_or_start_docker")
    image = str(config.get("person_mask", {}).get("docker_image", "spatial-3dgs-system-generation:latest"))
    container_arguments: list[str] = []
    for index, item in enumerate(arguments):
        container_arguments.append(item if index % 2 == 0 else container_path(item, root))
    return [
        "docker", "run", "--rm", *docker_host_user_arguments(), *docker_workspace_arguments(root), image,
        "python3", "generation_pipeline/tools/render_virtual_rig.py", *container_arguments,
    ]


def metric_tool_command(
    *,
    root: Path,
    config: dict[str, Any],
    script: Path,
    arguments: list[str],
) -> list[str]:
    """Run all metric tools in the generation image when Docker is available.

    ``numpy`` alone is not a complete metric-runtime probe: AprilTag
    detection additionally requires ``pupil_apriltags``.  Selecting the host
    just because NumPy happens to be installed caused App A jobs to complete
    SfM and then fail at the metric stage.  The generation image is already
    the declared runtime for the person-mask and AprilTag dependencies, so it
    is the reproducible default for the whole metric chain.

    A fully provisioned host remains a fallback only for installations where
    Docker is intentionally unavailable.
    """
    if shutil.which("docker") is not None:
        image = str(config.get("person_mask", {}).get("docker_image", "spatial-3dgs-system-generation:latest"))
        container_arguments: list[str] = []
        for value in arguments:
            path = Path(value)
            if path.is_absolute():
                try:
                    container_arguments.append(container_path(path, root))
                    continue
                except ValueError:
                    pass
            container_arguments.append(value)
        return [
            "docker", "run", "--rm", *docker_host_user_arguments(), *docker_workspace_arguments(root), image,
            "python3", container_path(script, root), *container_arguments,
        ]

    required_modules = ["numpy"]
    if script.name == "detect_apriltags.py":
        required_modules.append("pupil_apriltags")
    missing = [module for module in required_modules if importlib.util.find_spec(module) is None]
    if missing:
        raise RuntimeError(
            "metric_runtime_unavailable:install_" + "_and_".join(missing) + "_or_start_docker"
        )
    return [sys.executable, str(script), *arguments]


def colmap_model_converter_command(
    *, root: Path, config: dict[str, Any], input_model: Path, output_model: Path
) -> list[str]:
    """Convert the horizontalized text model into LFS's binary sparse model."""
    if shutil.which("docker") is None:
        raise RuntimeError("colmap_model_converter_unavailable:start_docker")
    image = str(config["sfm"].get("docker_image", PINNED_COLMAP_IMAGE))
    gpu = str(config["sfm"].get("gpu", "all"))
    return [
        "docker", "run", "--rm", "--gpus", gpu, *docker_host_user_arguments(),
        *docker_workspace_arguments(root),
        "--entrypoint", "colmap", image,
        "model_converter",
        "--input_path", container_path(input_model, root),
        "--output_path", container_path(output_model, root),
        "--output_type", "BIN",
    ]


def metric_reconstruction_package_path(config: dict[str, Any], root: Path) -> Path:
    reconstruction_id = config.get("reconstruction_id", f"{config['scene_id']}-fixed-rig")
    configured = config.get("metric_reconstruction_package_output")
    if isinstance(configured, str) and configured:
        return root / configured
    return SceneRepositoryLayout.from_root(root).metric_reconstructions / config["scene_id"] / reconstruction_id


def metric_markers(settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the configured AprilTag anchors, preserving legacy configs.

    New Application Preview jobs store one entry per fixed tag in ``markers``.
    The old family/id/size_m triplet remains accepted so existing scene configs
    and already-created job configs keep their exact behaviour.
    """
    configured = settings.get("markers")
    raw_markers = configured if isinstance(configured, list) else [{
        "family": settings.get("family"),
        "id": settings.get("id"),
        "size_m": settings.get("size_m"),
    }]
    if not raw_markers:
        raise RuntimeError("metric_markers_missing")
    markers: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for raw in raw_markers:
        if not isinstance(raw, dict):
            raise RuntimeError("metric_marker_invalid")
        family = raw.get("family")
        tag_id = raw.get("id")
        try:
            size_m = float(raw.get("size_m"))
        except (TypeError, ValueError) as error:
            raise RuntimeError("metric_marker_invalid") from error
        try:
            family = require_supported_apriltag_family(family)
        except ValueError as error:
            raise RuntimeError(f"metric_marker_invalid:{error}") from error
        if (
            not isinstance(tag_id, int)
            or isinstance(tag_id, bool)
            or tag_id < 0
            or tag_id > 9
            or not math.isfinite(size_m)
            or size_m <= 0
        ):
            raise RuntimeError("metric_marker_invalid")
        key = (family, tag_id)
        if key in seen:
            raise RuntimeError(f"metric_marker_duplicate:{family}/{tag_id}")
        seen.add(key)
        markers.append({"type": "apriltag", "family": family, "id": tag_id, "size_m": size_m})
    families = {str(marker["family"]) for marker in markers}
    if len(families) != 1:
        raise RuntimeError("metric_marker_multiple_families_not_supported")
    return markers


def legacy_main() -> None:
    parser = argparse.ArgumentParser(description="Reuse verified COLMAP dense output to build a surface-mesh proxy.")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--dense-workspace", type=Path, required=True)
    parser.add_argument("--world-alignment", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--scene-package", type=Path, required=True)
    parser.add_argument("--proxy-mode", choices=("surface_mesh",), default="surface_mesh")
    parser.add_argument("--reuse-dense", action="store_true", required=True)
    parser.add_argument("--docker-image", default=PINNED_COLMAP_IMAGE)
    args = parser.parse_args()
    root = args.repo_root.resolve()
    dense_workspace = args.dense_workspace.resolve()
    scene_package = args.scene_package.resolve()
    run_root = dense_workspace.parent
    fused = run_root / "fused.ply"
    dense_quality_path = run_root / "dense_quality.json"
    proxy_quality_path = run_root / "proxy_quality.json"
    final_quality_path = scene_package / "geometry" / "proxy_quality.json"
    manifest_path = scene_package / "scene_manifest.json"
    commands: list[list[str]] = []

    def disable_registered_proxy(reason: str) -> None:
        """Fail closed if a previous run had registered a proxy."""
        if not manifest_path.is_file():
            return
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.setdefault("assets", {})["proxy_mesh"] = None
        geometry_dir = scene_package / "geometry"
        for stale in (geometry_dir / "proxy.glb", geometry_dir / "proxy_metadata.json"):
            stale.unlink(missing_ok=True)
        pipeline = manifest.setdefault("provenance", {}).setdefault("coordinate_pipeline", {})
        pipeline["proxy_geometry"] = {
            "quality_uri": "geometry/proxy_quality.json",
            "metadata_uri": "geometry/proxy_metadata.json",
            "coordinate_space": "world",
            "disabled_reason": reason,
        }
        atomic_json(manifest_path, manifest)

    def failure(reason: str, status: str = "failed") -> None:
        quality = {
            "schema_version": "1.0.0",
            "stage": "surface_mesh",
            "proxy_kind": "surface_mesh",
            "status": status,
            "capabilities": {"measurement": False, "surface_placement": False, "fov": False, "occlusion": False, "semantic_object_query": False},
            "failure_reasons": [reason],
            "warnings": [],
            "provenance": {"semantic_proxy_status": "unverified"},
        }
        disable_registered_proxy(reason)
        atomic_json(proxy_quality_path, quality)
        atomic_json(final_quality_path, quality)
        raise SystemExit(2)

    if not args.reuse_dense:
        failure("dense_reuse_required")
    if not dense_workspace.is_dir() or not fused.is_file():
        failure("verified_dense_workspace_missing")

    dense_manifest_path = run_root / "dense_run_manifest.json"
    if not dense_manifest_path.is_file():
        failure("dense_run_manifest_missing")
    dense_manifest = json.loads(dense_manifest_path.read_text(encoding="utf-8"))
    source_image_count = int(dense_manifest.get("masks", {}).get("image_count", 0))
    if source_image_count <= 0:
        failure("dense_source_image_count_missing")

    dense_quality_tmp = Path(tempfile.mkstemp(prefix="dense_quality.", suffix=".json")[1])
    dense_eval_command = [
        "python3", str(root / "generation_pipeline/tools/evaluate_dense_geometry.py"),
        "--fused_ply", str(fused), "--world_alignment", str(args.world_alignment),
        "--config", str(args.config), "--dense_workspace", str(dense_workspace),
        "--source_image_count", str(source_image_count), "--output", str(dense_quality_tmp),
    ]
    commands.append(dense_eval_command)
    dense_eval = subprocess.run(dense_eval_command, text=True, capture_output=True)
    if not dense_quality_tmp.is_file():
        dense_quality_tmp.unlink(missing_ok=True)
        failure("dense_quality_cli_failed")
    dense_quality = json.loads(dense_quality_tmp.read_text(encoding="utf-8"))
    dense_quality_tmp.unlink(missing_ok=True)
    atomic_json(dense_quality_path, dense_quality)
    if dense_eval.returncode not in (0, 1) or dense_quality.get("status") != "verified":
        failure("dense_quality_not_verified", "unverified" if dense_quality.get("status") == "unverified" else "failed")

    # COLMAP's dense mesher reads fused.ply(.vis) from the dense workspace.
    for suffix in ("", ".vis"):
        link = dense_workspace / f"fused.ply{suffix}"
        target = run_root / f"fused.ply{suffix}"
        if not link.exists():
            link.symlink_to(os.path.relpath(target, link.parent))
    surface_ply = run_root / "surface_mesher.ply"
    mesher_command = [
        "docker", "run", "--rm", *docker_host_user_arguments(), *docker_workspace_arguments(root),
        args.docker_image, "colmap", "delaunay_mesher", "--input_path", container_path(dense_workspace, root),
        "--input_type", "dense", "--output_path", container_path(surface_ply, root),
    ]
    commands.append(mesher_command)
    result = subprocess.run(mesher_command, text=True, capture_output=True)
    if result.returncode or not surface_ply.is_file():
        failure("delaunay_mesher_failed")

    temporary_dir = Path(tempfile.mkdtemp(prefix="surface_proxy."))
    generator_command = [
        "python3", str(root / "generation_pipeline/tools/generate_proxy_geometry.py"),
        "--mode", args.proxy_mode, "--fused_ply", str(fused), "--surface_ply", str(surface_ply),
        "--world_alignment", str(args.world_alignment), "--config", str(args.config),
        "--dense_quality", str(dense_quality_path), "--output_dir", str(temporary_dir),
    ]
    commands.append(generator_command)
    generated = subprocess.run(generator_command, text=True, capture_output=True)
    generated_quality_path = temporary_dir / "proxy_quality.json"
    if generated.returncode not in (0, 2) or not generated_quality_path.is_file():
        shutil.rmtree(temporary_dir, ignore_errors=True)
        failure("surface_mesh_generation_failed")
    quality = json.loads(generated_quality_path.read_text(encoding="utf-8"))
    atomic_json(proxy_quality_path, quality)
    if quality.get("status") != "verified":
        shutil.rmtree(temporary_dir, ignore_errors=True)
        disable_registered_proxy("surface_mesh_quality_not_verified")
        atomic_json(final_quality_path, quality)
        raise SystemExit(2)

    geometry_dir = scene_package / "geometry"
    geometry_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = temporary_dir / "proxy_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    dense_relative = fused.relative_to(root).as_posix()
    alignment_relative = args.world_alignment.resolve().relative_to(root).as_posix()
    metadata["source_dense_cloud"] = dense_relative
    metadata.setdefault("world_transform", {})["source"] = alignment_relative
    metadata.setdefault("provenance", {}).update({
        "source_dense_cloud": dense_relative,
        "source_dense_quality": quality.get("provenance", {}).get("source_dense_quality"),
    })
    atomic_json(metadata_path, metadata)
    for name in ("proxy.glb", "proxy_metadata.json", "proxy_quality.json"):
        shutil.copy2(temporary_dir / name, geometry_dir / name)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["assets"]["proxy_mesh"] = {
        "uri": "geometry/proxy.glb",
        "purpose": "measurement_and_occlusion",
        "coordinate_space": "world",
        "metadata_uri": "geometry/proxy_metadata.json",
        "quality_uri": "geometry/proxy_quality.json",
    }
    pipeline = manifest.setdefault("provenance", {}).setdefault("coordinate_pipeline", {})
    pipeline["proxy_geometry"] = {
        "quality_uri": "geometry/proxy_quality.json",
        "metadata_uri": "geometry/proxy_metadata.json",
        "coordinate_space": "world",
        "transform_formula": "world = scale * Rx(180deg) * dense_aligned",
    }
    atomic_json(manifest_path, manifest)
    atomic_json(geometry_dir / "proxy_quality.json", quality)
    build_command = ["npm", "run", "build"]
    commands.append(build_command)
    build = subprocess.run(build_command, cwd=root / "web_viewer/app", text=True, capture_output=True)
    if build.returncode:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        disable_registered_proxy("viewer_build_failed")
        quality["status"] = "failed"
        quality["failure_reasons"] = ["viewer_build_failed"]
        atomic_json(proxy_quality_path, quality)
        atomic_json(final_quality_path, quality)
        raise SystemExit(2)
    atomic_json(proxy_quality_path, quality)
    atomic_json(geometry_dir / "proxy_quality.json", quality)
    shutil.rmtree(temporary_dir, ignore_errors=True)
    print(json.dumps({"quality": quality, "executed_commands": commands, "viewer_build_exit_code": build.returncode}, indent=2))
    if quality["status"] != "verified":
        raise SystemExit(2)


def dense_run_root(dense_workspace: Path) -> Path:
    """Return the new-run root that owns the configured inner ``dense/`` path."""
    if dense_workspace.name != "dense":
        raise RuntimeError("dense_workspace_must_be_the_inner_dense_directory")
    return dense_workspace.parent


def _scene_stage_artifacts(stage_id: str, config: dict[str, Any], root: Path) -> tuple[list[Path], list[Path]]:
    workspace = root / config["workspace"]
    if stage_id == "input_validation":
        return [root / config["input_video"]], [workspace / "input_manifest.json"]
    if stage_id == "preprocessing":
        preprocessing_inputs = (
            [root / config["input_video"]]
            if input_projection(config) == "perspective"
            else [root / config["virtual_rig"]["config"]]
        )
        render_manifest = config["virtual_rig"].get("render_manifest")
        return preprocessing_inputs, [
            root / render_manifest if render_manifest else workspace / "render_manifest.json"
        ]
    if stage_id == "person_mask":
        return (
            [root / config["person_mask"]["config"], root / config["person_mask"]["static_masks"]],
            [root / config["person_mask"]["report"]],
        ) if config.get("person_mask") else ([], [])
    if stage_id == "sfm":
        return [root / config["sfm"]["model"], root / config["sfm"]["manifest"]], []
    if stage_id == "metric":
        return (
            [root / config["sfm"]["model"]],
            [
                root / config["sfm"]["manifest"],
                root / config["metric_marker"].get("quality", str(workspace / "apriltag" / "quality.json")),
            ],
        )
    if stage_id == "lfs_export":
        return (
            [root / config["sfm"]["model"], root / config["sfm"].get("sparse_model", config["sfm"]["model"]), root / config["sfm"]["masks"]],
            [workspace / "lfs_ready" / "manifest.json"],
        )
    if stage_id == "metric_reconstruction_package":
        metric_package = metric_reconstruction_package_path(config, root)
        return (
            [root / config["metric_marker"]["world_alignment"], root / config["sfm"]["manifest"], workspace / "input_manifest.json"],
            [metric_package / "manifest.json"],
        )
    if stage_id == "gsplat":
        return [root / config["gsplat"]["spz"], root / config["gsplat"]["manifest"]], []
    if stage_id == "dense":
        dense_workspace = root / config["dense"]["workspace"]
        return [dense_workspace], [dense_workspace.parent / "dense_quality.json"]
    if stage_id in {"proxy", "scene_package", "viewer"}:
        viewer = root / config["viewer_publish_output"]
        if stage_id == "proxy":
            return [viewer / "scene_manifest.json"], [viewer / "geometry/proxy.glb", viewer / "geometry/proxy_quality.json"]
        if stage_id == "scene_package":
            package = root / config["scene_package_output"]
            return [viewer / "scene_manifest.json"], [package / "scene_manifest.json"]
        return [viewer / "scene_manifest.json"], [root / config["build"]["working_directory"] / "dist/index.html"]
    return [], []


def run_scene_config_pipeline(args: argparse.Namespace) -> int:
    root = args.repo_root.resolve()
    config_path = args.scene_config.resolve()
    try:
        config = load_scene_config(config_path, repo_root=root, check_paths=False)
    except SceneConfigError as error:
        print(json.dumps({"status": "failed", "failure_reasons": [str(error)]}, indent=2))
        return 2

    # The formal reconstruction application ends at verified MRP export, or
    # at horizontalized LichtFeld input when physical scale is intentionally
    # disabled.  Legacy callers may still request a later diagnostic stage
    # explicitly, but an omitted boundary must never fall through into the
    # retired mutable Scene Package pipeline.
    if args.stop_after is None:
        args.stop_after = reconstruction_stop_after(config)

    workspace = root / config["workspace"]
    workspace.mkdir(parents=True, exist_ok=True)
    report_path = workspace / "pipeline_report.json"
    profile = args.profile or config["execution"]["profile"]
    reuse = dict(config["reuse"])
    profile_settings = config.get("execution", {}).get("profiles", {}).get(profile, {})
    for key in ("sfm", "spz", "dense", "proxy"):
        if profile_settings.get(f"reuse_{key}") is True:
            reuse[key] = True
    for key in ("sfm", "spz", "dense", "proxy"):
        if getattr(args, f"reuse_{key}", False):
            reuse[key] = True
    prior: dict[str, Any] = {}
    if args.resume and report_path.is_file():
        try:
            prior = json.loads(report_path.read_text(encoding="utf-8"))
            for stage in prior.get("stages", []):
                if stage.get("status") in {"verified", "reused"}:
                    if stage.get("stage_id") == "person_mask":
                        reuse["person_mask"] = True
                    if stage.get("stage_id") == "sfm":
                        # A run may stop immediately after COLMAP (for example
                        # when an external SPZ command is not configured),
                        # before the metric stage has written its manifest.
                        # Reuse COLMAP only when both the model and its
                        # quality manifest are present; the partial case is
                        # handled by sfm_action and continues into metric.
                        sfm_model = root / config["sfm"]["model"]
                        sfm_manifest = root / config["sfm"]["manifest"]
                        reuse["sfm"] = sfm_model.is_dir() and sfm_manifest.is_file()
                    if stage.get("stage_id") == "gsplat":
                        reuse["spz"] = True
                    if stage.get("stage_id") == "dense":
                        reuse["dense"] = True
                    if stage.get("stage_id") == "proxy":
                        reuse["proxy"] = True
        except (OSError, json.JSONDecodeError):
            prior = {}

    try:
        config_reference = str(config_path.resolve().relative_to(root))
    except ValueError:
        config_reference = str(config_path.resolve())
    report: dict[str, Any] = {
        "schema_version": "1.0.0",
        "stage": "scene_pipeline",
        "scene_id": config["scene_id"],
        "status": "running",
        "started_at": utc_now(),
        "completed_at": None,
        "profile_requested": profile,
        "profile_effective": "quality" if profile == "auto" else profile,
        "scene_config": config_reference,
        "scene_config_sha256": sha256_file(config_path),
        "executed_commands": [],
        "stages": [],
        "failure_reasons": [],
        "warnings": [],
        "profile_settings": profile_settings,
        "profile_history": list(prior.get("profile_history", [])),
    }
    stage_ids = [
        "input_validation",
        "preprocessing",
        "person_mask",
        "sfm",
        "metric",
        "lfs_export",
        "metric_reconstruction_package",
    ]
    legacy_extension = ["gsplat", "dense", "proxy", "scene_package", "viewer"]
    if args.stop_after in legacy_extension:
        stage_ids.extend(legacy_extension)

    def save_report() -> None:
        atomic_json(report_path, report)

    def execute(stage_id: str, action: Any, *, command: list[str] | None = None) -> bool:
        inputs, outputs = _scene_stage_artifacts(stage_id, config, root)
        record = StageRecord(stage_id=stage_id, command=command)
        record.config_hash = report["scene_config_sha256"]
        record.input_artifacts = [artifact_record(path, root) for path in inputs if path.exists()]
        record.input_hashes = {
            item["path"]: item["sha256"] for item in record.input_artifacts if "sha256" in item
        }
        record.start()
        save_report()
        try:
            status, warnings = action(record)
            record.output_artifacts = [artifact_record(path, root) for path in outputs if path.exists()]
            record.output_hashes = {
                item["path"]: item["sha256"] for item in record.output_artifacts if "sha256" in item
            }
            if record.command:
                record.tool_version = record.command[0]
            record.reused = status == "reused"
            record.finish(status, warnings=warnings)
            report["stages"].append(record.as_dict())
            save_report()
            return True
        except StageUnverified as error:
            record.finish("unverified", failure_reasons=error.reasons)
            report["stages"].append(record.as_dict())
            report["failure_reasons"].extend(f"{stage_id}: {reason}" for reason in error.reasons)
            report["unverified_stage"] = stage_id
            report["exit_code"] = 2
            save_report()
            return False
        except Exception as error:  # noqa: BLE001 - failure must be serialized in the report
            record.finish("failed", failure_reasons=[str(error)])
            report["stages"].append(record.as_dict())
            report["failure_reasons"].append(f"{stage_id}: {error}")
            report["failed_stage"] = stage_id
            report["exit_code"] = 2
            save_report()
            return False

    def input_action(record: StageRecord) -> tuple[str, list[str]]:
        video = root / config["input_video"]
        if not video.is_file() or video.stat().st_size == 0:
            raise RuntimeError("input_video_missing_or_empty")
        probe: dict[str, Any] = {"status": "not_available"}
        probe_command = ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=width,height,avg_frame_rate", "-of", "json", str(video)]
        record.command = probe_command
        try:
            result = run_command(probe_command, cwd=root, timeout=60)
            report["executed_commands"].append(result)
            if result["exit_code"] == 0:
                probe = json.loads(result["stdout_tail"] or "{}")
            else:
                raise RuntimeError("ffprobe_failed")
        except (FileNotFoundError, json.JSONDecodeError) as error:
            raise RuntimeError(f"input_probe_failed: {error}") from error
        video_streams = [stream for stream in probe.get("streams", []) if stream.get("width") and stream.get("height")]
        if not video_streams:
            raise RuntimeError("input_has_no_video_stream")
        width = int(video_streams[0]["width"])
        height = int(video_streams[0]["height"])
        projection = validate_video_projection(config, width, height)
        duration = float(probe.get("format", {}).get("duration", 0.0) or 0.0)
        if duration <= 0:
            raise RuntimeError("input_duration_missing")
        if args.dry_run:
            return "skipped", ["dry-run: input_manifest was not written"]
        atomic_json(workspace / "input_manifest.json", {
            "schema_version": "1.0.0", "stage": "input_validation", "status": "verified",
            "scene_id": config["scene_id"], "input_video": config["input_video"],
            "size": video.stat().st_size, "sha256": sha256_file(video),
            "video": {
                "width": width,
                "height": height,
                "duration_sec": duration,
                "projection": projection,
                "equirectangular": projection == "equirectangular_360",
            },
            "probe": probe,
        })
        return "verified", []

    def preprocessing_action(record: StageRecord) -> tuple[str, list[str]]:
        projection = input_projection(config)
        render_manifest = config["virtual_rig"].get("render_manifest")
        render_path = root / render_manifest if render_manifest else workspace / "render_manifest.json"
        images = root / config["sfm"].get("images", "")
        # render_virtual_rig writes geometric validity masks.  Person masks
        # are generated in the following stage and must remain separate from
        # the COLMAP mask directory.
        static_mask_path = config.get("person_mask", {}).get("static_masks", config["sfm"].get("masks", ""))
        masks = root / static_mask_path
        rig = root / config["sfm"].get("rig_config", "")
        if projection == "perspective":
            if render_path.is_file() and images.is_dir() and masks.is_dir() and rig.is_file():
                manifest = json.loads(render_path.read_text(encoding="utf-8"))
                if manifest.get("input_projection") != "perspective" or int(manifest.get("frame_count", 0)) <= 0:
                    raise RuntimeError("perspective_frame_manifest_invalid")
                image_count = sum(1 for path in images.rglob("*") if path.is_file())
                mask_count = sum(1 for path in masks.rglob("*") if path.is_file())
                expected_count = int(manifest["frame_count"])
                if image_count != expected_count or mask_count != expected_count:
                    raise RuntimeError(f"preprocessing_count_mismatch:{image_count}:{mask_count}:{expected_count}")
                return "reused", ["existing_perspective_video_frames"]

            interval = float(config.get("preprocessing", {}).get("frame_interval_sec", 3.0))
            if any(path.exists() for path in (images, masks, rig, render_path)):
                raise RuntimeError("preprocessing_outputs_exist_but_are_incomplete")
            ffmpeg_command = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(root / config["input_video"]),
                "-vf", f"fps=1/{interval:g}",
                str(images / "frame_%06d.png"),
            ]
            record.command = ffmpeg_command
            if args.dry_run:
                return "skipped", ["dry-run: perspective frame extraction was not started"]
            images.mkdir(parents=True)
            extracted = run_command(ffmpeg_command, cwd=root)
            report["executed_commands"].append(extracted)
            if extracted["exit_code"] != 0:
                raise RuntimeError(f"perspective_frame_extraction_failed_exit_{extracted['exit_code']}")
            frame_count = write_full_validity_masks(images, masks)
            write_perspective_camera_contract(rig, render_path, frame_count)
            return "verified", []

        if render_path.is_file() and images.is_dir() and masks.is_dir() and rig.is_file():
            manifest = json.loads(render_path.read_text(encoding="utf-8"))
            if int(manifest.get("virtual_cameras_per_panorama", 0)) <= 0 or int(manifest.get("panorama_count", 0)) <= 0:
                raise RuntimeError("render_manifest_invalid")
            image_count = sum(1 for path in images.rglob("*") if path.is_file())
            mask_count = sum(1 for path in masks.rglob("*") if path.is_file())
            expected_count = int(manifest["panorama_count"]) * int(manifest["virtual_cameras_per_panorama"])
            if image_count != expected_count or mask_count != expected_count:
                raise RuntimeError(f"preprocessing_count_mismatch:{image_count}:{mask_count}:{expected_count}")
            return "reused", ["existing_rendered_virtual_rig"]

        panoramas = root / config.get("preprocessing", {}).get("panoramas", str(workspace / "panoramas"))
        interval = float(config.get("preprocessing", {}).get("frame_interval_sec", 3.0))
        if any(path.exists() for path in (panoramas, images, masks, rig, render_path)):
            raise RuntimeError("preprocessing_outputs_exist_but_are_incomplete")
        ffmpeg_command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(root / config["input_video"]), "-vf", f"fps=1/{interval:g}", str(panoramas / "frame_%06d.png")]
        render_command = virtual_rig_render_command(
            root=root,
            config=config,
            panoramas=panoramas,
            images=images,
            masks=masks,
            rig=rig,
            render_path=render_path,
        )
        record.command = ffmpeg_command
        if args.dry_run:
            return "skipped", ["dry-run: ffmpeg and render_virtual_rig were not started"]
        panoramas.mkdir(parents=True)
        extracted = run_command(ffmpeg_command, cwd=root)
        report["executed_commands"].append(extracted)
        if extracted["exit_code"] != 0:
            raise RuntimeError(f"panorama_extraction_failed_exit_{extracted['exit_code']}")
        rendered = run_command(render_command, cwd=root)
        report["executed_commands"].append(rendered)
        if rendered["exit_code"] != 0:
            raise RuntimeError(f"virtual_rig_render_failed_exit_{rendered['exit_code']}")
        return "verified", []

    def person_mask_action(record: StageRecord) -> tuple[str, list[str]]:
        settings = config.get("person_mask")
        if not settings:
            raise RuntimeError("person_mask_config_missing")
        report_path = root / settings["report"]
        person_masks = root / settings["person_masks"]
        colmap_masks = root / settings["colmap_masks"]
        if reuse.get("person_mask", False):
            if not report_path.is_file() or not person_masks.is_dir() or not colmap_masks.is_dir():
                raise RuntimeError("verified_person_mask_reuse_missing")
            quality = json.loads(report_path.read_text(encoding="utf-8"))
            if quality.get("status") != "verified":
                raise RuntimeError("person_mask_quality_not_verified")
            mask_config = json.loads((root / settings["config"]).read_text(encoding="utf-8"))
            expected_backend = str(mask_config.get("backend", ""))
            if expected_backend and quality.get("backend") != expected_backend:
                raise RuntimeError(f"person_mask_backend_mismatch:{quality.get('backend')}:{expected_backend}")
            return "reused", ["verified person mask set reused"]
        # Never let a failed or interrupted mask set look complete to the
        # next attempt.  The generator writes both directories afresh and the
        # report is the authoritative completion marker.
        for stale in (person_masks, colmap_masks):
            if stale.exists():
                shutil.rmtree(stale)
        report_path.unlink(missing_ok=True)
        command = [
            "docker", "run", "--rm", "--gpus", str(settings.get("gpu", "all")), *docker_host_user_arguments(),
            *docker_model_cache_arguments(root),
            *docker_workspace_arguments(root), settings.get("docker_image", "spatial-3dgs-system-generation:latest"),
            "python3", "generation_pipeline/tools/generate_person_masks.py",
            "--image-dir", container_path(root / config["sfm"]["images"], root),
            "--static-mask-dir", container_path(root / settings["static_masks"], root),
            "--person-mask-dir", container_path(root / settings["person_masks"], root),
            "--colmap-mask-dir", container_path(root / settings["colmap_masks"], root),
            "--report", container_path(root / settings["report"], root),
            "--config", container_path(root / settings["config"], root),
        ]
        if settings.get("model_asset"):
            command.extend(["--model-asset", container_path(root / settings["model_asset"], root)])
        record.command = command
        if args.dry_run:
            return "skipped", ["dry-run: person-mask generation was not started"]
        result = run_command(command, cwd=root)
        report["executed_commands"].append(result)
        if result["exit_code"] not in (0, 2) or not report_path.is_file():
            raise RuntimeError(f"person_mask_runner_failed_exit_{result['exit_code']}")
        quality = json.loads(report_path.read_text(encoding="utf-8"))
        if quality.get("status") != "verified":
            raise RuntimeError("person_mask_quality_not_verified")
        return "verified", []

    def sfm_action(record: StageRecord) -> tuple[str, list[str]]:
        projection = input_projection(config)
        model = root / config["sfm"]["model"]
        manifest_path = root / config["sfm"]["manifest"]
        if reuse["sfm"]:
            if not model.is_dir() or not manifest_path.is_file():
                raise RuntimeError("sfm_model_or_manifest_missing")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            expected_masks = Path(str(config["sfm"].get("masks", ""))).as_posix()
            recorded_masks = Path(str(manifest.get("input_masks", ""))).as_posix()
            if not recorded_masks:
                raise RuntimeError("sfm_mask_source_missing")
            if recorded_masks != expected_masks:
                raise RuntimeError(f"sfm_mask_source_mismatch:{recorded_masks}:{expected_masks}")
            metrics = manifest.get("colmap", {})
            if int(metrics.get("registered_images", 0)) <= 0:
                raise RuntimeError("sfm_metrics_missing")
            return "reused", [f"{config['sfm']['profile']} SfM is reused from the verified reconstruction"]
        sfm_workspace = root / config["sfm"].get("workspace", str(root / config["workspace"] / "colmap_sequential_generated"))
        database = sfm_workspace / "database.db"
        sparse = sfm_workspace / "sparse"
        if model.is_dir() and (model / "images.txt").is_file() and database.is_file() and (sparse / "0").is_dir():
            return "verified", [f"completed {config['sfm']['profile']} COLMAP model reused; metric manifest will be regenerated"]
        if sfm_workspace.exists() and any(sfm_workspace.iterdir()):
            raise RuntimeError("sfm_workspace_exists_requires_new_workspace")
        docker_image = config["sfm"].get("docker_image", PINNED_COLMAP_IMAGE)
        gpu = str(config["sfm"].get("gpu", "all"))
        def colmap_command(*arguments: str) -> list[str]:
            return ["docker", "run", "--rm", "--gpus", gpu, *docker_host_user_arguments(), *docker_workspace_arguments(root), "--entrypoint", "colmap", docker_image, *arguments]
        image_path = root / config["sfm"].get("images", "")
        mask_path = root / config["sfm"].get("masks", "")
        rig_config = root / config["sfm"].get("rig_config", "")
        if projection == "perspective":
            commands = [
                colmap_command("feature_extractor", "--database_path", container_path(database, root), "--image_path", container_path(image_path, root), "--ImageReader.camera_model", "SIMPLE_RADIAL", "--ImageReader.single_camera", "1", "--ImageReader.mask_path", container_path(mask_path, root), "--FeatureExtraction.use_gpu", "1", "--SiftExtraction.max_num_features", "8192"),
                colmap_command("sequential_matcher", "--database_path", container_path(database, root), "--FeatureMatching.use_gpu", "1", "--FeatureMatching.guided_matching", "1", "--SequentialMatching.overlap", "10", "--SequentialMatching.quadratic_overlap", "1", "--SequentialMatching.loop_detection", "0"),
                colmap_command("mapper", "--database_path", container_path(database, root), "--image_path", container_path(image_path, root), "--output_path", container_path(sparse, root), "--Mapper.ba_refine_focal_length", "1", "--Mapper.ba_refine_principal_point", "0", "--Mapper.ba_refine_extra_params", "1"),
                colmap_command("model_converter", "--input_path", container_path(sparse / "0", root), "--output_path", container_path(model, root), "--output_type", "TXT"),
            ]
        else:
            render_manifest_value = config["virtual_rig"].get("render_manifest")
            render_manifest_path = root / render_manifest_value if render_manifest_value else workspace / "render_manifest.json"
            camera_params = colmap_camera_params_from_render_manifest(render_manifest_path)
            commands = [
                colmap_command("feature_extractor", "--database_path", container_path(database, root), "--image_path", container_path(image_path, root), "--ImageReader.camera_model", "SIMPLE_PINHOLE", "--ImageReader.camera_params", camera_params, "--ImageReader.single_camera_per_folder", "1", "--ImageReader.mask_path", container_path(mask_path, root), "--FeatureExtraction.use_gpu", "1", "--SiftExtraction.max_num_features", "8192"),
                colmap_command("rig_configurator", "--database_path", container_path(database, root), "--rig_config_path", container_path(rig_config, root)),
                colmap_command("sequential_matcher", "--database_path", container_path(database, root), "--FeatureMatching.use_gpu", "1", "--FeatureMatching.guided_matching", "1", "--FeatureMatching.rig_verification", "1", "--FeatureMatching.skip_image_pairs_in_same_frame", "0", "--SequentialMatching.overlap", "10", "--SequentialMatching.quadratic_overlap", "1", "--SequentialMatching.expand_rig_images", "1", "--SequentialMatching.loop_detection", "0"),
                colmap_command("mapper", "--database_path", container_path(database, root), "--image_path", container_path(image_path, root), "--output_path", container_path(sparse, root), "--Mapper.ba_refine_sensor_from_rig", "0", "--Mapper.ba_refine_focal_length", "0", "--Mapper.ba_refine_principal_point", "0", "--Mapper.ba_refine_extra_params", "0"),
                colmap_command("model_converter", "--input_path", container_path(sparse / "0", root), "--output_path", container_path(model, root), "--output_type", "TXT"),
            ]
        record.command = commands[0]
        if args.dry_run:
            return "skipped", [f"dry-run: {config['sfm']['profile']} COLMAP commands were not started"]
        sfm_workspace.mkdir(parents=True)
        sparse.mkdir(parents=True, exist_ok=True)
        model.mkdir(parents=True, exist_ok=True)
        for command in commands:
            result = run_command(command, cwd=root)
            report["executed_commands"].append(result)
            if result["exit_code"] != 0:
                raise RuntimeError(f"sfm_runner_failed_exit_{result['exit_code']}")
        return "verified", []

    def gsplat_action(record: StageRecord) -> tuple[str, list[str]]:
        spz = root / config["gsplat"]["spz"]
        manifest_path = root / config["gsplat"]["manifest"]
        if reuse["spz"]:
            if not spz.is_file() or spz.stat().st_size == 0 or not manifest_path.is_file():
                raise RuntimeError("spz_artifact_or_manifest_missing")
            report["gsplat_provenance"] = {
                "asset": str(spz.relative_to(root)), "size": spz.stat().st_size, "sha256": sha256_file(spz),
                "profile": config["gsplat"]["profile"], "reused": reuse["spz"],
            }
            return "reused", ["SPZ is an external verified stage"]
        command = config["gsplat"].get("command")
        if not command:
            return "skipped", ["external SPZ/LichtFeld stage is not configured; App A stops at MRP export"]
        record.command = list(command)
        if args.dry_run:
            return "skipped", ["dry-run: configured SPZ command was not started"]
        result = run_command(list(command), cwd=root)
        report["executed_commands"].append(result)
        if result["exit_code"] != 0:
            raise RuntimeError(f"spz_runner_failed_exit_{result['exit_code']}")
        return "verified", []

    def metric_action(record: StageRecord) -> tuple[str, list[str]]:
        settings = config["metric_marker"]
        marker_enabled = bool(settings.get("enabled", True))
        alignment_path = root / settings["world_alignment"]
        corners_path = root / settings["corners_world"]
        sfm_workspace = root / config["sfm"].get("workspace", config["workspace"] + "/colmap_sequential")
        trajectory_path = root / config["sfm"].get("trajectory", str(sfm_workspace / "trajectory_diagnostic.aligned.json"))
        source_model_reference = config["sfm"].get("source_model", config["sfm"]["model"])
        source_model_path = Path(str(source_model_reference))
        if source_model_path.name == "text_horizontalized" and not config["sfm"].get("source_model"):
            candidate = source_model_path.parent / "text"
            if (root / candidate).is_dir():
                source_model_reference = candidate.as_posix()
        model = root / str(source_model_reference)
        aligned_model = model.parent / "text_horizontalized"
        images = root / config["sfm"]["images"]
        metric_workspace = root / config["workspace"] / "apriltag"
        metric_workspace.mkdir(parents=True, exist_ok=True)
        detections = metric_workspace / "detections.json"
        triangulated = metric_workspace / "triangulated_corners.json"
        quality_path = metric_workspace / "quality.json"
        configured_quality_path = settings.get("quality")
        if marker_enabled:
            if not isinstance(configured_quality_path, str) or not configured_quality_path:
                raise StageUnverified(["metric_quality_gate_path_missing"])
            if (root / configured_quality_path).resolve() != quality_path.resolve():
                # AprilTag quality is produced by this stage, so it must live
                # beside the current job workspace.  Early App A jobs were
                # written with the source scene's quality path while their
                # workspace had already been made project-specific.  Repair
                # only those persisted App A job configs; a normal scene
                # config with an inconsistent path remains an explicit error.
                layout = SceneRepositoryLayout.from_root(root)
                preview_job_roots = {
                    layout.jobs.resolve(),
                    layout.legacy_jobs.resolve(),
                }
                if not any(job_root in config_path.resolve().parents for job_root in preview_job_roots):
                    raise RuntimeError("metric_quality_path_does_not_match_workspace")
                settings["quality"] = str(quality_path.relative_to(root))
                configured_quality_path = settings["quality"]
        horizontal = metric_workspace / "horizontal_alignment.txt"
        horizontal_report = metric_workspace / "horizontal_alignment.json"
        aligned_sparse = sfm_workspace / "sparse_horizontalized"
        scale_markers = metric_workspace / "scale_markers.json"
        tool_root = root / "generation_pipeline/tools"
        commands: list[list[str]] = []
        alignment: dict[str, Any] | None = None
        corners: dict[str, Any] | None = None
        quality: dict[str, Any] = {}
        if marker_enabled and alignment_path.is_file() and corners_path.is_file():
            try:
                alignment = json.loads(alignment_path.read_text(encoding="utf-8"))
                corners = json.loads(corners_path.read_text(encoding="utf-8"))
                if alignment.get("status") != "verified" or alignment.get("unit") != "meter" or len(corners.get("corners_world_m", [])) != 4:
                    alignment = None
                    corners = None
                elif quality_path.is_file():
                    quality = json.loads(quality_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                alignment = None
                corners = None

        # Never reuse an older scale merely because its alignment file exists.
        # The triangulation quality report is the independent acceptance gate;
        # scale was itself estimated from these four corners and cannot verify
        # their shape or ray intersection quality.
        if marker_enabled and alignment is not None:
            if quality.get("status") != "verified":
                reasons = quality.get("failure_reasons")
                if not isinstance(reasons, list) or not reasons:
                    reasons = ["apriltag_metric_quality_report_missing_or_not_verified"]
                raise StageUnverified([str(reason) for reason in reasons])

        # The rig-up rotation is independent of AprilTag scale and is always
        # required.  Convert the rotated text model back to COLMAP binary so
        # LichtFeld consumes exactly the same camera poses.
        need_horizontalization = not horizontal.is_file() or not horizontal_report.is_file()
        if need_horizontalization:
            horizontal_arguments = ["--colmap_model", str(model), "--transform", str(horizontal), "--report", str(horizontal_report)]
            if input_projection(config) == "perspective":
                horizontal_arguments.extend(["--input_projection", "perspective", "--camera_prefix", ""])
            commands.append(metric_tool_command(root=root, config=config, script=tool_root / "derive_rig_up_alignment.py", arguments=horizontal_arguments))
        if need_horizontalization or not aligned_model.is_dir() or not (aligned_model / "images.txt").is_file():
            commands.append(metric_tool_command(root=root, config=config, script=tool_root / "apply_colmap_world_rotation.py", arguments=["--input_model", str(model), "--rotation", str(horizontal), "--output_model", str(aligned_model)]))
        required_sparse = ("cameras.bin", "images.bin", "points3D.bin")
        if need_horizontalization or any(not (aligned_sparse / name).is_file() for name in required_sparse):
            aligned_sparse.mkdir(parents=True, exist_ok=True)
            commands.append(colmap_model_converter_command(root=root, config=config, input_model=aligned_model, output_model=aligned_sparse))

        if marker_enabled and alignment is None:
            markers = metric_markers(settings)
            scale_markers.write_text(
                json.dumps({"markers": markers}, indent=2) + "\n",
                encoding="utf-8",
            )
            commands[0:0] = [
                metric_tool_command(root=root, config=config, script=tool_root / "detect_apriltags.py", arguments=["--image_root", str(images), "--tag_family", str(markers[0]["family"]), "--output", str(detections)]),
                metric_tool_command(root=root, config=config, script=tool_root / "triangulate_apriltag_corners.py", arguments=["--detections", str(detections), "--colmap_model", str(model), "--output", str(triangulated)]),
                metric_tool_command(root=root, config=config, script=tool_root / "evaluate_apriltag_triangulation.py", arguments=["--triangulated_corners", str(triangulated), "--scale_markers", str(scale_markers), "--scene_config", str(config_path), "--output", str(quality_path)]),
            ]
            commands.extend([
                metric_tool_command(root=root, config=config, script=tool_root / "estimate_world_alignment.py", arguments=["--triangulated_corners", str(triangulated), "--scale_markers", str(scale_markers), "--output", str(alignment_path)]),
                metric_tool_command(root=root, config=config, script=tool_root / "export_apriltag_world_corners.py", arguments=["--triangulated_corners", str(triangulated), "--scale_markers", str(scale_markers), "--horizontal_alignment", str(horizontal), "--world_alignment", str(alignment_path), "--asset_rotation_quaternion_xyzw", "1,0,0,0", "--output", str(corners_path)]),
            ])
        if not trajectory_path.is_file():
            commands.append(metric_tool_command(root=root, config=config, script=tool_root / "diagnose_colmap_trajectory.py", arguments=["--colmap_model", str(aligned_model), "--output", str(trajectory_path)]))
        if args.dry_run:
            record.command = commands[0] if commands else ["verified_artifacts"]
            return "skipped", ["dry-run: horizontalization and optional AprilTag scale alignment were not started"]
        for command in commands:
            result = run_command(command, cwd=root)
            report["executed_commands"].append(result)
            if result["exit_code"] != 0:
                tool_name = next(
                    (Path(token).stem for token in command if token.endswith(".py")),
                    "metric_tool",
                )
                detail_lines = str(result.get("stderr_tail") or "").strip().splitlines()
                detail = detail_lines[-1].strip() if detail_lines else "no_error_detail"
                raise RuntimeError(
                    f"{tool_name}_failed_exit_{result['exit_code']}:{detail}"
                )
            if any(Path(token).name == "evaluate_apriltag_triangulation.py" for token in command):
                try:
                    quality = json.loads(quality_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, json.JSONDecodeError) as error:
                    raise RuntimeError("apriltag_metric_quality_report_missing") from error
                if quality.get("status") != "verified":
                    reasons = quality.get("failure_reasons")
                    if not isinstance(reasons, list) or not reasons:
                        reasons = ["apriltag_metric_quality_not_verified"]
                    raise StageUnverified([str(reason) for reason in reasons])
        if marker_enabled:
            quality = json.loads(quality_path.read_text(encoding="utf-8"))
            alignment = json.loads(alignment_path.read_text(encoding="utf-8"))
            corners = json.loads(corners_path.read_text(encoding="utf-8"))
            if quality.get("status") != "verified" or alignment.get("status") != "verified" or len(corners.get("corners_world_m", [])) != 4:
                raise RuntimeError("apriltag_metric_quality_not_verified")
            report["metric_transform"] = alignment.get("generated_to_world", {})
            report["apriltag_quality"] = quality
            report["apriltag_corners"] = str(corners_path.relative_to(root))
        if not aligned_model.is_dir():
            raise RuntimeError("horizontalized_colmap_model_missing")
        if any(not (aligned_sparse / name).is_file() for name in required_sparse):
            raise RuntimeError("horizontalized_colmap_sparse_missing")
        # The native COLMAP commands produce the model; this manifest is the
        # runner's quality boundary consumed by the MRP exporter and resume.
        image_lines = [
            line for line in (aligned_model / "images.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#") and len(line.split()) == 10
        ]
        point_lines = [
            line for line in (aligned_model / "points3D.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        observation_count = sum(max(0, (len(line.split()) - 8) // 2) for line in point_lines)
        reprojection_values = [float(line.split()[7]) for line in point_lines if len(line.split()) > 7]
        sfm_manifest = {
            "schema_version": "1.0.0",
            "input_projection": input_projection(config),
            "profile": config["sfm"]["profile"],
            "workspace": str((root / config["sfm"].get("workspace", config["workspace"])).relative_to(root)),
            "input_images": config["sfm"]["images"],
            "input_masks": config["sfm"]["masks"],
            "rig_config": config["sfm"]["rig_config"],
            "colmap": {
                "version": config["sfm"].get("docker_image", "COLMAP"),
                "model": str(aligned_model.relative_to(root)),
                "registered_images": len(image_lines),
                "points": len(point_lines),
                "observations": observation_count,
                "mean_reprojection_error_px": float(statistics.mean(reprojection_values)) if reprojection_values else 0.0,
            },
            "horizontalization": {
                "path": str(horizontal_report.relative_to(root)),
                "sparse_model": str(aligned_sparse.relative_to(root)),
            },
        }
        if marker_enabled and alignment is not None:
            sfm_manifest["apriltag_quality"] = quality
            sfm_manifest["world_alignment"] = {"path": str(alignment_path.relative_to(root)), "scale_m_per_colmap_unit": alignment["generated_to_world"]["scale"]}
        atomic_json(root / config["sfm"]["manifest"], sfm_manifest)
        config["sfm"]["source_model"] = str(model.relative_to(root))
        config["sfm"]["model"] = str(aligned_model.relative_to(root))
        config["sfm"]["sparse_model"] = str(aligned_sparse.relative_to(root))
        config["sfm"]["trajectory"] = str(trajectory_path.relative_to(root))
        layout = SceneRepositoryLayout.from_root(root)
        preview_job_roots = {layout.jobs.resolve(), layout.legacy_jobs.resolve()}
        if config_path.is_file() and config_path.parent.resolve() in preview_job_roots:
            atomic_json(config_path, config)
        record.command = commands[0] if commands else ["verified_horizontalized_sparse"]
        if marker_enabled:
            return ("reused" if reuse["sfm"] and not commands else "verified"), []
        return ("reused" if reuse["sfm"] and not commands else "verified"), ["AprilTag disabled: horizontalized, non-metric COLMAP/LichtFeld input only"]

    def lfs_export_action(record: StageRecord) -> tuple[str, list[str]]:
        exporter = root / "generation_pipeline/tools/export_metric_reconstruction_package.py"
        record.command = [sys.executable, str(exporter), "--repo-root", str(root), "--scene-config", str(config_path)]
        if args.dry_run:
            return "skipped", ["dry-run: LichtFeld Studio input export was not started"]
        result = export_lfs_input(root, config_path)
        report["lfs_input"] = {
            "path": str(Path(result["path"]).relative_to(root)),
            "status": result["status"],
            "coordinate": result.get("coordinate", {}),
        }
        if result.get("status") != "verified":
            raise RuntimeError("lfs_input_export_not_verified")
        return ("reused" if result.get("reused") else "verified"), []

    def metric_reconstruction_package_action(record: StageRecord) -> tuple[str, list[str]]:
        if not bool(config["metric_marker"].get("enabled", True)):
            return "skipped", ["AprilTag disabled: Metric Reconstruction Package requires verified physical scale"]
        package = metric_reconstruction_package_path(config, root)
        exporter = root / "generation_pipeline/tools/export_metric_reconstruction_package.py"
        record.command = [sys.executable, str(exporter), "--repo-root", str(root), "--scene-config", str(config_path), "--output", str(package)]
        if args.dry_run:
            return "skipped", ["dry-run: Metric Reconstruction Package export was not started"]
        result = export_package(root, config_path, package)
        report["metric_reconstruction_package_validation"] = result
        if result.get("status") != "verified":
            raise RuntimeError("metric_reconstruction_package_export_not_verified")
        return "verified", []

    def dense_action(record: StageRecord) -> tuple[str, list[str]]:
        dense_workspace = root / config["dense"]["workspace"]
        dense_root = dense_run_root(dense_workspace)
        quality_path = dense_workspace.parent / "dense_quality.json"
        fused = dense_workspace.parent / "fused.ply"
        if reuse["dense"]:
            quality = json.loads(quality_path.read_text(encoding="utf-8")) if quality_path.is_file() else {}
            if quality.get("status") != "verified" or not fused.is_file():
                raise RuntimeError("verified_dense_reuse_missing")
            report["dense_quality_reference"] = {"uri": str(quality_path.relative_to(root)), "sha256": sha256_file(quality_path)}
            return "reused", ["verified fused.ply and dense_quality.json reused"]
        command = [sys.executable, str(root / "generation_pipeline/tools/run_colmap_dense_geometry_poc.py"), "--repo_root", str(root), "--input_model", str(root / config["sfm"]["model"]), "--image_path", str(root / config["sfm"].get("images", "")), "--mask_path", str(root / config["sfm"].get("masks", "")), "--workspace", str(dense_root), "--world_alignment", str(root / config["dense"]["world_alignment"]), "--config", str(root / config["dense"]["config"])]
        record.command = command
        if args.dry_run:
            record.command = command
            return "skipped", ["dry-run: Dense MVS command was not started"]
        result = run_command(command, cwd=root)
        report["executed_commands"].append(result)
        if result["exit_code"] != 0:
            raise RuntimeError(f"dense_runner_failed_exit_{result['exit_code']}")
        return "verified", []

    def proxy_action(record: StageRecord) -> tuple[str, list[str]]:
        del record
        # This runner formerly wrote geometry into the mutable Viewer root.
        # A proxy that belongs to a Scene Package must now be produced before
        # immutable MRP/SPZ registration; the legacy downstream stages are
        # deliberately fail-closed rather than modifying historical data.
        raise RuntimeError("legacy_root_scene_package_pipeline_retired:use_verified_mrp_and_spz_import")

    def package_action(record: StageRecord) -> tuple[str, list[str]]:
        del record
        raise RuntimeError("legacy_root_scene_package_pipeline_retired:use_verified_mrp_and_spz_import")

    def viewer_action(record: StageRecord) -> tuple[str, list[str]]:
        command = list(config["build"]["command"])
        cwd = root / config["build"]["working_directory"]
        record.command = command
        if args.dry_run:
            record.command = command
            return "skipped", ["dry-run: viewer build was not started"]
        result = run_command(command, cwd=cwd)
        report["executed_commands"].append(result)
        if result["exit_code"] != 0:
            raise RuntimeError(f"viewer_build_failed_exit_{result['exit_code']}")
        smoke_command = config["build"].get("smoke_command")
        if smoke_command:
            smoke = run_command(list(smoke_command), cwd=cwd)
            report["executed_commands"].append(smoke)
            if smoke["exit_code"] != 0:
                raise RuntimeError(f"viewer_smoke_failed_exit_{smoke['exit_code']}")
        return "verified", []

    actions = {
        "input_validation": input_action,
        "preprocessing": preprocessing_action,
        "person_mask": person_mask_action,
        "sfm": sfm_action,
        "gsplat": gsplat_action,
        "metric": metric_action,
        "lfs_export": lfs_export_action,
        "metric_reconstruction_package": metric_reconstruction_package_action,
        "dense": dense_action,
        "proxy": proxy_action,
        "scene_package": package_action,
        "viewer": viewer_action,
    }
    for stage_id in stage_ids:
        if not execute(stage_id, actions[stage_id]):
            break
        if args.stop_after == stage_id:
            report["stopped_after"] = stage_id
            break
    complete = not report.get("failure_reasons") and len(report["stages"]) == len(stage_ids)
    if report.get("unverified_stage"):
        report["status"] = "unverified"
    elif complete and all(stage["status"] in {"verified", "reused"} for stage in report["stages"]):
        report["status"] = "verified"
    elif report.get("failure_reasons"):
        report["status"] = "failed"
    elif args.stop_after:
        # App A's selected boundary is the MRP export.  Reaching that
        # boundary with no failure is a verified App A result, while the
        # explicit scope keeps downstream Gaussian/Viewer stages fail-closed.
        report["status"] = "verified"
        report["verification_scope"] = f"through:{args.stop_after}"
        report["warnings"].append(f"pipeline_stopped_after:{args.stop_after}")
    else:
        report["status"] = "skipped"
    report["completed_at"] = utc_now()
    durations = {stage["stage_id"]: stage.get("duration_sec") for stage in report["stages"]}
    workspace_bytes = sum(path.stat().st_size for path in workspace.rglob("*") if path.is_file())
    disk = shutil.disk_usage(root)
    gpu_snapshot: dict[str, Any] = {"status": "not_available"}
    try:
        gpu = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"],
            text=True, capture_output=True, timeout=10,
        )
        if gpu.returncode == 0:
            gpu_snapshot = {"status": "verified", "csv": gpu.stdout.strip()}
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    report["performance"] = {
        "total_duration_sec": (datetime.fromisoformat(report["completed_at"]) - datetime.fromisoformat(report["started_at"])).total_seconds(),
        "stage_durations_sec": durations,
        "workspace_bytes": workspace_bytes,
        "disk_free_bytes": disk.free,
        "gpu_snapshot": gpu_snapshot,
    }
    report["profile_history"].append({
        "profile": profile,
        "status": report["status"],
        "total_duration_sec": report["performance"]["total_duration_sec"],
        "stage_durations_sec": durations,
    })
    save_report()
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "verified" else 2


def main() -> None:
    if "--scene-config" not in sys.argv:
        legacy_main()
        return
    parser = argparse.ArgumentParser(description="Run the scene-config driven COLMAP-to-MRP reconstruction pipeline.")
    parser.add_argument("--scene-config", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--profile", choices=("fast", "quality", "auto"))
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--reuse-sfm", action="store_true")
    parser.add_argument("--reuse-spz", action="store_true")
    parser.add_argument("--reuse-dense", action="store_true")
    parser.add_argument("--reuse-proxy", action="store_true")
    parser.add_argument("--stop-after", choices=("input_validation", "preprocessing", "person_mask", "sfm", "gsplat", "metric", "lfs_export", "metric_reconstruction_package", "dense", "proxy", "scene_package", "viewer"))
    parser.add_argument("--dry-run", action="store_true")
    result = run_scene_config_pipeline(parser.parse_args())
    raise SystemExit(result)


if __name__ == "__main__":
    main()
