from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any

QUALITY_GATE_FIELDS = {
    "min_observation_count_per_corner",
    "max_side_cv",
    "max_diagonal_ratio",
    "max_normalized_ray_residual",
}


def quality_gate_from_scene_config(path: Path) -> dict[str, float] | None:
    """Return the single metric-quality policy declared by the scene config.

    A scale estimate must not be accepted merely because four corners exist:
    the policy is deliberately scene-config data rather than a second set of
    thresholds embedded in this diagnostic tool.
    """
    try:
        scene = json.loads(path.read_text(encoding="utf-8"))
        marker = scene.get("metric_marker", {})
        gate = marker.get("quality_gate") if isinstance(marker, dict) else None
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"metric_quality_config_unreadable:{path}") from error
    if not isinstance(gate, dict):
        return None
    if set(gate) != QUALITY_GATE_FIELDS:
        return None
    normalized: dict[str, float] = {}
    for name in sorted(QUALITY_GATE_FIELDS):
        try:
            value = float(gate[name])
        except (TypeError, ValueError) as error:
            raise RuntimeError(f"metric_quality_gate_invalid:{name}") from error
        if not math.isfinite(value) or value < 0:
            raise RuntimeError(f"metric_quality_gate_invalid:{name}")
        normalized[name] = value
    if normalized["min_observation_count_per_corner"] < 1:
        raise RuntimeError("metric_quality_gate_invalid:min_observation_count_per_corner")
    if normalized["max_diagonal_ratio"] < 1:
        raise RuntimeError("metric_quality_gate_invalid:max_diagonal_ratio")
    return normalized


def evaluate(
    data: dict[str, Any],
    family: str,
    tag_id: int,
    quality_gate: dict[str, float] | None = None,
) -> dict[str, Any]:
    triangulated = data.get("triangulated_corners", [])
    corners = {
        int(corner["corner_index"]): corner
        for corner in triangulated
        if corner["family"] == family and int(corner["id"]) == tag_id
    }
    available_markers = sorted({
        f"{corner['family']}/{int(corner['id'])}"
        for corner in triangulated
    })
    if not corners:
        available = ", ".join(available_markers) or "none"
        raise RuntimeError(
            f"requested_marker_not_triangulated:{family}/{tag_id};available={available}"
        )
    if set(corners) != {0, 1, 2, 3}:
        present = ",".join(str(index) for index in sorted(corners)) or "none"
        raise RuntimeError(
            f"requested_marker_corners_incomplete:{family}/{tag_id};present={present}"
        )

    points = [[float(value) for value in corners[index]["xyz_colmap"]] for index in range(4)]

    def distance(left: list[float], right: list[float]) -> float:
        return math.sqrt(sum((left[axis] - right[axis]) ** 2 for axis in range(3)))

    sides = [
        distance(points[index], points[(index + 1) % 4])
        for index in range(4)
    ]
    diagonals = [
        distance(points[0], points[2]),
        distance(points[1], points[3]),
    ]
    mean_side = float(statistics.mean(sides))
    mean_ray_distance = float(
        statistics.mean(
            float(corners[index]["mean_ray_distance_colmap"])
            for index in range(4)
        )
    )

    observations = [int(corners[index]["observation_count"]) for index in range(4)]
    side_cv = float(statistics.pstdev(sides) / mean_side) if mean_side > 0 else math.inf
    diagonal_ratio = float(max(diagonals) / min(diagonals)) if min(diagonals) > 0 else math.inf
    normalized_ray_residual = float(mean_ray_distance / mean_side) if mean_side > 0 else math.inf
    failures: list[str] = []
    warnings: list[str] = []
    if quality_gate is None:
        failures.append("metric_quality_gate_missing")
    else:
        values = [*sides, *diagonals, mean_side, mean_ray_distance, side_cv, diagonal_ratio, normalized_ray_residual]
        if not all(math.isfinite(value) for value in values):
            failures.append("non_finite_apriltag_triangulation_metric")
        if min(observations) < quality_gate["min_observation_count_per_corner"]:
            failures.append("apriltag_corner_observation_count_below_minimum")
        if side_cv > quality_gate["max_side_cv"]:
            failures.append("apriltag_side_cv_exceeds_maximum")
        if diagonal_ratio > quality_gate["max_diagonal_ratio"]:
            failures.append("apriltag_diagonal_ratio_exceeds_maximum")
        if normalized_ray_residual > quality_gate["max_normalized_ray_residual"]:
            failures.append("apriltag_normalized_ray_residual_exceeds_maximum")

    return {
        "schema_version": "1.0.0",
        "stage": "apriltag_triangulation_quality",
        "status": "verified" if not failures else "unverified",
        "family": family,
        "id": tag_id,
        "triangulated_corner_count": len(corners),
        "observation_counts": observations,
        "side_lengths_colmap": sides,
        "mean_side_colmap": mean_side,
        "side_cv": side_cv,
        "diagonals_colmap": diagonals,
        "diagonal_ratio": diagonal_ratio,
        "mean_ray_distance_colmap": mean_ray_distance,
        "normalized_ray_residual": normalized_ray_residual,
        "quality_gate": {
            "status": "verified" if not failures else "unverified",
            "thresholds": quality_gate,
        },
        "failure_reasons": failures,
        "warnings": warnings,
    }


def evaluate_markers(
    data: dict[str, Any],
    markers: list[dict[str, Any]],
    quality_gate: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Evaluate every configured fixed tag with one shared quality policy.

    A fixed tag is an anchor that may later be selected by the camera-side
    tracker.  Therefore accepting a scale from one tag while silently keeping
    an unverified second anchor would be unsafe.  All configured anchors must
    independently pass the same triangulation gate before the metric package
    is accepted.
    """
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for marker in markers:
        if not isinstance(marker, dict):
            raise RuntimeError("scale_marker_invalid")
        family = marker.get("family")
        tag_id = marker.get("id")
        size_m = marker.get("size_m")
        if not isinstance(family, str) or not family or not isinstance(tag_id, int) or tag_id < 0:
            raise RuntimeError("scale_marker_invalid")
        try:
            size = float(size_m)
        except (TypeError, ValueError) as error:
            raise RuntimeError("scale_marker_invalid") from error
        if not math.isfinite(size) or size <= 0:
            raise RuntimeError("scale_marker_invalid")
        key = (family, tag_id)
        if key in seen:
            raise RuntimeError(f"scale_marker_duplicate:{family}/{tag_id}")
        seen.add(key)
        normalized.append({"family": family, "id": tag_id, "size_m": size})
    if not normalized:
        raise RuntimeError("scale_markers_missing")

    reports = [
        evaluate(data, marker["family"], marker["id"], quality_gate)
        for marker in normalized
    ]
    failures = [
        f"{report['family']}/{report['id']}:" + str(reason)
        for report in reports
        for reason in report.get("failure_reasons", [])
    ]
    primary = reports[0]
    status = "verified" if not failures else "unverified"
    return {
        **primary,
        "status": status,
        "markers": normalized,
        "marker_reports": reports,
        "verified_marker_count": sum(report.get("status") == "verified" for report in reports),
        "quality": {
            "status": status,
            "marker_count": len(reports),
            "verified_marker_count": sum(report.get("status") == "verified" for report in reports),
        },
        "quality_gate": {
            "status": status,
            "thresholds": quality_gate,
        },
        "failure_reasons": failures,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--triangulated_corners", type=Path, required=True)
    parser.add_argument("--family", default="tag25h9")
    parser.add_argument("--id", type=int, default=0)
    parser.add_argument(
        "--scale_markers",
        type=Path,
        help="JSON file containing the configured fixed AprilTag markers.",
    )
    parser.add_argument("--scene_config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    data = json.loads(args.triangulated_corners.read_text(encoding="utf-8"))
    gate = quality_gate_from_scene_config(args.scene_config)
    if args.scale_markers is None:
        result = evaluate(data, args.family, args.id, gate)
    else:
        marker_document = json.loads(args.scale_markers.read_text(encoding="utf-8"))
        markers = marker_document.get("markers") if isinstance(marker_document, dict) else None
        if not isinstance(markers, list):
            raise RuntimeError("scale_markers_missing")
        result = evaluate_markers(data, markers, gate)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
