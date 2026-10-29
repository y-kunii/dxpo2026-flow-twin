from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__:
    from .evaluate_dense_geometry import evaluate_dense_geometry
    from .docker_runtime import PINNED_COLMAP_IMAGE, docker_workspace_arguments
else:
    from evaluate_dense_geometry import evaluate_dense_geometry
    from docker_runtime import PINNED_COLMAP_IMAGE, docker_workspace_arguments


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def container_path(path: Path, repo_root: Path) -> str:
    try:
        relative = path.resolve().relative_to(repo_root.resolve())
    except ValueError as error:
        raise RuntimeError(f"Path must be within repo root: {path}") from error
    return str(Path("/workspace") / relative)


def run_colmap(
    command: list[str],
    step: str,
    command_log: list[dict[str, Any]],
) -> None:
    started_at = utc_now()
    completed = subprocess.run(command, text=True, capture_output=True)
    command_log.append(
        {
            "step": step,
            "command": command,
            "started_at": started_at,
            "completed_at": utc_now(),
            "return_code": completed.returncode,
            "stdout_tail": completed.stdout[-4000:],
            "stderr_tail": completed.stderr[-4000:],
        }
    )
    if completed.returncode:
        raise RuntimeError(f"COLMAP {step} failed with exit code {completed.returncode}.")


def validate_masks(image_path: Path, mask_path: Path | None) -> dict[str, Any]:
    if mask_path is None:
        return {"configured": False, "applied_to": "none", "missing_count": 0}

    images = sorted(path for path in image_path.rglob("*") if path.is_file())
    missing = [
        str(path.relative_to(image_path))
        for path in images
        if not (mask_path / f"{path.relative_to(image_path)}.png").is_file()
    ]
    return {
        "configured": True,
        "mask_path": str(mask_path),
        "image_count": len(images),
        "missing_count": len(missing),
        "missing_examples": missing[:20],
        "applied_to": "stereo_fusion",
    }


def normalize_model_image_names(input_model: Path, image_path: Path, output_model: Path) -> dict[str, Any]:
    """Copy a COLMAP TXT model with image names resolved against the source images.

    LichtFeld preparation can change only filename extensions (for example PNG to
    JPEG) while keeping the COLMAP poses valid. Dense COLMAP must instead use
    names that exist below its --image_path. This accepts an exact name or one
    unique same-stem image with a supported extension; anything else fails.
    """
    required_files = ("cameras.txt", "images.txt", "points3D.txt")
    for filename in required_files:
        if not (input_model / filename).is_file():
            raise RuntimeError(f"COLMAP TXT model is missing {filename}: {input_model}")
    output_model.mkdir()
    for filename in ("cameras.txt", "points3D.txt"):
        shutil.copy2(input_model / filename, output_model / filename)

    remapped = 0
    total = 0
    expect_image_header = True
    output_lines: list[str] = []
    for raw_line in (input_model / "images.txt").read_text(encoding="utf-8").splitlines():
        if raw_line.startswith("#"):
            output_lines.append(raw_line)
            continue
        if not raw_line:
            if not expect_image_header:
                expect_image_header = True
            output_lines.append(raw_line)
            continue
        if expect_image_header:
            fields = raw_line.split()
            if len(fields) < 10:
                raise RuntimeError(f"Invalid COLMAP image record: {raw_line}")
            source_name = Path(fields[-1])
            if not (image_path / source_name).is_file():
                candidates = [
                    candidate
                    for extension in (".png", ".jpg", ".jpeg", ".PNG", ".JPG", ".JPEG")
                    if (candidate := image_path / source_name.with_suffix(extension)).is_file()
                ]
                if len(candidates) != 1:
                    raise RuntimeError(
                        f"Cannot map COLMAP image {source_name} to exactly one source image."
                    )
                fields[-1] = str(candidates[0].relative_to(image_path))
                raw_line = " ".join(fields)
                remapped += 1
            total += 1
        expect_image_header = not expect_image_header
        output_lines.append(raw_line)

    if not total:
        raise RuntimeError("COLMAP TXT model does not contain image records.")
    (output_model / "images.txt").write_text(
        "\n".join(output_lines) + "\n", encoding="utf-8"
    )
    return {
        "source_model": str(input_model),
        "normalized_model": str(output_model),
        "image_record_count": total,
        "remapped_image_name_count": remapped,
    }


def _uniform_indices(item_count: int, selected_count: int) -> list[int]:
    """Return stable, endpoint-preserving indices for one camera stream."""
    if selected_count >= item_count:
        return list(range(item_count))
    if selected_count <= 1:
        return [item_count // 2]
    return [round(index * (item_count - 1) / (selected_count - 1)) for index in range(selected_count)]


def select_patch_match_references(
    configuration_path: Path,
    maximum_reference_images: int | None,
) -> dict[str, Any]:
    """Bound dense reference views without hiding the complete input dataset.

    COLMAP's generated ``patch-match.cfg`` contains two lines per registered
    image.  We retain complete pairs and allocate a proportional quota to each
    virtual-camera directory, then sample each stream uniformly over time.
    Source-view selection remains COLMAP's audited ``__auto__`` rule.  This
    changes only which images receive depth maps; all source images and masks
    remain validated inputs.
    """
    raw_lines = configuration_path.read_text(encoding="utf-8").splitlines()
    if len(raw_lines) % 2:
        raise RuntimeError("invalid_patch_match_configuration_pair_count")
    pairs = [(raw_lines[index], raw_lines[index + 1]) for index in range(0, len(raw_lines), 2)]
    if any(not image_name or not source_rule for image_name, source_rule in pairs):
        raise RuntimeError("invalid_patch_match_configuration_empty_record")

    total = len(pairs)
    limit = int(maximum_reference_images or 0)
    if limit <= 0 or total <= limit:
        selected_pairs = pairs
        policy = "all-registered-references-v1"
    else:
        streams: dict[str, list[tuple[int, tuple[str, str]]]] = {}
        for original_index, pair in enumerate(pairs):
            stream = Path(pair[0]).parent.as_posix()
            streams.setdefault(stream, []).append((original_index, pair))
        if limit < len(streams):
            raise RuntimeError(
                f"dense_reference_limit_below_camera_stream_count:{limit}:{len(streams)}"
            )

        exact_quotas = {
            stream: limit * len(records) / total for stream, records in streams.items()
        }
        quotas = {
            stream: max(1, min(len(streams[stream]), math.floor(exact)))
            for stream, exact in exact_quotas.items()
        }
        remaining = limit - sum(quotas.values())
        order = sorted(
            streams,
            key=lambda stream: (
                -(exact_quotas[stream] - math.floor(exact_quotas[stream])),
                stream,
            ),
        )
        while remaining > 0:
            progressed = False
            for stream in order:
                if quotas[stream] >= len(streams[stream]):
                    continue
                quotas[stream] += 1
                remaining -= 1
                progressed = True
                if remaining == 0:
                    break
            if not progressed:
                break

        selected: list[tuple[int, tuple[str, str]]] = []
        per_stream: dict[str, int] = {}
        for stream in sorted(streams):
            records = streams[stream]
            indices = _uniform_indices(len(records), quotas[stream])
            per_stream[stream] = len(indices)
            selected.extend(records[index] for index in indices)
        selected_pairs = [pair for _, pair in sorted(selected)]
        policy = "uniform-per-camera-stream-v1"

    normalized = "".join(f"{image_name}\n{source_rule}\n" for image_name, source_rule in selected_pairs)
    configuration_path.write_text(normalized, encoding="utf-8")
    selected_names = [image_name for image_name, _ in selected_pairs]
    return {
        "policy": policy,
        "registered_reference_count": total,
        "selected_reference_count": len(selected_pairs),
        "selected_ratio": len(selected_pairs) / total if total else 0.0,
        "selected_names_sha256": hashlib.sha256(
            "\n".join(selected_names).encode("utf-8")
        ).hexdigest(),
        "camera_stream_count": len({Path(name).parent.as_posix() for name in selected_names}),
        "maximum_reference_images": limit or None,
    }


def write_json(path: Path, content: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the non-destructive COLMAP dense geometry PoC."
    )
    parser.add_argument("--repo_root", type=Path, required=True)
    parser.add_argument(
        "--input_model",
        type=Path,
        required=True,
        help="Horizontalized COLMAP TXT model. It is converted into this PoC workspace only.",
    )
    parser.add_argument("--image_path", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--world_alignment", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--mask_path", type=Path)
    parser.add_argument(
        "--asset_rotation_quaternion_xyzw",
        default="1,0,0,0",
        help="Matches Spark's LichtFeld SPZ decode rotation.",
    )
    parser.add_argument("--docker_image", default=PINNED_COLMAP_IMAGE)
    parser.add_argument("--gpu", default="all")
    parser.add_argument(
        "--prepare_only",
        action="store_true",
        help="Validate inputs and prepare the normalized model without starting MVS.",
    )
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    input_model = args.input_model.resolve()
    image_path = args.image_path.resolve()
    workspace = args.workspace.resolve()
    world_alignment = args.world_alignment.resolve()
    config_path = args.config.resolve()
    mask_path = args.mask_path.resolve() if args.mask_path else None
    report_path = workspace / "dense_quality.json"
    run_manifest_path = workspace / "dense_run_manifest.json"

    for path in (input_model, image_path, world_alignment, config_path):
        if not path.exists():
            raise RuntimeError(f"Required input does not exist: {path}")
    if mask_path and not mask_path.is_dir():
        raise RuntimeError(f"Mask path is not a directory: {mask_path}")
    if workspace.exists():
        raise RuntimeError(
            f"Refusing to overwrite existing workspace: {workspace}. Choose a new path."
        )

    config = json.loads(config_path.read_text(encoding="utf-8"))
    dense_config = config["dense_poc"]
    masks = validate_masks(image_path, mask_path)
    commands: list[dict[str, Any]] = []
    manifest: dict[str, Any] = {
        "schema_version": "1.0.0",
        "stage": "automatic_dense_geometry_poc",
        "backend": "colmap_dense",
        "status": "running",
        "started_at": utc_now(),
        "source_reconstruction": str(input_model),
        "input_image_path": str(image_path),
        "world_alignment": str(world_alignment),
        "asset_rotation_quaternion_xyzw": [
            float(value) for value in args.asset_rotation_quaternion_xyzw.split(",")
        ],
        "masks": masks,
        "commands": commands,
        "outputs": {
            "fused_pointcloud": "fused.ply",
            "quality_report": "dense_quality.json",
        },
    }
    workspace.mkdir(parents=True)
    write_json(run_manifest_path, manifest)

    if masks["configured"] and masks["missing_count"]:
        manifest["status"] = "unverified"
        manifest["failure_reasons"] = ["mask_mapping_incomplete"]
        manifest["completed_at"] = utc_now()
        write_json(run_manifest_path, manifest)
        write_json(
            report_path,
            {
                "schema_version": "1.0.0",
                "stage": "automatic_dense_geometry_poc",
                "status": "unverified",
                "dense_point_count": 0,
                "input_image_count": masks["image_count"],
                "failure_reasons": ["mask_mapping_incomplete"],
                "warnings": ["COLMAP was not executed because masks do not map to all input images."],
            },
        )
        raise RuntimeError("Mask mapping is incomplete; no dense geometry was generated.")

    def colmap_command(*arguments: str) -> list[str]:
        return [
            "docker",
            "run",
            "--rm",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--gpus",
            args.gpu,
            *docker_workspace_arguments(repo_root),
            "--entrypoint",
            "colmap",
            args.docker_image,
            *arguments,
        ]

    aligned_sparse = workspace / "aligned_sparse"
    normalized_model = workspace / "normalized_model"
    dense_workspace = workspace / "dense"
    fused_pointcloud = workspace / "fused.ply"
    input_image_count = len([path for path in image_path.rglob("*") if path.is_file()])
    aligned_sparse.mkdir()

    try:
        manifest["image_name_normalization"] = normalize_model_image_names(
            input_model, image_path, normalized_model
        )
        if args.prepare_only:
            manifest["status"] = "prepared"
            manifest["completed_at"] = utc_now()
            write_json(run_manifest_path, manifest)
            print(json.dumps(manifest, indent=2))
            return
        write_json(run_manifest_path, manifest)
        run_colmap(
            colmap_command(
                "model_converter",
                "--input_path",
                container_path(normalized_model, repo_root),
                "--output_path",
                container_path(aligned_sparse, repo_root),
                "--output_type",
                "BIN",
            ),
            "model_converter",
            commands,
        )
        run_colmap(
            colmap_command(
                "image_undistorter",
                "--image_path",
                container_path(image_path, repo_root),
                "--input_path",
                container_path(aligned_sparse, repo_root),
                "--output_path",
                container_path(dense_workspace, repo_root),
                "--output_type",
                "COLMAP",
                "--copy_policy",
                "SOFT_LINK",
                "--max_image_size",
                str(dense_config["max_image_size"]),
            ),
            "image_undistorter",
            commands,
        )
        reference_selection = select_patch_match_references(
            dense_workspace / "stereo" / "patch-match.cfg",
            dense_config.get("maximum_reference_images"),
        )
        manifest["reference_selection"] = reference_selection
        write_json(run_manifest_path, manifest)
        run_colmap(
            colmap_command(
                "patch_match_stereo",
                "--workspace_path",
                container_path(dense_workspace, repo_root),
                "--workspace_format",
                "COLMAP",
                "--PatchMatchStereo.gpu_index",
                str(dense_config["gpu_index"]),
                "--PatchMatchStereo.max_image_size",
                str(dense_config["max_image_size"]),
                "--PatchMatchStereo.geom_consistency",
                "1" if dense_config["geom_consistency"] else "0",
            ),
            "patch_match_stereo",
            commands,
        )
        fusion_arguments = [
            "stereo_fusion",
            "--workspace_path",
            container_path(dense_workspace, repo_root),
            "--workspace_format",
            "COLMAP",
            "--input_type",
            str(dense_config["stereo_fusion_input_type"]),
            "--output_path",
            container_path(fused_pointcloud, repo_root),
        ]
        if mask_path:
            fusion_arguments.extend(
                ["--StereoFusion.mask_path", container_path(mask_path, repo_root)]
            )
        run_colmap(colmap_command(*fusion_arguments), "stereo_fusion", commands)

        quality = evaluate_dense_geometry(
            fused_pointcloud,
            world_alignment,
            config,
            dense_workspace,
            int(reference_selection["selected_reference_count"]),
            args.asset_rotation_quaternion_xyzw,
            total_registered_image_count=input_image_count,
            reference_selection=reference_selection,
        )
        write_json(report_path, quality)
        manifest["status"] = quality["status"]
        manifest["quality_report"] = str(report_path)
        manifest["completed_at"] = utc_now()
        write_json(run_manifest_path, manifest)
        print(json.dumps(quality, indent=2))
    except Exception as error:
        manifest["status"] = "failed"
        manifest["failure_reasons"] = [str(error)]
        manifest["completed_at"] = utc_now()
        write_json(run_manifest_path, manifest)
        if not report_path.exists():
            write_json(
                report_path,
                {
                    "schema_version": "1.0.0",
                    "stage": "automatic_dense_geometry_poc",
                    "status": "failed",
                    "dense_point_count": 0,
                    "input_image_count": input_image_count,
                    "failure_reasons": [str(error)],
                    "warnings": [],
                },
            )
        raise


if __name__ == "__main__":
    main()
