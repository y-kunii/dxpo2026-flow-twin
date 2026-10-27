from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

if __package__:
    from .colmap_text_model import load_colmap_text_model
    from .render_virtual_rig import DEFAULT_CONFIG, virtual_rotations
else:
    from colmap_text_model import load_colmap_text_model
    from render_virtual_rig import DEFAULT_CONFIG, virtual_rotations


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--colmap_model", type=Path, required=True)
    parser.add_argument("--camera_prefix", default="pano_camera0/")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    _, images = load_colmap_text_model(args.colmap_model)
    selected = sorted(
        (image for name, image in images.items() if name.startswith(args.camera_prefix)),
        key=lambda image: image.name,
    )
    if len(selected) < 2:
        raise RuntimeError("At least two trajectory images are required.")

    steps = []
    for previous, current in zip(selected[:-1], selected[1:], strict=True):
        translation = float(
            np.linalg.norm(current.camera_center_world - previous.camera_center_world)
        )
        relative_rotation = (
            current.rotation_world_to_camera
            @ previous.rotation_world_to_camera.T
        )
        rotation_deg = float(
            np.rad2deg(Rotation.from_matrix(relative_rotation).magnitude())
        )
        steps.append(
            {
                "from": previous.name,
                "to": current.name,
                "translation_colmap": translation,
                "rotation_deg": rotation_deg,
            }
        )

    translations = np.asarray(
        [step["translation_colmap"] for step in steps], dtype=float
    )
    rotations = np.asarray([step["rotation_deg"] for step in steps], dtype=float)
    camera0_from_pano = virtual_rotations(
        [float(value) for value in DEFAULT_CONFIG["pitches_deg"]],
        int(DEFAULT_CONFIG["yaw_steps"]),
    )[0]
    up_vectors = []
    for image in selected:
        pano_from_world = camera0_from_pano.T @ image.rotation_world_to_camera
        up_world = pano_from_world.T @ np.array([0.0, -1.0, 0.0])
        up_vectors.append(up_world / np.linalg.norm(up_world))
    mean_up = np.sum(up_vectors, axis=0)
    mean_up /= np.linalg.norm(mean_up)
    up_deviations = np.asarray(
        [
            np.rad2deg(np.arccos(np.clip(np.dot(up, mean_up), -1.0, 1.0)))
            for up in up_vectors
        ],
        dtype=float,
    )
    output = {
        "camera_prefix": args.camera_prefix,
        "frame_count": len(selected),
        "camera_centers_world": [
            {
                "image_name": image.name,
                "xyz_colmap": image.camera_center_world.tolist(),
            }
            for image in selected
        ],
        "steps": steps,
        "translation": {
            "median": float(np.median(translations)),
            "max": float(np.max(translations)),
            "max_over_median": float(np.max(translations) / np.median(translations)),
        },
        "rotation_deg": {
            "median": float(np.median(rotations)),
            "max": float(np.max(rotations)),
        },
        "inferred_panorama_up": {
            "mean_xyz_world": mean_up.tolist(),
            "median_deviation_deg": float(np.median(up_deviations)),
            "max_deviation_deg": float(np.max(up_deviations)),
            "per_frame_deviation_deg": [float(value) for value in up_deviations],
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
