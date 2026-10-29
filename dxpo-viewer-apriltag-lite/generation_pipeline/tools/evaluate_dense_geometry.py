from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from typing import Any

if __package__:
    from .dense_geometry_common import evaluate, iter_ply_points
else:
    from dense_geometry_common import evaluate, iter_ply_points


def count_map_files(workspace: Path, name: str, map_kind: str) -> int:
    # COLMAP keeps one directory per camera stream beneath each map type.
    return len(list((workspace / "stereo" / name).rglob(f"*.{map_kind}.bin")))


def count_non_finite(pointcloud: Path) -> tuple[int, int]:
    nan_count = 0
    inf_count = 0
    for points in iter_ply_points(pointcloud):
        for point in points:
            if any(math.isnan(value) for value in point):
                nan_count += 1
            elif any(math.isinf(value) for value in point):
                inf_count += 1
    return nan_count, inf_count


def evaluate_dense_geometry(
    fused_ply: Path,
    world_alignment: Path,
    config: dict[str, Any],
    dense_workspace: Path,
    source_image_count: int,
    asset_rotation: str,
    *,
    total_registered_image_count: int | None = None,
    reference_selection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = evaluate(
        fused_ply, world_alignment, config, source_image_count, asset_rotation
    )
    fusion_input_type = str(
        config.get("dense_poc", {}).get("stereo_fusion_input_type", "geometric")
    )
    if fusion_input_type not in {"geometric", "photometric"}:
        raise ValueError(f"unsupported_stereo_fusion_input_type:{fusion_input_type}")
    depth_map_count = count_map_files(dense_workspace, "depth_maps", fusion_input_type)
    normal_map_count = count_map_files(dense_workspace, "normal_maps", fusion_input_type)
    nan_count, inf_count = count_non_finite(fused_ply)
    dimensions = result["world_bounds_m"]["extent"]
    volume = math.prod(dimensions) if dimensions and all(value > 0 for value in dimensions) else None
    density = result["finite_dense_point_count"] / volume if volume else None
    coverage = depth_map_count / source_image_count if source_image_count else 0.0
    gate = config["quality_gate"]
    failures = list(result["failure_reasons"])
    if coverage < float(gate["minimum_coverage_ratio"]):
        failures.append("insufficient_camera_trajectory_coverage")
    finite_count = result["finite_dense_point_count"]
    outlier_ratio = result["outlier_count"] / finite_count if finite_count else 1.0
    if outlier_ratio > float(gate["maximum_outlier_ratio"]):
        failures.append("excessive_outliers")

    transform = json.loads(world_alignment.read_text(encoding="utf-8"))
    asset_quaternion = [float(value) for value in asset_rotation.split(",")]
    transform_consistent = (
        transform.get("unit") == "meter"
        and transform.get("status") == "verified"
        and asset_quaternion == [1.0, 0.0, 0.0, 0.0]
    )
    if not transform_consistent:
        failures.append("world_transform_not_metric_scene_compatible")

    warnings = list(result.get("warnings", []))
    warnings.append(
        "fusion_used_image_count equals successfully generated geometric depth maps; COLMAP fusion does not emit a separate accepted-image manifest."
    )
    if depth_map_count != source_image_count:
        warnings.append(
            f"depth_map_count {depth_map_count} differs from source_image_count {source_image_count}; coverage is reported explicitly."
        )
    if normal_map_count != depth_map_count:
        warnings.append(
            f"normal_map_count {normal_map_count} differs from depth_map_count {depth_map_count}."
        )
    return {
        "schema_version": "1.0.0",
        "stage": "automatic_dense_geometry",
        "status": "verified" if not failures else "unverified",
        "dense_point_count": result["dense_point_count"],
        "finite_point_count": finite_count,
        "nan_point_count": nan_count,
        "inf_point_count": inf_count,
        "outlier_point_count": result["outlier_count"],
        "outlier_method": result["outlier_method"],
        "source_image_count": source_image_count,
        "total_registered_image_count": total_registered_image_count or source_image_count,
        "reference_selection": reference_selection or {
            "policy": "all-registered-references-v1",
            "registered_reference_count": source_image_count,
            "selected_reference_count": source_image_count,
            "selected_ratio": 1.0,
        },
        "stereo_fusion_input_type": fusion_input_type,
        "depth_map_count": depth_map_count,
        "normal_map_count": normal_map_count,
        "fusion_candidate_image_count": depth_map_count,
        "fusion_used_image_count": depth_map_count,
        "aligned_bounding_box": result["generated_bounds"],
        "world_bounding_box": result["world_bounds_m"],
        "world_dimensions_m": dimensions,
        "point_density_per_m3": density,
        "camera_trajectory_coverage": {
            "ratio": coverage,
            "method": "depth_map_count / source_image_count",
        },
        "fusion_quality": result["fusion_quality"],
        "transform_consistency": {
            "matches_metric_scene": transform_consistent,
            "world_alignment": str(world_alignment),
            "asset_rotation_quaternion_xyzw": asset_quaternion,
            "formula": "world = scale * world_rotation * asset_rotation * dense_aligned + translation",
        },
        "failure_reasons": sorted(set(failures)),
        "warnings": warnings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate a fused COLMAP dense cloud.")
    parser.add_argument("--fused_ply", type=Path, required=True)
    parser.add_argument("--world_alignment", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--dense_workspace", type=Path, required=True)
    parser.add_argument("--source_image_count", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--asset_rotation_quaternion_xyzw", default="1,0,0,0")
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        result = evaluate_dense_geometry(
            args.fused_ply,
            args.world_alignment,
            config,
            args.dense_workspace,
            args.source_image_count,
            args.asset_rotation_quaternion_xyzw,
        )
    except Exception as error:
        result = {
            "schema_version": "1.0.0",
            "stage": "automatic_dense_geometry",
            "status": "failed",
            "failure_reasons": [str(error)],
            "warnings": [],
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, args.output)
    print(json.dumps(result, indent=2))
    if result["status"] == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
