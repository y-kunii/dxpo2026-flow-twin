from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

if __package__:
    from .colmap_text_model import load_colmap_text_model
else:
    from colmap_text_model import load_colmap_text_model


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corners_world", type=Path, required=True)
    parser.add_argument("--triangulated_corners", type=Path, required=True)
    parser.add_argument("--colmap_model", type=Path, required=True)
    parser.add_argument("--horizontal_alignment", type=Path, required=True)
    parser.add_argument("--world_alignment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    corner_data = json.loads(args.corners_world.read_text(encoding="utf-8"))
    points = np.asarray(
        [item["xyz_world_m"] for item in corner_data["corners_world_m"]], dtype=float
    )
    center = np.mean(points, axis=0)
    right = points[1] - points[0]
    right /= np.linalg.norm(right)
    down = points[3] - points[0]
    down /= np.linalg.norm(down)
    tag_up = -down
    normal = np.cross(right, down)
    normal /= np.linalg.norm(normal)

    triangulated = json.loads(args.triangulated_corners.read_text(encoding="utf-8"))
    observed_images = set()
    for corner in triangulated["triangulated_corners"]:
        if corner["family"] == corner_data["family"] and int(corner["id"]) == int(
            corner_data["id"]
        ):
            observed_images.update(corner["observed_in_images"])

    _, images = load_colmap_text_model(args.colmap_model)
    horizontal = np.loadtxt(args.horizontal_alignment, dtype=float)[:3, :3]
    aqx, aqy, aqz, aqw = corner_data["coordinate_stages"][
        "asset_rotation_quaternion_xyzw"
    ]
    asset_rotation = Rotation.from_quat([aqx, aqy, aqz, aqw]).as_matrix()
    world = json.loads(args.world_alignment.read_text(encoding="utf-8"))[
        "generated_to_world"
    ]
    scale = float(world["scale"])
    qx, qy, qz, qw = world["rotation_quaternion_xyzw"]
    world_rotation = Rotation.from_quat([qx, qy, qz, qw]).as_matrix()
    translation = np.asarray(world["translation_m"], dtype=float)
    observation_centers = []
    for name in sorted(observed_images):
        original = images[name].camera_center_world
        observation_centers.append(
            scale * (world_rotation @ (asset_rotation @ (horizontal @ original)))
            + translation
        )
    mean_observer = np.mean(observation_centers, axis=0)
    if np.dot(normal, mean_observer - center) < 0:
        normal = -normal

    face_position = center + normal * 0.65
    oblique_direction = normal + right
    oblique_direction /= np.linalg.norm(oblique_direction)
    oblique_position = center + oblique_direction * 0.65

    def values(vector: np.ndarray) -> list[float]:
        return [float(value) for value in vector]

    output = {
        "cameras": [
            {
                "id": "apriltag_front",
                "name": "AprilTag front",
                "position_m": values(face_position),
                "look_at_m": values(center),
                "up_world": values(tag_up),
                "vertical_fov_deg": 22.0,
                "resolution": [1600, 1000],
            },
            {
                "id": "apriltag_oblique_45",
                "name": "AprilTag oblique 45deg",
                "position_m": values(oblique_position),
                "look_at_m": values(center),
                "up_world": values(tag_up),
                "vertical_fov_deg": 22.0,
                "resolution": [1600, 1000],
            },
            {
                "id": "scene_overview",
                "name": "Scene overview",
                "position_m": [0.0, 2.0, 5.0],
                "look_at_m": [0.0, 0.0, 0.0],
                "up_world": [0.0, 1.0, 0.0],
                "vertical_fov_deg": 60.0,
                "resolution": [1600, 1000],
            },
        ],
        "apriltag_debug": {
            "center_world_m": values(center),
            "front_normal_world": values(normal),
            "right_world": values(right),
            "up_world": values(tag_up),
            "mean_observer_world_m": values(mean_observer),
            "source_observation_count": len(observation_centers),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
