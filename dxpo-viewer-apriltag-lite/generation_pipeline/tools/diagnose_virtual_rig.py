from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

if __package__:
    from .render_virtual_rig import load_config, rig_config, virtual_rotations
else:
    from render_virtual_rig import load_config, rig_config, virtual_rotations


def rotation_error_deg(actual: np.ndarray, expected: np.ndarray) -> float:
    delta = actual @ expected.T
    return float(np.rad2deg(Rotation.from_matrix(delta).magnitude()))


def load_camera_ids_by_prefix(images_path: Path) -> dict[str, int]:
    camera_ids: dict[str, int] = {}
    lines = images_path.read_text(encoding="utf-8").splitlines()

    for line in lines:
        fields = line.split()
        if len(fields) < 10 or not fields[9].lower().endswith(
            (".jpg", ".jpeg", ".png")
        ):
            continue
        camera_id = int(fields[8])
        prefix = str(Path(fields[9]).parent) + "/"
        previous = camera_ids.setdefault(prefix, camera_id)
        if previous != camera_id:
            raise RuntimeError(f"Multiple camera IDs found for {prefix}")

    return camera_ids


def load_rig_rotations(rigs_path: Path) -> tuple[int, dict[int, np.ndarray]]:
    data_line = next(
        line.strip()
        for line in rigs_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    )
    fields = data_line.split()
    sensor_count = int(fields[1])
    reference_camera_id = int(fields[3])
    rotations = {reference_camera_id: np.eye(3)}
    offset = 4

    for _ in range(sensor_count - 1):
        if fields[offset] != "CAMERA":
            raise RuntimeError("Only camera rig sensors are supported.")
        camera_id = int(fields[offset + 1])
        has_pose = int(fields[offset + 2])
        if has_pose != 1:
            raise RuntimeError(f"Camera {camera_id} has no cam_from_rig pose.")
        qw, qx, qy, qz = map(float, fields[offset + 3 : offset + 7])
        rotations[camera_id] = Rotation.from_quat([qx, qy, qz, qw]).as_matrix()
        offset += 10

    return reference_camera_id, rotations


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare renderer-derived cam_from_rig rotations with JSON and COLMAP."
    )
    parser.add_argument("--rig_config", type=Path, required=True)
    parser.add_argument("--colmap_model", type=Path, required=True)
    parser.add_argument("--render_config", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    render_config = load_config(args.render_config)
    rotations = virtual_rotations(
        [float(value) for value in render_config["pitches_deg"]],
        int(render_config["yaw_steps"]),
    )
    expected = rig_config(rotations, "SIMPLE_PINHOLE", [1.0, 0.0, 0.0])[0][
        "cameras"
    ]
    configured = json.loads(args.rig_config.read_text(encoding="utf-8"))[0][
        "cameras"
    ]
    configured_by_prefix = {camera["image_prefix"]: camera for camera in configured}

    camera_ids = load_camera_ids_by_prefix(args.colmap_model / "images.txt")
    reference_camera_id, model_rotations = load_rig_rotations(
        args.colmap_model / "rigs.txt"
    )

    comparisons = []
    for camera in expected:
        prefix = camera["image_prefix"]
        camera_id = camera_ids[prefix]
        expected_q = camera.get("cam_from_rig_rotation", [1.0, 0.0, 0.0, 0.0])
        configured_q = configured_by_prefix[prefix].get(
            "cam_from_rig_rotation", [1.0, 0.0, 0.0, 0.0]
        )
        expected_rotation = Rotation.from_quat(
            [expected_q[1], expected_q[2], expected_q[3], expected_q[0]]
        ).as_matrix()
        configured_rotation = Rotation.from_quat(
            [configured_q[1], configured_q[2], configured_q[3], configured_q[0]]
        ).as_matrix()
        comparisons.append(
            {
                "image_prefix": prefix,
                "camera_id": camera_id,
                "renderer_to_config_error_deg": rotation_error_deg(
                    configured_rotation, expected_rotation
                ),
                "renderer_to_model_error_deg": rotation_error_deg(
                    model_rotations[camera_id], expected_rotation
                ),
            }
        )

    if camera_ids["pano_camera0/"] != reference_camera_id:
        raise RuntimeError("COLMAP reference sensor does not match pano_camera0.")

    output = {
        "convention": {
            "renderer_row_vector": "ray_pano_row = ray_camera_row @ R_camera_from_pano",
            "renderer_column_vector": "ray_pano = R_camera_from_pano.T @ ray_camera",
            "colmap_composition": "cam_i_from_world = cam_i_from_rig @ rig_from_world",
            "derived_rotation": "cam_i_from_rig = R_i @ R_ref.T",
        },
        "reference_camera_id": reference_camera_id,
        "comparisons": comparisons,
        "max_renderer_to_config_error_deg": max(
            item["renderer_to_config_error_deg"] for item in comparisons
        ),
        "max_renderer_to_model_error_deg": max(
            item["renderer_to_model_error_deg"] for item in comparisons
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
