from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import struct
from collections import deque
from pathlib import Path
from typing import Any

if __package__:
    from .dense_geometry_common import iter_ply_points, load_transform, matrix_vector
else:
    from dense_geometry_common import iter_ply_points, load_transform, matrix_vector


Point = tuple[float, float, float]


def read_surface_ply(path: Path) -> tuple[list[Point], list[tuple[int, int, int]]]:
    """Read the binary/ascii vertex and triangular face data emitted by COLMAP."""
    with path.open("rb") as handle:
        format_name = ""
        vertex_count = 0
        face_count = 0
        header: list[str] = []
        while True:
            line = handle.readline().decode("ascii").strip()
            header.append(line)
            if line == "end_header":
                break
            fields = line.split()
            if fields[:1] == ["format"]:
                format_name = fields[1]
            elif fields[:2] == ["element", "vertex"]:
                vertex_count = int(fields[2])
            elif fields[:2] == ["element", "face"]:
                face_count = int(fields[2])
        if format_name not in {"ascii", "binary_little_endian"}:
            raise RuntimeError(f"Unsupported surface PLY format: {format_name}")
        vertices: list[Point] = []
        faces: list[tuple[int, int, int]] = []
        if format_name == "ascii":
            for _ in range(vertex_count):
                fields = handle.readline().split()
                vertices.append((float(fields[0]), float(fields[1]), float(fields[2])))
            for _ in range(face_count):
                fields = handle.readline().split()
                if int(fields[0]) >= 3:
                    faces.append((int(fields[1]), int(fields[2]), int(fields[3])))
            return vertices, faces

        vertex_struct = struct.Struct("<fff")
        for _ in range(vertex_count):
            row = vertex_struct.unpack(handle.read(vertex_struct.size))
            vertices.append((float(row[0]), float(row[1]), float(row[2])))
        for _ in range(face_count):
            count_raw = handle.read(1)
            if len(count_raw) != 1:
                raise RuntimeError("Unexpected end of surface PLY face data.")
            count = struct.unpack("<B", count_raw)[0]
            values = struct.unpack("<" + "i" * count, handle.read(4 * count))
            if count >= 3:
                faces.append((int(values[0]), int(values[1]), int(values[2])))
        return vertices, faces


def triangle_area(a: Point, b: Point, c: Point) -> float:
    return math.sqrt(dot(cross(sub(b, a), sub(c, a)), cross(sub(b, a), sub(c, a)))) * 0.5


def point_triangle_distance_sq(point: Point, a: Point, b: Point, c: Point) -> float:
    """Return the squared distance from a point to a (non-degenerate) triangle."""
    ab = sub(b, a)
    ac = sub(c, a)
    ap = sub(point, a)
    d1 = dot(ab, ap)
    d2 = dot(ac, ap)
    if d1 <= 0.0 and d2 <= 0.0:
        return dot(ap, ap)

    bp = sub(point, b)
    d3 = dot(ab, bp)
    d4 = dot(ac, bp)
    if d3 >= 0.0 and d4 <= d3:
        return dot(bp, bp)

    vc = d1 * d4 - d3 * d2
    if vc <= 0.0 and d1 >= 0.0 and d3 <= 0.0:
        denominator = d1 - d3
        if denominator > 0.0:
            v = d1 / denominator
            projection = tuple(a[index] + v * ab[index] for index in range(3))
            delta = sub(point, projection)
            return dot(delta, delta)

    cp = sub(point, c)
    d5 = dot(ab, cp)
    d6 = dot(ac, cp)
    if d6 >= 0.0 and d5 <= d6:
        return dot(cp, cp)

    vb = d5 * d2 - d1 * d6
    if vb <= 0.0 and d2 >= 0.0 and d6 <= 0.0:
        denominator = d2 - d6
        if denominator > 0.0:
            w = d2 / denominator
            projection = tuple(a[index] + w * ac[index] for index in range(3))
            delta = sub(point, projection)
            return dot(delta, delta)

    va = d3 * d6 - d5 * d4
    if va <= 0.0 and (d4 - d3) >= 0.0 and (d5 - d6) >= 0.0:
        denominator = (d4 - d3) + (d5 - d6)
        if denominator > 0.0:
            w = (d4 - d3) / denominator
            projection = tuple(b[index] + w * (c[index] - b[index]) for index in range(3))
            delta = sub(point, projection)
            return dot(delta, delta)

    denominator = va + vb + vc
    if denominator <= 0.0:
        return min(dot(sub(point, candidate), sub(point, candidate)) for candidate in (a, b, c))
    inverse = 1.0 / denominator
    v = vb * inverse
    w = vc * inverse
    projection = tuple(a[index] + v * ab[index] + w * ac[index] for index in range(3))
    delta = sub(point, projection)
    return dot(delta, delta)


def surface_mesh_components(faces: list[tuple[int, int, int]], minimum_faces: int) -> tuple[list[tuple[int, int, int]], int, float, int]:
    if not faces:
        return [], 0, 0.0, 0
    edge_to_faces: dict[tuple[int, int], list[int]] = {}
    for index, face in enumerate(faces):
        for a, b in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0])):
            edge_to_faces.setdefault(tuple(sorted((a, b))), []).append(index)
    adjacency: list[list[int]] = [[] for _ in faces]
    for linked in edge_to_faces.values():
        for left in linked:
            adjacency[left].extend(right for right in linked if right != left)
    unseen = set(range(len(faces)))
    components: list[list[int]] = []
    while unseen:
        seed = unseen.pop()
        queue = deque([seed])
        component = [seed]
        while queue:
            current = queue.popleft()
            for neighbor in adjacency[current]:
                if neighbor in unseen:
                    unseen.remove(neighbor)
                    queue.append(neighbor)
                    component.append(neighbor)
        components.append(component)
    kept = [component for component in components if len(component) >= minimum_faces]
    kept_faces = [faces[index] for component in kept for index in component]
    largest_ratio = max((len(component) for component in components), default=0) / len(faces)
    return kept_faces, len(components), largest_ratio, len(components) - len(kept)


def surface_mesh_normals(vertices: list[Point], faces: list[tuple[int, int, int]]) -> list[Point]:
    normals = [[0.0, 0.0, 0.0] for _ in vertices]
    for a, b, c in faces:
        normal = cross(sub(vertices[b], vertices[a]), sub(vertices[c], vertices[a]))
        for index in (a, b, c):
            normals[index][0] += normal[0]
            normals[index][1] += normal[1]
            normals[index][2] += normal[2]
    return [normalize(tuple(normal)) or (0.0, 1.0, 0.0) for normal in normals]


def write_surface_glb(vertices: list[Point], normals: list[Point], faces: list[tuple[int, int, int]], output: Path) -> None:
    positions = struct.pack("<" + "f" * (3 * len(vertices)), *(value for point in vertices for value in point))
    normal_bytes = struct.pack("<" + "f" * (3 * len(normals)), *(value for normal in normals for value in normal))
    index_count = 3 * len(faces)
    indices = struct.pack("<" + "I" * index_count, *(index for face in faces for index in face))
    position_offset = 0
    normal_offset = len(positions)
    index_offset = normal_offset + len(normal_bytes)
    binary = positions + normal_bytes + indices
    document = {
        "asset": {"version": "2.0", "generator": "generate_proxy_geometry.py surface_mesh"},
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": position_offset, "byteLength": len(positions), "target": 34962},
            {"buffer": 0, "byteOffset": normal_offset, "byteLength": len(normal_bytes), "target": 34962},
            {"buffer": 0, "byteOffset": index_offset, "byteLength": len(indices), "target": 34963},
        ],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": len(vertices), "type": "VEC3",
             "min": [min(point[axis] for point in vertices) for axis in range(3)],
             "max": [max(point[axis] for point in vertices) for axis in range(3)]},
            {"bufferView": 1, "componentType": 5126, "count": len(normals), "type": "VEC3"},
            {"bufferView": 2, "componentType": 5125, "count": index_count, "type": "SCALAR"},
        ],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "NORMAL": 1}, "indices": 2}]}],
        "nodes": [{"mesh": 0}], "scenes": [{"nodes": [0]}], "scene": 0,
    }
    json_bytes = json.dumps(document, separators=(",", ":")).encode("utf-8")
    json_bytes += b" " * ((4 - len(json_bytes) % 4) % 4)
    binary += b"\x00" * ((4 - len(binary) % 4) % 4)
    total = 12 + 8 + len(json_bytes) + 8 + len(binary)
    output.write_bytes(
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<I4s", len(json_bytes), b"JSON")
        + json_bytes
        + struct.pack("<I4s", len(binary), b"BIN\x00")
        + binary
    )


def surface_mesh_quality(
    vertices: list[Point],
    faces: list[tuple[int, int, int]],
    dense_points: list[Point],
    config: dict[str, Any],
    dense_quality: dict[str, Any],
    output_glb: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    gate = config["surface_mesh_quality_gate"]
    extraction = config["surface_mesh"]
    finite_vertices = [point for point in vertices if all(math.isfinite(value) for value in point)]
    nonfinite_vertices = len(vertices) - len(finite_vertices)
    valid_faces: list[tuple[int, int, int]] = []
    degenerate_count = 0
    for face in faces:
        if any(index < 0 or index >= len(vertices) for index in face) or len(set(face)) != 3:
            degenerate_count += 1
            continue
        if not all(math.isfinite(value) for index in face for value in vertices[index]):
            degenerate_count += 1
            continue
        if triangle_area(vertices[face[0]], vertices[face[1]], vertices[face[2]]) <= float(gate["minimum_triangle_area_m2"]):
            degenerate_count += 1
            continue
        valid_faces.append(face)
    filtered_faces, component_count, largest_component_ratio, removed_component_count = surface_mesh_components(
        valid_faces, int(gate["minimum_component_faces"])
    )
    used = sorted({index for face in filtered_faces for index in face})
    remap = {old: new for new, old in enumerate(used)}
    compact_vertices = [vertices[index] for index in used]
    compact_faces = [tuple(remap[index] for index in face) for face in filtered_faces]
    finite_compact_vertices = sum(
        all(math.isfinite(value) for value in point) for point in compact_vertices
    )
    normals = surface_mesh_normals(compact_vertices, compact_faces)
    mesh_min = [min(point[axis] for point in compact_vertices) for axis in range(3)] if compact_vertices else None
    mesh_max = [max(point[axis] for point in compact_vertices) for axis in range(3)] if compact_vertices else None
    dense_min = [min(point[axis] for point in dense_points) for axis in range(3)] if dense_points else None
    dense_max = [max(point[axis] for point in dense_points) for axis in range(3)] if dense_points else None
    mesh_extent = [mesh_max[axis] - mesh_min[axis] for axis in range(3)] if mesh_min and mesh_max else None
    dense_extent = [dense_max[axis] - dense_min[axis] for axis in range(3)] if dense_min and dense_max else None
    extent_ratio = [mesh_extent[axis] / dense_extent[axis] if dense_extent[axis] else None for axis in range(3)] if mesh_extent and dense_extent else None
    triangle_step = max(1, len(compact_faces) // int(extraction["distance_sample_triangle_count"]))
    distance_triangles = [
        (compact_vertices[face[0]], compact_vertices[face[1]], compact_vertices[face[2]])
        for face in compact_faces[::triangle_step]
    ]
    dense_step = max(1, len(dense_points) // int(extraction["distance_sample_point_count"]))
    distance_samples = []
    for point in dense_points[::dense_step]:
        if not distance_triangles:
            break
        distance_samples.append(
            math.sqrt(
                min(point_triangle_distance_sq(point, first, second, third) for first, second, third in distance_triangles)
            )
        )
    distance_samples.sort()
    coverage_threshold = float(gate["surface_distance_threshold_m"])
    coverage = sum(distance <= coverage_threshold for distance in distance_samples) / len(distance_samples) if distance_samples else 0.0
    surface_area = sum(triangle_area(compact_vertices[a], compact_vertices[b], compact_vertices[c]) for a, b, c in compact_faces)
    signed_volume = sum(
        dot(compact_vertices[a], cross(compact_vertices[b], compact_vertices[c])) / 6.0
        for a, b, c in compact_faces
    ) if compact_faces else 0.0
    volume = abs(signed_volume)
    failures: list[str] = []
    if dense_quality.get("status") != "verified":
        failures.append("dense_quality_not_verified")
    if len(compact_vertices) < int(gate["minimum_vertex_count"]):
        failures.append("insufficient_mesh_vertices")
    if len(compact_faces) < int(gate["minimum_triangle_count"]):
        failures.append("insufficient_mesh_triangles")
    if nonfinite_vertices:
        failures.append("nonfinite_mesh_vertices")
    if degenerate_count > int(gate["maximum_degenerate_triangle_count"]):
        failures.append("degenerate_mesh_faces")
    if component_count > int(gate["maximum_component_count"]):
        failures.append("excessive_mesh_components")
    if largest_component_ratio < float(gate["minimum_largest_component_ratio"]):
        failures.append("fragmented_mesh_components")
    if not extent_ratio or any(value is None or value < float(gate["minimum_extent_ratio"]) or value > float(gate["maximum_extent_ratio"]) for value in extent_ratio):
        failures.append("mesh_dense_extent_mismatch")
    if coverage < float(gate["minimum_mesh_coverage_ratio"]):
        failures.append("insufficient_mesh_coverage")
    if distance_samples and max(distance_samples) > float(gate["maximum_surface_distance_m"]):
        failures.append("excessive_surface_distance")
    if not math.isfinite(surface_area) or surface_area <= 0 or surface_area > float(gate["maximum_surface_area_m2"]):
        failures.append("abnormal_mesh_surface_area")
    if not math.isfinite(volume) or volume > float(gate["maximum_mesh_volume_m3"]):
        failures.append("abnormal_mesh_volume")
    quality = {
        "schema_version": "1.0.0",
        "stage": "surface_mesh",
        "proxy_kind": "surface_mesh",
        "status": "verified" if not failures else "unverified",
        "capabilities": {"measurement": not failures, "surface_placement": not failures, "fov": not failures, "occlusion": not failures, "semantic_object_query": False},
        "vertex_count": len(compact_vertices),
        "triangle_count": len(compact_faces),
        "finite_vertex_count": finite_compact_vertices,
        "nan_vertex_count": sum(any(math.isnan(value) for value in point) for point in vertices),
        "inf_vertex_count": sum(any(math.isinf(value) for value in point) for point in vertices),
        "degenerate_triangle_count": degenerate_count,
        "connected_component_count": component_count,
        "removed_small_component_count": removed_component_count,
        "largest_component_ratio": largest_component_ratio,
        "mesh_bounding_box": {"min": mesh_min, "max": mesh_max, "extent": mesh_extent},
        "bounding_box_dimension_ratio": extent_ratio,
        "sampled_dense_to_mesh_distance": {"sample_count": len(distance_samples), "threshold_m": coverage_threshold},
        "mesh_coverage_ratio": coverage,
        "mean_surface_distance_m": sum(distance_samples) / len(distance_samples) if distance_samples else None,
        "median_surface_distance_m": distance_samples[len(distance_samples) // 2] if distance_samples else None,
        "max_surface_distance_m": max(distance_samples) if distance_samples else None,
        "surface_area_m2": surface_area,
        "failure_reasons": failures,
        "warnings": ["Surface distances use sampled point-to-triangle distances for the MVP quality gate."],
        "provenance": {
            "semantic_proxy_status": "unverified",
        },
    }
    metadata = {
        "schema_version": "1.0.0",
        "proxy_kind": "surface_mesh",
        "unit": "meter",
        "coordinate_system": {"up_axis": "Y"},
        "world_transform": {"formula": "world = scale * world_rotation * asset_rotation * dense_aligned + translation"},
        "mesh": {
            "vertex_count": len(compact_vertices),
            "triangle_count": len(compact_faces),
            "bounding_box": {"min": mesh_min, "max": mesh_max, "extent": mesh_extent},
            "source_mesher": "COLMAP delaunay_mesher",
        },
        "capabilities": quality["capabilities"],
        "generation_parameters": {
            "distance_sample_point_count": int(extraction["distance_sample_point_count"]),
            "distance_sample_triangle_count": int(extraction["distance_sample_triangle_count"]),
            "minimum_component_faces": int(gate["minimum_component_faces"]),
            "surface_placement": config.get("surface_placement", {}),
        },
        "provenance": {
            "semantic_proxy_status": "unverified",
        },
    }
    write_surface_glb(compact_vertices, normals, compact_faces, output_glb)
    glb_write_success = output_glb.is_file() and output_glb.stat().st_size > 0
    quality["glb_write_result"] = {
        "status": "verified" if glb_write_success else "failed",
        "byte_length": output_glb.stat().st_size if glb_write_success else 0,
    }
    quality["viewer_load_result"] = {
        "status": "not_evaluated",
        "reason": "Requires the Web Viewer runtime acceptance step.",
    }
    if not glb_write_success:
        quality["status"] = "failed"
        quality["failure_reasons"].append("glb_write_failed")
    return quality, metadata


def sub(a: Point, b: Point) -> Point:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def cross(a: Point, b: Point) -> Point:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def dot(a: Point, b: Point) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def normalize(vector: Point) -> Point | None:
    length = math.sqrt(dot(vector, vector))
    if length < 1e-9:
        return None
    return (vector[0] / length, vector[1] / length, vector[2] / length)


def median(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def orient_plane(normal: Point, distance: float) -> tuple[Point, float]:
    """Choose a deterministic normal sign so offsets can be compared."""
    vector = normalize(normal)
    if vector is None:
        raise RuntimeError("Plane normal must not be zero.")
    axis = max(range(3), key=lambda index: abs(vector[index]))
    if vector[axis] < 0:
        vector = tuple(-value for value in vector)
        distance = -distance
    return vector, distance


def load_world_points(fused_ply: Path, world_alignment: Path, asset_rotation: str) -> list[Point]:
    scale, rotation, translation, _ = load_transform(world_alignment, asset_rotation)
    points: list[Point] = []
    for batch in iter_ply_points(fused_ply):
        for point in batch:
            if not all(math.isfinite(value) for value in point):
                continue
            rotated = matrix_vector(rotation, point)
            points.append(tuple(scale * rotated[index] + translation[index] for index in range(3)))
    return points


def voxel_downsample(points: list[Point], voxel_size: float) -> tuple[list[Point], dict[tuple[int, int, int], Point]]:
    bins: dict[tuple[int, int, int], list[float]] = {}
    for point in points:
        key = tuple(math.floor(value / voxel_size) for value in point)
        values = bins.setdefault(key, [0.0, 0.0, 0.0, 0.0])
        values[0] += point[0]
        values[1] += point[1]
        values[2] += point[2]
        values[3] += 1
    centers = {
        key: (values[0] / values[3], values[1] / values[3], values[2] / values[3])
        for key, values in bins.items()
    }
    return list(centers.values()), centers


def extract_planes(points: list[Point], config: dict[str, Any]) -> tuple[list[dict[str, Any]], set[int]]:
    extraction = config["future_proxy_extraction"]
    threshold = float(extraction["plane_distance_threshold_m"])
    minimum_support = int(extraction["minimum_plane_support_points"])
    max_planes = int(extraction["maximum_plane_count"])
    iterations = int(extraction["plane_ransac_iterations"])
    remaining = set(range(len(points)))
    planes: list[dict[str, Any]] = []
    rng = random.Random(0)
    for _ in range(max_planes):
        candidates = list(remaining)
        if len(candidates) < minimum_support:
            break
        sampled = candidates if len(candidates) <= 15000 else rng.sample(candidates, 15000)
        best_normal: Point | None = None
        best_distance = 0.0
        best_support: list[int] = []
        for _ in range(iterations):
            i, j, k = rng.sample(sampled, 3)
            normal = normalize(cross(sub(points[j], points[i]), sub(points[k], points[i])))
            if normal is None:
                continue
            distance = -dot(normal, points[i])
            support = [index for index in sampled if abs(dot(normal, points[index]) + distance) <= threshold]
            if len(support) > len(best_support):
                best_normal, best_distance, best_support = normal, distance, support
        if best_normal is None or len(best_support) < minimum_support:
            break
        support = [index for index in candidates if abs(dot(best_normal, points[index]) + best_distance) <= threshold]
        if len(support) < minimum_support:
            break
        planes.append({"normal": best_normal, "distance": best_distance, "support": support})
        remaining.difference_update(support)
    return planes, remaining


def classify_planes(planes: list[dict[str, Any]], points: list[Point], config: dict[str, Any]) -> list[dict[str, Any]]:
    all_median_y = median([point[1] for point in points])
    angle = math.radians(float(config["future_proxy_extraction"]["normal_angle_threshold_deg"]))
    vertical_limit = math.cos(angle)
    classified = []
    for plane in planes:
        support = [points[index] for index in plane["support"]]
        centroid = tuple(sum(point[axis] for point in support) / len(support) for axis in range(3))
        plane_normal, plane_offset = orient_plane(plane["normal"], plane["distance"])
        normal = plane_normal
        if abs(normal[1]) >= vertical_limit:
            semantic = "floor" if centroid[1] <= all_median_y else "ceiling"
            target_normal = (0.0, 1.0, 0.0) if semantic == "floor" else (0.0, -1.0, 0.0)
            if dot(plane_normal, target_normal) < 0:
                plane_normal = tuple(-value for value in plane_normal)
                plane_offset = -plane_offset
            normal = target_normal
        else:
            semantic = "wall"
        bounds = {
            "min": [min(point[axis] for point in support) for axis in range(3)],
            "max": [max(point[axis] for point in support) for axis in range(3)],
        }
        residuals = [abs(dot(plane["normal"], point) + plane["distance"]) for point in support]
        ordered_residuals = sorted(residuals)
        classified.append({
            "semantic_class": semantic,
            "normal": list(normal),
            "centroid": list(centroid),
            "bounds_m": bounds,
            "support_count": len(support),
            "plane_normal": list(plane_normal),
            "plane_offset": plane_offset,
            "surface_residual_m": {
                "mean": sum(residuals) / len(residuals),
                "median": ordered_residuals[len(ordered_residuals) // 2],
                "max": max(residuals),
            },
        })
    return classified


def bounds_gap(left: dict[str, Any], right: dict[str, Any]) -> float:
    gaps = [
        max(
            0.0,
            float(left["max"][axis]) - float(right["min"][axis]),
            float(right["max"][axis]) - float(left["min"][axis]),
        )
        for axis in range(3)
    ]
    # The expression above is the overlap, so a separated axis has a negative
    # value before max(). Compute the actual separation explicitly.
    gaps = [
        max(
            0.0,
            float(right["min"][axis]) - float(left["max"][axis]),
            float(left["min"][axis]) - float(right["max"][axis]),
        )
        for axis in range(3)
    ]
    return max(gaps)


def compatible_plane(left: dict[str, Any], right: dict[str, Any], config: dict[str, Any]) -> bool:
    if left["semantic_class"] != right["semantic_class"]:
        return False
    angle = math.radians(float(config["future_proxy_extraction"]["merge_normal_angle_deg"]))
    normal_dot = abs(dot(tuple(left["plane_normal"]), tuple(right["plane_normal"])))
    if normal_dot < math.cos(angle):
        return False
    if abs(float(left["plane_offset"]) - float(right["plane_offset"])) > float(config["future_proxy_extraction"]["merge_plane_offset_distance_m"]):
        return False
    return bounds_gap(left["bounds_m"], right["bounds_m"]) <= float(config["future_proxy_extraction"]["merge_boundary_distance_m"])


def merge_plane_segments(raw_objects: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for candidate in raw_objects:
        match = next((item for item in merged if compatible_plane(item, candidate, config)), None)
        if match is None:
            merged.append(dict(candidate))
            continue
        left_count = int(match["support_count"])
        right_count = int(candidate["support_count"])
        total = left_count + right_count
        match["centroid"] = [
            (left_count * match["centroid"][axis] + right_count * candidate["centroid"][axis]) / total
            for axis in range(3)
        ]
        match["support_count"] = total
        match["bounds_m"] = {
            "min": [min(match["bounds_m"]["min"][axis], candidate["bounds_m"]["min"][axis]) for axis in range(3)],
            "max": [max(match["bounds_m"]["max"][axis], candidate["bounds_m"]["max"][axis]) for axis in range(3)],
        }
        normal = normalize(tuple(left_count * value + right_count * candidate["plane_normal"][axis] for axis, value in enumerate(match["plane_normal"])) )
        if normal is not None:
            match["plane_normal"] = list(normal)
            match["normal"] = list(normal)
        match["plane_offset"] = (left_count * match["plane_offset"] + right_count * candidate["plane_offset"]) / total
        match["surface_residual_m"] = {
            key: max(match["surface_residual_m"][key], candidate["surface_residual_m"][key])
            for key in ("mean", "median", "max")
        }
    return merged


def components(centers: dict[tuple[int, int, int], Point]) -> list[list[Point]]:
    unseen = set(centers)
    groups: list[list[Point]] = []
    while unseen:
        seed = unseen.pop()
        queue = deque([seed])
        group = [centers[seed]]
        while queue:
            key = queue.popleft()
            for axis in range(3):
                for delta in (-1, 1):
                    neighbor = list(key)
                    neighbor[axis] += delta
                    neighbor_tuple = tuple(neighbor)
                    if neighbor_tuple in unseen:
                        unseen.remove(neighbor_tuple)
                        queue.append(neighbor_tuple)
                        group.append(centers[neighbor_tuple])
        groups.append(group)
    return groups


def write_glb(objects: list[dict[str, Any]], output: Path) -> None:
    vertices: list[float] = []
    indices: list[int] = []
    for item in objects:
        minimum = item["bounds_m"]["min"]
        maximum = item["bounds_m"]["max"]
        start = len(vertices) // 3
        for x, y, z in ((minimum[0], minimum[1], minimum[2]), (maximum[0], minimum[1], minimum[2]), (maximum[0], maximum[1], minimum[2]), (minimum[0], maximum[1], minimum[2]), (minimum[0], minimum[1], maximum[2]), (maximum[0], minimum[1], maximum[2]), (maximum[0], maximum[1], maximum[2]), (minimum[0], maximum[1], maximum[2])):
            vertices.extend((x, y, z))
        indices.extend(start + value for value in (0, 1, 2, 0, 2, 3, 4, 6, 5, 4, 7, 6, 0, 4, 5, 0, 5, 1, 1, 5, 6, 1, 6, 2, 2, 6, 7, 2, 7, 3, 3, 7, 4, 3, 4, 0))
    position_bytes = struct.pack("<" + "f" * len(vertices), *vertices)
    index_bytes = struct.pack("<" + "I" * len(indices), *indices)
    padding = b"\x00" * ((4 - len(position_bytes) % 4) % 4)
    binary = position_bytes + padding + index_bytes
    document = {
        "asset": {"version": "2.0", "generator": "generate_proxy_geometry.py"},
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(position_bytes), "target": 34962}, {"buffer": 0, "byteOffset": len(position_bytes) + len(padding), "byteLength": len(index_bytes), "target": 34963}],
        "accessors": [{"bufferView": 0, "componentType": 5126, "count": len(vertices) // 3, "type": "VEC3"}, {"bufferView": 1, "componentType": 5125, "count": len(indices), "type": "SCALAR"}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
        "nodes": [{"mesh": 0}], "scenes": [{"nodes": [0]}], "scene": 0,
    }
    json_bytes = json.dumps(document, separators=(",", ":")).encode("utf-8")
    json_bytes += b" " * ((4 - len(json_bytes) % 4) % 4)
    total = 12 + 8 + len(json_bytes) + 8 + len(binary)
    output.write_bytes(struct.pack("<4sII", b"glTF", 2, total) + struct.pack("<I4s", len(json_bytes), b"JSON") + json_bytes + struct.pack("<I4s", len(binary), b"BIN\x00") + binary)


def bbox_volume(item: dict[str, Any]) -> float:
    bounds = item["bounds_m"]
    return math.prod(max(0.0, bounds["max"][axis] - bounds["min"][axis]) for axis in range(3))


def merge_obstacles(
    raw_objects: list[dict[str, Any]], config: dict[str, Any]
) -> tuple[list[dict[str, Any]], int]:
    extraction = config["future_proxy_extraction"]
    minimum_support = int(extraction["minimum_obstacle_support_points"])
    minimum_volume = float(extraction["minimum_obstacle_volume_m3"])
    removed_small = 0
    candidates: list[dict[str, Any]] = []
    for item in raw_objects:
        if item["support_count"] < minimum_support or bbox_volume(item) < minimum_volume:
            removed_small += 1
        else:
            candidates.append(dict(item))

    merged: list[dict[str, Any]] = []
    merge_distance = float(extraction["obstacle_merge_distance_m"])

    def should_merge(left: dict[str, Any], right: dict[str, Any]) -> bool:
        if bounds_gap(left["bounds_m"], right["bounds_m"]) > merge_distance:
            return False
        left_volume = bbox_volume(left)
        right_volume = bbox_volume(right)
        overlap = [
            min(left["bounds_m"]["max"][axis], right["bounds_m"]["max"][axis])
            - max(left["bounds_m"]["min"][axis], right["bounds_m"]["min"][axis])
            for axis in range(3)
        ]
        if not all(value > 0 for value in overlap):
            return True
        overlap_volume = math.prod(overlap)
        small_volume = min(left_volume, right_volume)
        large_to_small = max(left_volume, right_volume) / max(small_volume, 1e-9)
        # A broad residual component containing a separate object is not a
        # contact/merge candidate merely because its AABB overlaps it.
        if large_to_small > 8.0 and overlap_volume / small_volume > 0.8:
            return False
        return True

    changed = True
    while changed:
        changed = False
        next_objects: list[dict[str, Any]] = []
        while candidates:
            current = candidates.pop()
            match_index = next(
                (
                    index
                    for index, item in enumerate(next_objects)
                    if should_merge(current, item)
                ),
                None,
            )
            if match_index is None:
                next_objects.append(current)
                continue
            other = next_objects.pop(match_index)
            left_count = int(current["support_count"])
            right_count = int(other["support_count"])
            total = left_count + right_count
            current["support_count"] = total
            current["centroid"] = [
                (left_count * current["centroid"][axis] + right_count * other["centroid"][axis]) / total
                for axis in range(3)
            ]
            current["bounds_m"] = {
                "min": [min(current["bounds_m"]["min"][axis], other["bounds_m"]["min"][axis]) for axis in range(3)],
                "max": [max(current["bounds_m"]["max"][axis], other["bounds_m"]["max"][axis]) for axis in range(3)],
            }
            next_objects.append(current)
            changed = True
        candidates = next_objects
    merged.extend(candidates)
    return merged, removed_small


def classify_intersections(objects: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, int]:
    tolerance = float(config["future_proxy_extraction"]["contact_tolerance_m"])
    valid_contact_count = 0
    invalid_penetration_count = 0
    overlapping_surface_count = 0
    for index, left in enumerate(objects):
        for right in objects[index + 1 :]:
            overlap = [
                min(left["bounds_m"]["max"][axis], right["bounds_m"]["max"][axis])
                - max(left["bounds_m"]["min"][axis], right["bounds_m"]["min"][axis])
                for axis in range(3)
            ]
            if any(value < -tolerance for value in overlap):
                continue
            if any(abs(value) <= tolerance for value in overlap):
                valid_contact_count += 1
                continue
            left_plane = left.get("plane_normal") is not None
            right_plane = right.get("plane_normal") is not None
            overlap_volume = math.prod(max(0.0, value) for value in overlap)
            minimum_volume = min(bbox_volume(left), bbox_volume(right))
            overlap_ratio = overlap_volume / max(minimum_volume, 1e-9)
            if left_plane and right_plane:
                normal_dot = abs(dot(tuple(left["plane_normal"]), tuple(right["plane_normal"])))
                if left["semantic_class"] == right["semantic_class"] and normal_dot > 0.95:
                    overlapping_surface_count += 1
                elif normal_dot < 0.5 and overlap_ratio <= 0.5:
                    valid_contact_count += 1
                else:
                    invalid_penetration_count += 1
            elif left_plane or right_plane:
                plane = left if left_plane else right
                obstacle = right if left_plane else left
                normal = tuple(plane["plane_normal"])
                extent = [obstacle["bounds_m"]["max"][axis] - obstacle["bounds_m"]["min"][axis] for axis in range(3)]
                half_extent = sum(abs(normal[axis]) * extent[axis] / 2.0 for axis in range(3))
                signed_distance = dot(normal, tuple(obstacle["centroid"])) + float(plane["plane_offset"])
                penetration_depth = half_extent - abs(signed_distance)
                if penetration_depth <= tolerance * 3.0:
                    valid_contact_count += 1
                else:
                    invalid_penetration_count += 1
            else:
                invalid_penetration_count += 1
    return {
        "valid_contact_count": valid_contact_count,
        "invalid_penetration_count": invalid_penetration_count,
        "overlapping_surface_count": overlapping_surface_count,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a verified automatic proxy mesh.")
    parser.add_argument("--fused_ply", type=Path, required=True)
    parser.add_argument("--world_alignment", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--dense_quality", type=Path, required=True)
    parser.add_argument("--output_dir", type=Path, required=True)
    parser.add_argument("--mode", choices=("semantic", "surface_mesh"), default="semantic")
    parser.add_argument("--surface_ply", type=Path, help="COLMAP delaunay_mesher PLY for --mode surface_mesh.")
    parser.add_argument("--asset_rotation_quaternion_xyzw", default="1,0,0,0")
    args = parser.parse_args()
    quality = json.loads(args.dense_quality.read_text(encoding="utf-8"))
    dense_status = quality.get("status")
    if dense_status != "verified":
        raise RuntimeError("Refusing proxy generation because dense geometry is not verified.")
    config = json.loads(args.config.read_text(encoding="utf-8"))
    dense_quality_reference = "sha256:" + hashlib.sha256(args.dense_quality.read_bytes()).hexdigest()
    if args.mode == "surface_mesh":
        if args.surface_ply is None or not args.surface_ply.is_file():
            raise RuntimeError("--surface_ply is required for --mode surface_mesh.")
        dense_points = load_world_points(args.fused_ply, args.world_alignment, args.asset_rotation_quaternion_xyzw)
        vertices, faces = read_surface_ply(args.surface_ply)
        scale, rotation, translation, _ = load_transform(args.world_alignment, args.asset_rotation_quaternion_xyzw)
        vertices = [
            tuple(scale * value + translation[axis] for axis, value in enumerate(matrix_vector(rotation, point)))
            for point in vertices
        ]
        args.output_dir.mkdir(parents=True, exist_ok=True)
        surface_quality, surface_metadata = surface_mesh_quality(
            vertices, faces, dense_points, config, quality, args.output_dir / "proxy.glb"
        )
        surface_metadata["world_transform"] = {
            "source": str(args.world_alignment),
            "formula": "world = scale * world_rotation * asset_rotation * dense_aligned + translation",
            "asset_rotation_quaternion_xyzw": [float(value) for value in args.asset_rotation_quaternion_xyzw.split(",")],
        }
        surface_quality.setdefault("provenance", {})["source_dense_quality"] = dense_quality_reference
        surface_quality["source_dense_quality"] = dense_quality_reference
        surface_metadata.setdefault("provenance", {}).update({
            "source_dense_quality": dense_quality_reference,
            "source_dense_cloud": str(args.fused_ply),
        })
        (args.output_dir / "proxy_metadata.json").write_text(json.dumps(surface_metadata, indent=2) + "\n", encoding="utf-8")
        (args.output_dir / "proxy_quality.json").write_text(json.dumps(surface_quality, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(surface_quality, indent=2))
        if surface_quality["status"] != "verified":
            raise SystemExit(2)
        return
    points = load_world_points(args.fused_ply, args.world_alignment, args.asset_rotation_quaternion_xyzw)
    voxel_size = float(config["future_proxy_extraction"]["voxel_size_m"])
    downsampled, _ = voxel_downsample(points, voxel_size)
    planes, residual = extract_planes(downsampled, config)
    raw_plane_objects = classify_planes(planes, downsampled, config)
    objects = merge_plane_segments(raw_plane_objects, config)
    minimum_volume = float(config["future_proxy_extraction"]["minimum_obstacle_volume_m3"])
    residual_centers = {
        tuple(math.floor(value / voxel_size) for value in downsampled[index]): downsampled[index]
        for index in residual
    }
    raw_obstacle_objects: list[dict[str, Any]] = []
    residual_groups = components(residual_centers)
    raw_obstacle_clusters = len(residual_groups)
    for group in residual_groups:
        if len(group) < 8:
            continue
        bounds = {"min": [min(point[axis] for point in group) for axis in range(3)], "max": [max(point[axis] for point in group) for axis in range(3)]}
        extent = [bounds["max"][axis] - bounds["min"][axis] for axis in range(3)]
        if math.prod(extent) >= minimum_volume:
            raw_obstacle_objects.append({"semantic_class": "large_obstacle", "normal": None, "plane_normal": None, "centroid": [sum(point[axis] for point in group) / len(group) for axis in range(3)], "bounds_m": bounds, "support_count": len(group)})
    merged_obstacles, removed_small_cluster_count = merge_obstacles(raw_obstacle_objects, config)
    removed_small_cluster_count += raw_obstacle_clusters - len(raw_obstacle_objects)
    objects.extend(merged_obstacles)
    for index, item in enumerate(objects):
        item["id"] = f"proxy_{item['semantic_class']}_{index:03d}"
    explained_point_count = sum(item["support_count"] for item in objects)
    plane_residuals = [
        item["surface_residual_m"]
        for item in objects
        if item.get("surface_residual_m")
    ]
    residual_mean = (
        sum(item["mean"] for item in plane_residuals) / len(plane_residuals)
        if plane_residuals
        else None
    )
    residual_median_values = sorted(item["median"] for item in plane_residuals)
    residual_median = (
        residual_median_values[len(residual_median_values) // 2]
        if residual_median_values
        else None
    )
    downsampled_count = len(downsampled)
    explanation_ratio = explained_point_count / downsampled_count if downsampled_count else 0.0
    plane_counts = {
        "raw_floor_segments": sum(item["semantic_class"] == "floor" for item in raw_plane_objects),
        "merged_floor_objects": sum(item["semantic_class"] == "floor" for item in objects),
        "raw_wall_segments": sum(item["semantic_class"] == "wall" for item in raw_plane_objects),
        "merged_wall_objects": sum(item["semantic_class"] == "wall" for item in objects),
        "raw_ceiling_segments": sum(item["semantic_class"] == "ceiling" for item in raw_plane_objects),
        "merged_ceiling_objects": sum(item["semantic_class"] == "ceiling" for item in objects),
    }
    wall_objects = [item for item in objects if item["semantic_class"] == "wall"]
    horizontal_objects = [item for item in objects if item["semantic_class"] in ("floor", "ceiling")]
    plane_threshold = float(config["future_proxy_extraction"]["plane_distance_threshold_m"]) * 2.0
    vertical_points = [
        point for point in downsampled
        if not any(abs(dot(tuple(item["plane_normal"]), point) + item["plane_offset"]) <= plane_threshold for item in horizontal_objects)
    ]
    def wall_contains(point: Point, item: dict[str, Any]) -> bool:
        normal = tuple(item["plane_normal"])
        if abs(dot(normal, point) + item["plane_offset"]) > plane_threshold:
            return False
        bounds = item["bounds_m"]
        return all(
            abs(normal[axis]) > 0.7 or bounds["min"][axis] - plane_threshold <= point[axis] <= bounds["max"][axis] + plane_threshold
            for axis in range(3)
        )
    wall_explained = sum(1 for point in vertical_points if any(wall_contains(point, item) for item in wall_objects))
    wall_coverage_ratio = wall_explained / len(vertical_points) if vertical_points else 0.0
    wall_normals = [math.degrees(math.atan2(item["plane_normal"][2], item["plane_normal"][0])) % 180.0 for item in wall_objects]
    wall_angular_coverage = max(wall_normals) - min(wall_normals) if wall_normals else 0.0
    if vertical_points:
        vertical_min_y = min(point[1] for point in vertical_points)
        vertical_max_y = max(point[1] for point in vertical_points)
        vertical_height = max(1e-9, vertical_max_y - vertical_min_y)
        covered_y = sum(
            max(0.0, min(item["bounds_m"]["max"][1], vertical_max_y) - max(item["bounds_m"]["min"][1], vertical_min_y))
            for item in wall_objects
        )
        wall_height_coverage_ratio = min(1.0, covered_y / vertical_height)
    else:
        wall_height_coverage_ratio = 0.0
    intersection_metrics = classify_intersections(objects, config)
    quality_metrics = {
        **plane_counts,
        "raw_obstacle_clusters": raw_obstacle_clusters,
        "merged_obstacle_count": len(merged_obstacles),
        "removed_small_cluster_count": removed_small_cluster_count,
        "obstacle_volume_total_m3": sum(bbox_volume(item) for item in merged_obstacles),
        "vertical_dense_point_count": len(vertical_points),
        "wall_explained_point_count": wall_explained,
        "wall_coverage_ratio": wall_coverage_ratio,
        "wall_angular_coverage_deg": wall_angular_coverage,
        "wall_height_coverage_ratio": wall_height_coverage_ratio,
        "downsampled_point_count": downsampled_count,
        "explained_point_count": explained_point_count,
        "dense_explanation_ratio": explanation_ratio,
        "unexplained_point_ratio": 1.0 - explanation_ratio,
        "surface_residual_m": {
            "mean": residual_mean,
            "median": residual_median,
            "max": max((item["max"] for item in plane_residuals), default=None),
        },
        **intersection_metrics,
    }
    counts = {
        name: sum(item["semantic_class"] == name for item in objects)
        for name in ("floor", "wall", "ceiling", "large_obstacle")
    }
    measures = []
    for item in objects:
        extent = [
            max(0.0, item["bounds_m"]["max"][axis] - item["bounds_m"]["min"][axis])
            for axis in range(3)
        ]
        measures.append({
            "id": item["id"],
            "semantic_class": item["semantic_class"],
            "support_count": item["support_count"],
            "extent_m": extent,
            "area_m2": max(extent[0] * extent[1], extent[0] * extent[2], extent[1] * extent[2]),
            "volume_m3": math.prod(extent),
        })
    proxy_gate = config.get("proxy_quality_gate", {})
    proxy_failures = [
        f"missing_{name}" for name in ("floor", "wall", "ceiling") if counts[name] == 0
    ]
    if explanation_ratio < float(proxy_gate.get("minimum_explanation_ratio", 0.5)):
        proxy_failures.append("insufficient_proxy_explanation")
    if residual_mean is None or residual_mean > float(proxy_gate.get("maximum_surface_residual_m", 0.05)):
        proxy_failures.append("excessive_proxy_surface_residual")
    if quality_metrics["invalid_penetration_count"] > int(proxy_gate.get("maximum_invalid_penetration_count", 0)):
        proxy_failures.append("excessive_proxy_intersections")
    if wall_coverage_ratio < float(proxy_gate.get("minimum_wall_coverage_ratio", 0.5)):
        proxy_failures.append("insufficient_wall_coverage")
    if wall_height_coverage_ratio < float(proxy_gate.get("minimum_wall_height_coverage_ratio", 0.5)):
        proxy_failures.append("insufficient_wall_height_coverage")
    for semantic in ("floor", "wall", "ceiling"):
        if plane_counts[f"merged_{semantic}_objects"] > int(proxy_gate.get(f"maximum_{semantic}_objects", 3 if semantic != "wall" else 8)):
            proxy_failures.append(f"excessive_{semantic}_segmentation")
    if len(merged_obstacles) > int(proxy_gate.get("maximum_merged_obstacle_count", 30)):
        proxy_failures.append("excessive_obstacle_segmentation")
    semantic_status = "verified" if not proxy_failures else "unverified"
    quality = {
        "schema_version": "1.0.0",
        "stage": "semantic_proxy",
        "proxy_kind": "semantic",
        "status": semantic_status,
        "capabilities": {
            "measurement": False,
            "surface_placement": False,
            "fov": False,
            "occlusion": False,
            "semantic_object_query": semantic_status == "verified",
        },
        "failure_reasons": proxy_failures,
        "warnings": [],
        "source_dense_quality": dense_quality_reference,
        "provenance": {
            "source_dense_quality": dense_quality_reference,
            "semantic_proxy_status": semantic_status,
        },
        "proxy_quality": {
        "detected_counts": counts,
        "object_count": len(objects),
        "object_measures": measures,
        **quality_metrics,
        },
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_glb(objects, args.output_dir / "proxy.glb")
    metadata = {"schema_version": "1.0.0", "unit": "meter", "coordinate_system": {"up_axis": "Y"}, "source_dense_cloud": str(args.fused_ply), "world_transform": {"source": str(args.world_alignment), "formula": "world = scale * world_rotation * asset_rotation * dense_aligned + translation", "asset_rotation_quaternion_xyzw": [float(value) for value in args.asset_rotation_quaternion_xyzw.split(",")]}, "generation_parameters": config["future_proxy_extraction"], "objects": objects, "quality_metrics": quality_metrics, "provenance": {"downsampled_point_count": len(downsampled), "residual_point_count": len(residual), "source_dense_quality": dense_quality_reference}}
    (args.output_dir / "proxy_metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    (args.output_dir / "proxy_quality.json").write_text(json.dumps(quality, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
