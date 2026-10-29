from __future__ import annotations

import argparse
import itertools
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

if __package__:
    from .colmap_text_model import (
        ColmapCamera,
        ColmapImage,
        camera_intrinsics_matrix,
        load_colmap_text_model,
    )
else:
    from colmap_text_model import (
        ColmapCamera,
        ColmapImage,
        camera_intrinsics_matrix,
        load_colmap_text_model,
    )


def load_detections(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(f"detections.json not found: {path}")

    return json.loads(path.read_text(encoding="utf-8"))


def pixel_to_world_ray(
    xy_px: list[float],
    camera: ColmapCamera,
    image: ColmapImage,
) -> tuple[np.ndarray, np.ndarray]:
    k = camera_intrinsics_matrix(camera)
    k_inv = np.linalg.inv(k)

    pixel_h = np.array([xy_px[0], xy_px[1], 1.0], dtype=float)
    ray_camera = k_inv @ pixel_h
    ray_camera /= np.linalg.norm(ray_camera)

    r_cw = image.rotation_world_to_camera
    ray_world = r_cw.T @ ray_camera
    ray_world /= np.linalg.norm(ray_world)

    origin_world = image.camera_center_world

    return origin_world, ray_world


def triangulate_rays(
    rays: list[tuple[np.ndarray, np.ndarray]]
) -> tuple[np.ndarray, float, list[float]]:
    if len(rays) < 2:
        raise RuntimeError("At least two rays are required for triangulation.")

    a = np.zeros((3, 3), dtype=float)
    b = np.zeros(3, dtype=float)
    identity = np.eye(3)

    for origin, direction in rays:
        direction = direction / np.linalg.norm(direction)
        projection = identity - np.outer(direction, direction)
        a += projection
        b += projection @ origin

    point = np.linalg.solve(a, b)

    residuals = []
    for origin, direction in rays:
        v = point - origin
        cross = np.cross(v, direction)
        residuals.append(float(np.linalg.norm(cross)))

    mean_ray_distance = float(np.mean(residuals)) if residuals else 0.0

    return point, mean_ray_distance, residuals


def robust_residual_threshold(residuals: np.ndarray) -> float:
    """Return a scale-adaptive inlier threshold from a residual sample.

    The marker size is deliberately not used here: triangulation happens in
    arbitrary COLMAP units, while the later metric-quality gate is scale
    independent.  Median absolute deviation keeps a small number of false
    AprilTag detections from moving the least-squares intersection.
    """

    median = float(np.median(residuals))
    mad = float(np.median(np.abs(residuals - median)))
    robust_sigma = 1.4826 * mad
    # A perfect synthetic observation set has MAD == 0.  Keep its threshold
    # finite rather than accepting every ray due to a zero comparison.
    return max(median + 3.5 * robust_sigma, 1e-9)


def pair_hypothesis_indices(count: int, maximum: int = 4096) -> list[tuple[int, int]]:
    """Select deterministic ray pairs without quadratic blow-up on long videos."""

    pairs = list(itertools.combinations(range(count), 2))
    if len(pairs) <= maximum:
        return pairs

    positions = np.linspace(0, len(pairs) - 1, maximum, dtype=int)
    return [pairs[int(position)] for position in np.unique(positions)]


def robust_triangulate_rays(
    rays: list[tuple[np.ndarray, np.ndarray]],
) -> tuple[np.ndarray, float, list[float], list[bool], float]:
    """Triangulate with deterministic pair hypotheses and MAD inlier pruning.

    A fixed AprilTag can be detected spuriously in a few virtual-rig views.
    Ordinary least squares gives every ray equal leverage, which lets those
    false positives corrupt all four corners.  This estimator selects the
    largest geometrically consistent ray set, refines it once, and records
    the rejected observations for the independent quality gate.
    """

    if len(rays) < 2:
        raise RuntimeError("At least two rays are required for triangulation.")

    origins = np.asarray([origin for origin, _ in rays], dtype=float)
    directions = np.asarray([direction for _, direction in rays], dtype=float)

    def residuals_for(point: np.ndarray) -> np.ndarray:
        return np.linalg.norm(
            np.cross(point - origins, directions), axis=1
        )

    best: tuple[int, float, float, np.ndarray, np.ndarray] | None = None
    for first, second in pair_hypothesis_indices(len(rays)):
        try:
            point, _, _ = triangulate_rays([rays[first], rays[second]])
        except np.linalg.LinAlgError:
            continue
        residuals = residuals_for(point)
        threshold = robust_residual_threshold(residuals)
        inliers = residuals <= threshold
        inlier_residuals = residuals[inliers]
        score = (
            int(inliers.sum()),
            -float(np.median(inlier_residuals)),
            -float(np.mean(inlier_residuals)),
        )
        if best is None or score > best[:3]:
            best = (*score, point, inliers)

    if best is None or int(best[4].sum()) < 2:
        raise RuntimeError("robust_triangulation_no_non_degenerate_hypothesis")

    inliers = best[4]
    point, _, _ = triangulate_rays(
        [ray for ray, keep in zip(rays, inliers, strict=True) if keep]
    )
    residuals = residuals_for(point)
    threshold = robust_residual_threshold(residuals[inliers])
    inliers = residuals <= threshold
    if int(inliers.sum()) < 2:
        raise RuntimeError("robust_triangulation_insufficient_inliers")
    point, mean_ray_distance, _ = triangulate_rays(
        [ray for ray, keep in zip(rays, inliers, strict=True) if keep]
    )
    final_residuals = residuals_for(point).astype(float).tolist()
    return point, mean_ray_distance, final_residuals, inliers.tolist(), threshold


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--detections", type=Path, required=True)
    parser.add_argument("--colmap_model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--min_observations", type=int, default=2)
    args = parser.parse_args()

    detection_data = load_detections(args.detections)
    cameras, images = load_colmap_text_model(args.colmap_model)

    observations: dict[tuple[str, int, int], list[dict[str, Any]]] = defaultdict(list)

    skipped_unmatched_images = 0
    detection_count = 0

    for image_record in detection_data.get("images", []):
        image_name = image_record["image_name"]
        colmap_image = images.get(image_name)

        if colmap_image is None:
            skipped_unmatched_images += 1
            continue

        camera = cameras[colmap_image.camera_id]

        for detection in image_record.get("detections", []):
            detection_count += 1
            family = detection["family"]
            tag_id = int(detection["id"])

            for corner_index, corner_px in enumerate(detection["corners_px"]):
                origin, direction = pixel_to_world_ray(
                    corner_px,
                    camera,
                    colmap_image,
                )

                observations[(family, tag_id, corner_index)].append(
                    {
                        "image_name": image_name,
                        "corner_px": corner_px,
                        "origin_world": origin,
                        "direction_world": direction,
                    }
                )

    triangulated_corners = []
    insufficient_observation_count = 0

    for (family, tag_id, corner_index), corner_observations in sorted(observations.items()):
        if len(corner_observations) < args.min_observations:
            insufficient_observation_count += 1
            continue

        rays = [
            (obs["origin_world"], obs["direction_world"])
            for obs in corner_observations
        ]

        (
            point,
            mean_ray_distance,
            ray_distances,
            inlier_mask,
            inlier_threshold,
        ) = robust_triangulate_rays(rays)
        inlier_observations = [
            observation
            for observation, is_inlier in zip(
                corner_observations, inlier_mask, strict=True
            )
            if is_inlier
        ]
        raw_mean_ray_distance = float(np.mean(ray_distances))

        triangulated_corners.append(
            {
                "family": family,
                "id": tag_id,
                "corner_index": corner_index,
                "xyz_colmap": [
                    float(point[0]),
                    float(point[1]),
                    float(point[2]),
                ],
                "observation_count": len(inlier_observations),
                "raw_observation_count": len(corner_observations),
                "inlier_observation_count": len(inlier_observations),
                "rejected_observation_count": len(corner_observations)
                - len(inlier_observations),
                "mean_ray_distance_colmap": mean_ray_distance,
                "raw_mean_ray_distance_colmap": raw_mean_ray_distance,
                "robust_method": "deterministic_pair_mad",
                "inlier_threshold_colmap": inlier_threshold,
                "observed_in_images": [
                    obs["image_name"] for obs in inlier_observations
                ],
                "rejected_observed_in_images": [
                    observation["image_name"]
                    for observation, is_inlier in zip(
                        corner_observations, inlier_mask, strict=True
                    )
                    if not is_inlier
                ],
                "ray_distance_colmap_per_observation": [
                    {
                        "image_name": obs["image_name"],
                        "distance_colmap": distance,
                        "inlier": is_inlier,
                    }
                    for obs, distance, is_inlier in zip(
                        corner_observations,
                        ray_distances,
                        inlier_mask,
                        strict=True,
                    )
                ],
            }
        )

    output_data = {
        "schema_version": "1.0.0",
        "detections": str(args.detections),
        "colmap_model": str(args.colmap_model),
        "min_observations": args.min_observations,
        "detection_count": detection_count,
        "corner_track_count": len(observations),
        "triangulated_corner_count": len(triangulated_corners),
        "insufficient_observation_count": insufficient_observation_count,
        "skipped_unmatched_images": skipped_unmatched_images,
        "triangulated_corners": triangulated_corners,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"Detections: {detection_count}")
    print(f"Corner tracks: {len(observations)}")
    print(f"Triangulated corners: {len(triangulated_corners)}")
    print(f"Insufficient observations: {insufficient_observation_count}")
    print(f"Skipped unmatched images: {skipped_unmatched_images}")
    print(f"Output: {args.output}")


if __name__ == "__main__":
    main()
