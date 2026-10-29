from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

import numpy as np


SIDE_CORNER_PAIRS = [(0, 1), (1, 2), (2, 3), (3, 0)]


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(f"JSON file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def marker_size_lookup(scale_markers: dict[str, Any]) -> dict[tuple[str, int], float]:
    lookup: dict[tuple[str, int], float] = {}

    for marker in scale_markers.get("markers", []):
        if marker.get("type") != "apriltag":
            continue

        family = str(marker["family"])
        tag_id = int(marker["id"])
        size_m = float(marker["size_m"])
        lookup[(family, tag_id)] = size_m

    return lookup


def group_corners(triangulated: dict[str, Any]) -> dict[tuple[str, int], dict[int, dict[str, Any]]]:
    grouped: dict[tuple[str, int], dict[int, dict[str, Any]]] = {}

    for corner in triangulated.get("triangulated_corners", []):
        family = str(corner["family"])
        tag_id = int(corner["id"])
        corner_index = int(corner["corner_index"])

        grouped.setdefault((family, tag_id), {})[corner_index] = corner

    return grouped


def estimate_tag_scale(
    family: str,
    tag_id: int,
    corners: dict[int, dict[str, Any]],
    size_m: float,
) -> dict[str, Any] | None:
    if not all(index in corners for index in range(4)):
        return None

    points = {
        index: np.array(corners[index]["xyz_colmap"], dtype=float)
        for index in range(4)
    }

    side_lengths = []

    for a, b in SIDE_CORNER_PAIRS:
        side_lengths.append(float(np.linalg.norm(points[a] - points[b])))

    mean_size_colmap = float(statistics.mean(side_lengths))

    if mean_size_colmap <= 0:
        return None

    scale = float(size_m / mean_size_colmap)

    return {
        "family": family,
        "id": tag_id,
        "size_m": size_m,
        "side_lengths_colmap": side_lengths,
        "mean_size_colmap": mean_size_colmap,
        "scale": scale,
        "observation_count_per_corner": {
            str(index): int(corners[index].get("observation_count", 0))
            for index in range(4)
        },
        "mean_ray_distance_colmap_per_corner": {
            str(index): float(corners[index].get("mean_ray_distance_colmap", 0.0))
            for index in range(4)
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--triangulated_corners", type=Path, required=True)
    parser.add_argument("--scale_markers", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    triangulated = load_json(args.triangulated_corners)
    scale_markers = load_json(args.scale_markers)

    sizes = marker_size_lookup(scale_markers)
    grouped = group_corners(triangulated)

    tag_estimates = []
    missing_marker_definitions = 0
    incomplete_tags = 0

    for (family, tag_id), corners in sorted(grouped.items()):
        size_m = sizes.get((family, tag_id))

        if size_m is None:
            missing_marker_definitions += 1
            continue

        estimate = estimate_tag_scale(family, tag_id, corners, size_m)

        if estimate is None:
            incomplete_tags += 1
            continue

        tag_estimates.append(estimate)

    scales = [float(item["scale"]) for item in tag_estimates]

    if scales:
        scale = float(statistics.median(scales))
        status = "verified"
        method = "apriltag_triangulated_corners"
        notes = "Scale estimated from triangulated AprilTag corners."
    else:
        scale = 1.0
        status = "unverified"
        method = "apriltag_pending"
        notes = "No valid triangulated AprilTag was available. Do not use measurements as real-world distances."

    output_data = {
        "unit": "meter",
        "status": status,
        "method": method,
        "generated_to_world": {
            "scale": scale,
            "rotation_quaternion_xyzw": [0, 0, 0, 1],
            "translation_m": [0, 0, 0],
        },
        "quality": {
            "tag_count": len(tag_estimates),
            "scale_values": scales,
            "scale_median": scale if scales else None,
            "missing_marker_definitions": missing_marker_definitions,
            "incomplete_tags": incomplete_tags,
            "triangulated_corner_count": int(triangulated.get("triangulated_corner_count", 0)),
        },
        "tag_estimates": tag_estimates,
        "notes": notes,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"Status: {status}")
    print(f"Valid tags: {len(tag_estimates)}")
    print(f"Scale: {scale}")
    print(f"Output: {args.output}")


if __name__ == "__main__":
    main()
