"""Shared PLY and coordinate-transform helpers for dense geometry tools."""

from __future__ import annotations

import argparse
import json
import math
import struct
from pathlib import Path
from typing import Any, BinaryIO


PLY_SCALAR_TYPES = {
    "char": "b",
    "uchar": "B",
    "int8": "b",
    "uint8": "B",
    "short": "h",
    "ushort": "H",
    "int16": "h",
    "uint16": "H",
    "int": "i",
    "uint": "I",
    "int32": "i",
    "uint32": "I",
    "float": "f",
    "float32": "f",
    "double": "d",
    "float64": "d",
}


def read_ply_header(handle: BinaryIO) -> tuple[str, int, list[tuple[str, str]]]:
    first = handle.readline().decode("ascii").strip()
    if first != "ply":
        raise RuntimeError("Input is not a PLY file.")

    file_format = ""
    vertex_count: int | None = None
    properties: list[tuple[str, str]] = []
    in_vertex = False
    while True:
        line = handle.readline().decode("ascii").strip()
        if not line:
            raise RuntimeError("PLY header ended unexpectedly.")
        fields = line.split()
        if fields[:1] == ["format"]:
            file_format = fields[1]
        elif fields[:2] == ["element", "vertex"]:
            vertex_count = int(fields[2])
            in_vertex = True
        elif fields[:1] == ["element"]:
            in_vertex = False
        elif in_vertex and fields[:1] == ["property"]:
            if len(fields) != 3 or fields[1] == "list":
                raise RuntimeError("Only scalar vertex properties are supported.")
            properties.append((fields[2], fields[1]))
        elif fields == ["end_header"]:
            break

    if file_format not in {"ascii", "binary_little_endian"}:
        raise RuntimeError(f"Unsupported PLY format: {file_format}")
    if vertex_count is None:
        raise RuntimeError("PLY does not contain a vertex element.")
    names = {name for name, _ in properties}
    if not {"x", "y", "z"}.issubset(names):
        raise RuntimeError("PLY vertex properties must include x, y, z.")
    return file_format, vertex_count, properties


def iter_ply_points(path: Path, batch_size: int = 100_000):
    with path.open("rb") as handle:
        file_format, vertex_count, properties = read_ply_header(handle)
        property_names = [name for name, _ in properties]
        xyz_indices = [property_names.index(axis) for axis in ("x", "y", "z")]

        if file_format == "ascii":
            remaining = vertex_count
            while remaining:
                count = min(batch_size, remaining)
                rows = []
                for _ in range(count):
                    values = handle.readline().split()
                    if len(values) != len(properties):
                        raise RuntimeError("Unexpected ASCII PLY vertex row.")
                    rows.append([float(values[index]) for index in xyz_indices])
                remaining -= count
                yield rows
            return

        try:
            fmt = "<" + "".join(PLY_SCALAR_TYPES[property_type] for _, property_type in properties)
        except KeyError as error:
            raise RuntimeError(f"Unsupported PLY scalar type: {error.args[0]}") from error
        row_size = struct.calcsize(fmt)
        unpack = struct.Struct(fmt).unpack
        remaining = vertex_count
        while remaining:
            count = min(batch_size, remaining)
            raw = handle.read(count * row_size)
            if len(raw) != count * row_size:
                raise RuntimeError("Unexpected end of binary PLY vertex data.")
            points: list[tuple[float, float, float]] = []
            for row_index in range(count):
                row = unpack(raw[row_index * row_size : (row_index + 1) * row_size])
                points.append(tuple(float(row[index]) for index in xyz_indices))
            remaining -= count
            yield points


def quaternion_matrix(quaternion: list[float]) -> list[list[float]]:
    x, y, z, w = quaternion
    length = math.sqrt(x * x + y * y + z * z + w * w)
    if length == 0:
        raise RuntimeError("Rotation quaternion must not be zero.")
    x, y, z, w = x / length, y / length, z / length, w / length
    return [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]


def matrix_multiply(left: list[list[float]], right: list[list[float]]) -> list[list[float]]:
    return [
        [sum(left[row][index] * right[index][column] for index in range(3)) for column in range(3)]
        for row in range(3)
    ]


def matrix_vector(matrix: list[list[float]], point: tuple[float, float, float]) -> list[float]:
    return [sum(matrix[row][column] * point[column] for column in range(3)) for row in range(3)]


def load_transform(world_alignment: Path, asset_rotation: str) -> tuple[float, list[list[float]], list[float], list[float]]:
    config = json.loads(world_alignment.read_text(encoding="utf-8"))
    transform = config["generated_to_world"]
    scale = float(transform["scale"])
    world_rotation = quaternion_matrix(
        [float(value) for value in transform["rotation_quaternion_xyzw"]]
    )
    translation = [float(value) for value in transform["translation_m"]]
    quaternion = [float(value) for value in asset_rotation.split(",")]
    if len(quaternion) != 4:
        raise RuntimeError("asset_rotation_quaternion_xyzw must have four values.")
    asset_matrix = quaternion_matrix(quaternion)
    return scale, matrix_multiply(world_rotation, asset_matrix), translation, quaternion


def evaluate(
    pointcloud: Path,
    world_alignment: Path,
    config: dict[str, Any],
    input_image_count: int,
    asset_rotation: str,
) -> dict[str, Any]:
    scale, rotation, translation, asset_quaternion = load_transform(
        world_alignment, asset_rotation
    )
    generated_min = [math.inf, math.inf, math.inf]
    generated_max = [-math.inf, -math.inf, -math.inf]
    world_min = [math.inf, math.inf, math.inf]
    world_max = [-math.inf, -math.inf, -math.inf]
    finite_count = 0
    non_finite_count = 0
    means = [0.0, 0.0, 0.0]
    sum_squared_deltas = [0.0, 0.0, 0.0]

    for generated_points in iter_ply_points(pointcloud):
        for generated_point in generated_points:
            if not all(math.isfinite(value) for value in generated_point):
                non_finite_count += 1
                continue
            finite_count += 1
            for axis in range(3):
                generated_min[axis] = min(generated_min[axis], generated_point[axis])
                generated_max[axis] = max(generated_max[axis], generated_point[axis])
                delta = generated_point[axis] - means[axis]
                means[axis] += delta / finite_count
                sum_squared_deltas[axis] += delta * (generated_point[axis] - means[axis])
            rotated = matrix_vector(rotation, generated_point)
            world_point = [scale * rotated[axis] + translation[axis] for axis in range(3)]
            for axis in range(3):
                world_min[axis] = min(world_min[axis], world_point[axis])
                world_max[axis] = max(world_max[axis], world_point[axis])

    dense_count = finite_count + non_finite_count
    quality_gate = config["quality_gate"]
    failure_reasons: list[str] = []
    warnings = [
        "Dense-stage gate evaluates point validity and bounds; proxy-stage plane coverage and residuals are reported separately."
    ]
    if dense_count == 0:
        failure_reasons.append("empty_dense_pointcloud")
    if finite_count < int(quality_gate["minimum_dense_point_count"]):
        failure_reasons.append("insufficient_dense_point_count")
    non_finite_ratio = non_finite_count / dense_count if dense_count else 1.0
    if non_finite_ratio > float(quality_gate["maximum_non_finite_point_ratio"]):
        failure_reasons.append("non_finite_dense_points")

    world_extent = [world_max[axis] - world_min[axis] for axis in range(3)] if finite_count else None
    if finite_count and max(world_extent) > float(
        quality_gate["maximum_world_axis_extent_m"]
    ):
        failure_reasons.append("implausible_world_extent")

    standard_deviations = [
        math.sqrt(value / (finite_count - 1)) if finite_count > 1 else 0.0
        for value in sum_squared_deltas
    ]
    outlier_count = 0
    outlier_sigma = float(quality_gate["outlier_sigma"])
    if finite_count > 1:
        for generated_points in iter_ply_points(pointcloud):
            for generated_point in generated_points:
                if not all(math.isfinite(value) for value in generated_point):
                    continue
                if any(
                    standard_deviations[axis] > 0
                    and abs(generated_point[axis] - means[axis])
                    > outlier_sigma * standard_deviations[axis]
                    for axis in range(3)
                ):
                    outlier_count += 1

    return {
        "schema_version": "1.0.0",
        "stage": "automatic_dense_geometry_poc",
        "status": "passed" if not failure_reasons else "unverified",
        "dense_point_count": dense_count,
        "finite_dense_point_count": finite_count,
        "nan_or_inf_point_count": non_finite_count,
        "outlier_count": outlier_count,
        "outlier_method": {
            "name": "generated_axiswise_standard_deviation",
            "sigma": outlier_sigma,
            "removed_from_output": False,
        },
        "input_image_count": input_image_count,
        "geometric_coverage": None,
        "plane_support_counts": None,
        "detected_floor_count": None,
        "detected_wall_count": None,
        "detected_ceiling_count": None,
        "unexplained_point_ratio": None,
        "mean_surface_residual_m": None,
        "median_surface_residual_m": None,
        "fusion_quality": {
            "mean_residual": None,
            "median_residual": None,
            "reason": "COLMAP stereo_fusion PLY has no per-point residual field.",
        },
        "invalid_intersection_count": None,
        "generated_bounds": {
            "min": generated_min if finite_count else None,
            "max": generated_max if finite_count else None,
        },
        "world_bounds_m": {
            "min": world_min if finite_count else None,
            "max": world_max if finite_count else None,
            "extent": world_extent,
        },
        "world_transform": {
            "source": str(world_alignment),
            "asset_rotation_quaternion_xyzw": asset_quaternion,
            "description": "world = scale * world_rotation * asset_rotation * generated + translation",
        },
        "failure_reasons": failure_reasons,
        "warnings": warnings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate a COLMAP dense PLY without creating proxy geometry."
    )
    parser.add_argument("--pointcloud", type=Path, required=True)
    parser.add_argument("--world_alignment", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--input_image_count", type=int, required=True)
    parser.add_argument(
        "--asset_rotation_quaternion_xyzw", default="1,0,0,0",
        help="Applied after the aligned COLMAP coordinates and before world alignment.",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    result = evaluate(
        args.pointcloud,
        args.world_alignment,
        config,
        args.input_image_count,
        args.asset_rotation_quaternion_xyzw,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
