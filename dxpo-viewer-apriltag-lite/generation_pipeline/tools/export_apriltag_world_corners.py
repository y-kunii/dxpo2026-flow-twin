from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation


SIDE_PAIRS = [(0, 1), (1, 2), (2, 3), (3, 0)]
DIAGONAL_PAIRS = [(0, 2), (1, 3)]


def load_markers(path: Path) -> list[dict[str, object]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        raw_markers = value.get("markers") if isinstance(value, dict) else None
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("scale_markers_unreadable") from error
    if not isinstance(raw_markers, list) or not raw_markers:
        raise RuntimeError("scale_markers_missing")
    markers: list[dict[str, object]] = []
    seen: set[tuple[str, int]] = set()
    for raw in raw_markers:
        if not isinstance(raw, dict):
            raise RuntimeError("scale_marker_invalid")
        family = raw.get("family")
        tag_id = raw.get("id")
        try:
            size_m = float(raw.get("size_m"))
        except (TypeError, ValueError) as error:
            raise RuntimeError("scale_marker_invalid") from error
        if not isinstance(family, str) or not family or not isinstance(tag_id, int) or tag_id < 0 or not np.isfinite(size_m) or size_m <= 0:
            raise RuntimeError("scale_marker_invalid")
        key = (family, tag_id)
        if key in seen:
            raise RuntimeError(f"scale_marker_duplicate:{family}/{tag_id}")
        seen.add(key)
        markers.append({"family": family, "id": tag_id, "size_m": size_m})
    return markers


def tag_annotation(
    triangulated_corners: list[object],
    marker: dict[str, object],
    horizontal: np.ndarray,
    asset_rotation: np.ndarray,
    scale: float,
    world_rotation: np.ndarray,
    translation: np.ndarray,
) -> dict[str, object]:
    family = str(marker["family"])
    tag_id = int(marker["id"])
    corners = {
        int(item["corner_index"]): item
        for item in triangulated_corners
        if isinstance(item, dict) and item.get("family") == family and int(item.get("id", -1)) == tag_id
    }
    if set(corners) != {0, 1, 2, 3}:
        raise RuntimeError(f"apriltag_corners_incomplete:{family}/{tag_id}")

    records: list[dict[str, object]] = []
    world_points: dict[int, np.ndarray] = {}
    for index in range(4):
        corner = corners[index]
        original = np.asarray(corner["xyz_colmap"], dtype=float)
        aligned = horizontal @ original
        asset = asset_rotation @ aligned
        world = scale * (world_rotation @ asset) + translation
        world_points[index] = world
        records.append({
            "corner_index": index,
            "xyz_colmap": original.tolist(),
            "xyz_aligned": aligned.tolist(),
            "xyz_asset": asset.tolist(),
            "xyz_world_m": world.tolist(),
            "observation_count": int(corner["observation_count"]),
            "mean_ray_distance_colmap": float(corner["mean_ray_distance_colmap"]),
        })

    def measurements(pairs: list[tuple[int, int]]) -> list[dict[str, object]]:
        return [
            {
                "corners": [a, b],
                "distance_m": float(np.linalg.norm(world_points[a] - world_points[b])),
            }
            for a, b in pairs
        ]

    return {
        "family": family,
        "id": tag_id,
        "physical_side_m": float(marker["size_m"]),
        "corners_world_m": records,
        "sides": measurements(SIDE_PAIRS),
        "diagonals": measurements(DIAGONAL_PAIRS),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--triangulated_corners", type=Path, required=True)
    parser.add_argument("--horizontal_alignment", type=Path, required=True)
    parser.add_argument("--world_alignment", type=Path, required=True)
    parser.add_argument(
        "--asset_rotation_quaternion_xyzw",
        default="1,0,0,0",
        help="Spark SPZ decoder rotation from horizontalized LichtFeld coordinates into viewer asset coordinates (xyzw).",
    )
    parser.add_argument("--family", default="tag25h9")
    parser.add_argument("--id", type=int, default=0)
    parser.add_argument("--physical_side_m", type=float, default=0.082)
    parser.add_argument("--scale_markers", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    triangulated = json.loads(args.triangulated_corners.read_text(encoding="utf-8"))
    triangulated_corners = triangulated.get("triangulated_corners") if isinstance(triangulated, dict) else None
    if not isinstance(triangulated_corners, list):
        raise RuntimeError("triangulated_corners_missing")
    markers = (
        load_markers(args.scale_markers)
        if args.scale_markers is not None
        else [{"family": args.family, "id": args.id, "size_m": args.physical_side_m}]
    )

    horizontal = np.loadtxt(args.horizontal_alignment, dtype=float)[:3, :3]
    aqx, aqy, aqz, aqw = map(
        float, args.asset_rotation_quaternion_xyzw.split(",")
    )
    asset_rotation = Rotation.from_quat([aqx, aqy, aqz, aqw]).as_matrix()
    world_config = json.loads(args.world_alignment.read_text(encoding="utf-8"))
    transform = world_config["generated_to_world"]
    scale = float(transform["scale"])
    qx, qy, qz, qw = map(float, transform["rotation_quaternion_xyzw"])
    world_rotation = Rotation.from_quat([qx, qy, qz, qw]).as_matrix()
    translation = np.asarray(transform["translation_m"], dtype=float)

    tags = [
        tag_annotation(
            triangulated_corners,
            marker,
            horizontal,
            asset_rotation,
            scale,
            world_rotation,
            translation,
        )
        for marker in markers
    ]
    primary = tags[0]

    output = {
        "schema_version": "1.0.0",
        "type": "apriltag_world_corners",
        # Keep the first tag at the legacy top level.  Existing v1.0/v1.1
        # consumers continue to load it, while v1.2-capable consumers use
        # ``tags`` to expose every verified fixed anchor.
        **primary,
        "tags": tags,
        "coordinate_stages": {
            "horizontal_alignment_matrix": horizontal.tolist(),
            "asset_rotation_quaternion_xyzw": [aqx, aqy, aqz, aqw],
            "world_scale": scale,
            "world_rotation_quaternion_xyzw": [qx, qy, qz, qw],
            "world_translation_m": translation.tolist(),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
