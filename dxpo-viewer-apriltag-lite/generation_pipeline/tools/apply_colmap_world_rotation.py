from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input_model", type=Path, required=True)
    parser.add_argument("--rotation", type=Path, required=True)
    parser.add_argument("--output_model", type=Path, required=True)
    args = parser.parse_args()

    transform = np.loadtxt(args.rotation, dtype=float)
    if transform.shape not in {(3, 3), (4, 4)}:
        raise RuntimeError("Rotation must be a 3x3 or 4x4 matrix.")
    world_rotation = transform[:3, :3]
    if not np.allclose(world_rotation @ world_rotation.T, np.eye(3), atol=1e-9):
        raise RuntimeError("Rotation matrix is not orthonormal.")

    args.output_model.mkdir(parents=True, exist_ok=True)
    shutil.copy2(args.input_model / "cameras.txt", args.output_model / "cameras.txt")

    image_output = []
    for line in (args.input_model / "images.txt").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) >= 10 and fields[9].lower().endswith((".jpg", ".jpeg", ".png")):
            qw, qx, qy, qz = map(float, fields[1:5])
            camera_from_world = Rotation.from_quat([qx, qy, qz, qw]).as_matrix()
            camera_from_rotated_world = camera_from_world @ world_rotation.T
            x, y, z, w = Rotation.from_matrix(camera_from_rotated_world).as_quat()
            fields[1:5] = [
                f"{w:.17g}",
                f"{x:.17g}",
                f"{y:.17g}",
                f"{z:.17g}",
            ]
            line = " ".join(fields)
        image_output.append(line)
    (args.output_model / "images.txt").write_text(
        "\n".join(image_output) + "\n", encoding="utf-8"
    )

    point_output = []
    for line in (args.input_model / "points3D.txt").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if fields and not line.startswith("#"):
            xyz = world_rotation @ np.asarray(fields[1:4], dtype=float)
            fields[1:4] = [f"{value:.17g}" for value in xyz]
            line = " ".join(fields)
        point_output.append(line)
    (args.output_model / "points3D.txt").write_text(
        "\n".join(point_output) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
