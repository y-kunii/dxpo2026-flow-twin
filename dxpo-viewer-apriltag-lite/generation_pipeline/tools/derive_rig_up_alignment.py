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
    parser.add_argument(
        "--input_projection",
        choices=("equirectangular_360", "perspective"),
        default="equirectangular_360",
    )
    parser.add_argument("--camera_prefix", default="pano_camera0/")
    parser.add_argument("--transform", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    _, images = load_colmap_text_model(args.colmap_model)
    selected = sorted(
        (image for name, image in images.items() if name.startswith(args.camera_prefix)),
        key=lambda image: image.name,
    )
    if not selected:
        raise RuntimeError("No reference-camera images found.")

    camera_from_capture = (
        np.eye(3)
        if args.input_projection == "perspective"
        else virtual_rotations(
            [float(value) for value in DEFAULT_CONFIG["pitches_deg"]],
            int(DEFAULT_CONFIG["yaw_steps"]),
        )[0]
    )
    up_vectors = []
    for image in selected:
        capture_from_world = camera_from_capture.T @ image.rotation_world_to_camera
        up_world = capture_from_world.T @ np.array([0.0, -1.0, 0.0])
        up_vectors.append(up_world / np.linalg.norm(up_world))

    mean_up = np.sum(up_vectors, axis=0)
    mean_up /= np.linalg.norm(mean_up)

    # LichtFeld's ground grid is the XZ plane. Choose the closer Y direction so
    # horizontalization never introduces an unnecessary 180-degree flip.
    target_up = np.array([0.0, np.sign(mean_up[1]) or 1.0, 0.0])
    alignment, _ = Rotation.align_vectors([target_up], [mean_up])
    rotation = alignment.as_matrix()
    transformed_up = rotation @ mean_up

    matrix = np.eye(4)
    matrix[:3, :3] = rotation
    args.transform.parent.mkdir(parents=True, exist_ok=True)
    np.savetxt(args.transform, matrix, fmt="%.17g")

    angle_deg = float(
        np.rad2deg(np.arccos(np.clip(np.dot(mean_up, target_up), -1.0, 1.0)))
    )
    report = {
        "input_projection": args.input_projection,
        "camera_prefix": args.camera_prefix,
        "frame_count": len(selected),
        "mean_up_before": mean_up.tolist(),
        "target_up": target_up.tolist(),
        "mean_up_after": transformed_up.tolist(),
        "alignment_angle_deg": angle_deg,
        "transform_path": str(args.transform),
    }
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
