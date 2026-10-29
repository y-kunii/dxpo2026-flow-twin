"""SPZ import, immutable Scene Package, and scene object service extracted from the PreviewService compatibility facade."""

from __future__ import annotations

from .common import *  # noqa: F403


class SceneImportService:
    _PLY_TYPE_FORMATS = {
        "char": "b", "int8": "b", "uchar": "B", "uint8": "B",
        "short": "h", "int16": "h", "ushort": "H", "uint16": "H",
        "int": "i", "int32": "i", "uint": "I", "uint32": "I",
        "float": "f", "float32": "f", "double": "d", "float64": "d",
    }

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _validate_spz(path: Path) -> dict[str, Any]:
        """Validate the SPZ container without expanding it into memory."""
        if path.suffix.lower() not in GAUSSIAN_SUFFIXES:
            raise ValueError("gaussian_asset_extension_invalid")
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError("gaussian_asset_empty")
        with path.open("rb") as source:
            signature = source.read(2)
        if signature != b"\x1f\x8b":
            raise ValueError("spz_gzip_header_invalid")
        try:
            with gzip.open(path, "rb") as decoded:
                header = decoded.read(12)
        except (OSError, EOFError) as error:
            raise ValueError(f"spz_gzip_decode_failed:{error}") from error
        if len(header) < 12 or header[:4] != b"NGSP":
            raise ValueError("spz_magic_invalid")
        version, gaussian_count = struct.unpack("<II", header[4:12])
        if version <= 0 or gaussian_count <= 0:
            raise ValueError("spz_header_values_invalid")
        return {
            "status": "verified",
            "format": "spz",
            "container": "gzip",
            "magic": "NGSP",
            "version": version,
            "gaussian_count": gaussian_count,
            "size_bytes": path.stat().st_size,
            "sha256": SceneImportService._sha256(path),
        }

    @classmethod
    def _validate_ply(cls, path: Path) -> dict[str, Any]:
        """Validate a standard 3DGS PLY without loading the payload in memory."""
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError("gaussian_asset_empty")
        with path.open("rb") as source:
            header_bytes = source.read(min(path.stat().st_size, 1024 * 1024))
        marker = b"end_header\n"
        header_end = header_bytes.find(marker)
        if header_end < 0:
            marker = b"end_header\r\n"
            header_end = header_bytes.find(marker)
        if header_end < 0:
            raise ValueError("ply_header_missing_or_too_large")
        data_offset = header_end + len(marker)
        try:
            lines = header_bytes[:data_offset].decode("ascii").splitlines()
        except UnicodeDecodeError as error:
            raise ValueError("ply_header_not_ascii") from error
        if not lines or lines[0].strip() != "ply":
            raise ValueError("ply_magic_invalid")
        storage_format = ""
        vertex_count = 0
        current_element = ""
        vertex_properties: list[tuple[str, str]] = []
        for line in lines[1:]:
            tokens = line.strip().split()
            if not tokens or tokens[0] in {"comment", "obj_info", "end_header"}:
                continue
            if tokens[0] == "format" and len(tokens) == 3 and tokens[2] == "1.0":
                storage_format = tokens[1]
            elif tokens[0] == "element" and len(tokens) == 3:
                current_element = tokens[1]
                if current_element == "vertex":
                    try:
                        vertex_count = int(tokens[2])
                    except ValueError as error:
                        raise ValueError("ply_vertex_count_invalid") from error
            elif tokens[0] == "property" and current_element == "vertex":
                if len(tokens) != 3 or tokens[1] == "list":
                    raise ValueError("ply_vertex_property_invalid")
                if tokens[1] not in cls._PLY_TYPE_FORMATS:
                    raise ValueError("ply_vertex_property_type_unsupported")
                vertex_properties.append((tokens[2], cls._PLY_TYPE_FORMATS[tokens[1]]))
        if storage_format not in {"ascii", "binary_little_endian"}:
            raise ValueError("ply_storage_format_unsupported")
        if vertex_count <= 0 or not vertex_properties:
            raise ValueError("ply_vertex_schema_invalid")
        names = {name for name, _format in vertex_properties}
        geometry = {"x", "y", "z", "opacity", "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"}
        colors = {"f_dc_0", "f_dc_1", "f_dc_2"}
        byte_colors = {"red", "green", "blue"}
        if not geometry.issubset(names) or not (colors.issubset(names) or byte_colors.issubset(names)):
            raise ValueError("ply_3dgs_properties_missing")
        finite_fields = sorted(geometry | (colors if colors.issubset(names) else set()))
        if storage_format == "binary_little_endian":
            record = struct.Struct("<" + "".join(item[1] for item in vertex_properties))
            expected_end = data_offset + record.size * vertex_count
            if expected_end > path.stat().st_size:
                raise ValueError("ply_vertex_payload_truncated")
            finite_indexes = [index for index, (name, _format) in enumerate(vertex_properties) if name in finite_fields]
            with path.open("rb") as source:
                source.seek(data_offset)
                for _index in range(vertex_count):
                    raw = source.read(record.size)
                    if len(raw) != record.size:
                        raise ValueError("ply_vertex_payload_truncated")
                    values = record.unpack(raw)
                    if not all(math.isfinite(float(values[index])) for index in finite_indexes):
                        raise ValueError("ply_vertex_non_finite")
        else:
            indexes = {name: index for index, (name, _format) in enumerate(vertex_properties)}
            with path.open("rb") as source:
                source.seek(data_offset)
                for _index in range(vertex_count):
                    line = source.readline()
                    if not line:
                        raise ValueError("ply_vertex_payload_truncated")
                    try:
                        values = line.decode("ascii").split()
                        valid = len(values) >= len(vertex_properties) and all(
                            math.isfinite(float(values[indexes[name]])) for name in finite_fields
                        )
                    except (UnicodeDecodeError, ValueError) as error:
                        raise ValueError("ply_vertex_value_invalid") from error
                    if not valid:
                        raise ValueError("ply_vertex_non_finite")
        return {
            "status": "verified",
            "format": "ply",
            "container": storage_format,
            "version": 1,
            "gaussian_count": vertex_count,
            "properties": [name for name, _format in vertex_properties],
            "finite_fields": finite_fields,
            "size_bytes": path.stat().st_size,
            "sha256": cls._sha256(path),
        }

    @classmethod
    def _validate_rad(cls, path: Path) -> dict[str, Any]:
        """Validate Spark RAD metadata and all bounded chunk ranges."""
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError("gaussian_asset_empty")
        with path.open("rb") as source:
            if source.read(4) != b"RAD0":
                raise ValueError("rad_magic_invalid")
            length_bytes = source.read(4)
            if len(length_bytes) != 4:
                raise ValueError("rad_metadata_length_missing")
            metadata_length = struct.unpack("<I", length_bytes)[0]
            if metadata_length <= 0 or metadata_length > 16 * 1024 * 1024:
                raise ValueError("rad_metadata_length_invalid")
            metadata_bytes = source.read(metadata_length)
        if len(metadata_bytes) != metadata_length:
            raise ValueError("rad_metadata_truncated")
        try:
            metadata = json.loads(metadata_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("rad_metadata_invalid") from error
        if not isinstance(metadata, dict):
            raise ValueError("rad_metadata_invalid")
        version = metadata.get("version")
        gaussian_count = metadata.get("count")
        chunks = metadata.get("chunks")
        if not isinstance(version, int) or version <= 0 or not isinstance(gaussian_count, int) or gaussian_count <= 0:
            raise ValueError("rad_header_values_invalid")
        if metadata.get("type") not in {"gsplat", "csplat"} or not isinstance(chunks, list) or not chunks:
            raise ValueError("rad_chunk_table_invalid")
        expected_offset = 0
        for chunk in chunks:
            if not isinstance(chunk, dict) or not isinstance(chunk.get("offset"), int) or not isinstance(chunk.get("bytes"), int):
                raise ValueError("rad_chunk_table_invalid")
            if chunk["offset"] != expected_offset or chunk["bytes"] <= 0:
                raise ValueError("rad_chunk_table_not_contiguous")
            expected_offset += chunk["bytes"]
        if metadata.get("allChunkBytes") != expected_offset:
            raise ValueError("rad_chunk_bytes_mismatch")
        payload_capacity = path.stat().st_size - (8 + metadata_length)
        if expected_offset > payload_capacity or payload_capacity - expected_offset > 7:
            raise ValueError("rad_payload_size_invalid")
        return {
            "status": "verified",
            "format": "rad",
            "container": "rad0",
            "magic": "RAD0",
            "version": version,
            "gaussian_count": gaussian_count,
            "chunk_count": len(chunks),
            "paged": bool(metadata.get("lodTree")),
            "size_bytes": path.stat().st_size,
            "sha256": cls._sha256(path),
        }

    @classmethod
    def _validate_gaussian_asset(cls, path: Path) -> dict[str, Any]:
        suffix = path.suffix.lower()
        if suffix not in GAUSSIAN_SUFFIXES:
            raise ValueError("gaussian_asset_extension_invalid")
        if suffix == ".spz":
            return cls._validate_spz(path)
        if suffix == ".ply":
            return cls._validate_ply(path)
        return cls._validate_rad(path)

    @staticmethod
    def _copy_file(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.partial")
        try:
            shutil.copyfile(source, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _import_package_id(
        self,
        reconstruction_id: str,
        asset_hash: str,
        dataset_fingerprint: str | None = None,
    ) -> str:
        # v4 is a self-contained dataset edition.  The ID covers both the 3DGS
        # asset and the mutable source snapshot (including Field Memory), so a
        # later data export creates a new immutable package instead of changing
        # an existing one in place.
        value = self_contained_package_id(
            reconstruction_id,
            asset_hash,
            dataset_fingerprint or "",
        )
        if not JOB_ID_RE.fullmatch(value):
            raise ValueError("scene_package_id_invalid")
        return value

    @staticmethod
    def _rotate_point(
        values: list[Any],
        quaternion_xyzw: list[Any] | tuple[Any, ...],
        error_label: str,
        quaternion_field: str,
    ) -> list[float]:
        """Rotate a point with an already validated xyzw unit quaternion."""
        if len(values) != 3:
            raise ValueError(error_label)
        x, y, z = (float(value) for value in values)
        qx, qy, qz, qw = require_unit_quaternion_xyzw(quaternion_xyzw, quaternion_field)
        # Quaternion-vector multiplication: q * [v, 0] * conjugate(q).
        tx = 2.0 * (qy * z - qz * y)
        ty = 2.0 * (qz * x - qx * z)
        tz = 2.0 * (qx * y - qy * x)
        return [
            x + qw * tx + (qy * tz - qz * ty),
            y + qw * ty + (qz * tx - qx * tz),
            z + qw * tz + (qx * ty - qy * tx),
        ]

    @classmethod
    def _world_point(
        cls,
        values: list[Any],
        transform: dict[str, Any],
        *,
        asset_rotation_quaternion_xyzw: list[Any] | tuple[Any, ...] = (0.0, 0.0, 0.0, 1.0),
        error_label: str = "camera_trajectory_point_invalid",
    ) -> list[float]:
        """Map horizontalized COLMAP coordinates through asset and world space."""
        asset = cls._rotate_point(
            values,
            asset_rotation_quaternion_xyzw,
            error_label,
            "spark_spz_decoder.rotation_quaternion_xyzw",
        )
        scale = float(transform.get("scale", 1.0))
        rotation = transform.get("rotation_quaternion_xyzw", [0.0, 0.0, 0.0, 1.0])
        translation = transform.get("translation_m", [0.0, 0.0, 0.0])
        if not isinstance(rotation, list) or len(rotation) != 4 or not isinstance(translation, list) or len(translation) != 3:
            raise ValueError("world_alignment_transform_invalid")
        rx, ry, rz = cls._rotate_point(
            asset,
            rotation,
            "world_alignment_transform_invalid",
            "world_alignment.generated_to_world.rotation_quaternion_xyzw",
        )
        return [
            scale * rx + float(translation[0]),
            scale * ry + float(translation[1]),
            scale * rz + float(translation[2]),
        ]

    @classmethod
    def _gaussian_corners_in_viewer_world(
        cls,
        corners: dict[str, Any],
        world_alignment: dict[str, Any],
        asset_rotation_quaternion_xyzw: tuple[float, float, float, float],
        coordinate_contract_id: str,
    ) -> dict[str, Any]:
        """Reproject metric tag annotations into Gaussian viewer coordinates.

        Historical MRP annotations can predate the explicit Spark decoder
        convention.  Their ``xyz_aligned`` values are the canonical source;
        values already named ``xyz_world_m`` are deliberately not trusted here.
        Missing aligned source coordinates fail closed instead of drawing a
        plausible-looking tag in the wrong place.
        """
        transform = world_alignment.get("generated_to_world", {}) if isinstance(world_alignment, dict) else {}
        if not isinstance(transform, dict):
            raise ValueError("world_alignment_transform_invalid")
        def project_tag(source_tag: dict[str, Any]) -> dict[str, Any]:
            source_points = source_tag.get("corners_world_m", [])
            if not isinstance(source_points, list) or len(source_points) != 4:
                raise ValueError("spz_apriltag_corners_not_complete")
            records: list[dict[str, Any]] = []
            by_index: dict[int, list[float]] = {}
            for source in source_points:
                if not isinstance(source, dict):
                    raise ValueError("spz_apriltag_aligned_coordinates_missing")
                index = source.get("corner_index")
                aligned = source.get("xyz_aligned")
                if not isinstance(index, int) or index not in {0, 1, 2, 3} or not isinstance(aligned, list) or len(aligned) != 3:
                    raise ValueError("spz_apriltag_aligned_coordinates_missing")
                record = dict(source)
                asset = cls._rotate_point(
                    aligned,
                    asset_rotation_quaternion_xyzw,
                    "gaussian_apriltag_aligned_coordinates_missing",
                    "gaussian_asset_decoder.rotation_quaternion_xyzw",
                )
                world = cls._world_point(
                    aligned,
                    transform,
                    asset_rotation_quaternion_xyzw=asset_rotation_quaternion_xyzw,
                    error_label="gaussian_apriltag_aligned_coordinates_missing",
                )
                record["xyz_asset"] = asset
                record["xyz_world_m"] = world
                records.append(record)
                by_index[index] = world
            if set(by_index) != {0, 1, 2, 3}:
                raise ValueError("spz_apriltag_corners_not_complete")

            def measurements(pairs: list[tuple[int, int]]) -> list[dict[str, Any]]:
                return [{
                    "corners": [first, second],
                    "distance_m": sum((by_index[first][axis] - by_index[second][axis]) ** 2 for axis in range(3)) ** 0.5,
                } for first, second in pairs]

            return {
                **source_tag,
                "corners_world_m": sorted(records, key=lambda item: int(item["corner_index"])),
                "sides": measurements([(0, 1), (1, 2), (2, 3), (3, 0)]),
                "diagonals": measurements([(0, 2), (1, 3)]),
            }

        raw_tags = corners.get("tags")
        source_tags = raw_tags if isinstance(raw_tags, list) and raw_tags else [corners]
        if not all(isinstance(tag, dict) for tag in source_tags):
            raise ValueError("spz_apriltag_corners_not_complete")
        tags = [project_tag(tag) for tag in source_tags]
        primary = tags[0]

        stages = dict(corners.get("coordinate_stages", {})) if isinstance(corners.get("coordinate_stages"), dict) else {}
        stages["source_asset_rotation_quaternion_xyzw"] = stages.get("asset_rotation_quaternion_xyzw")
        stages["asset_rotation_quaternion_xyzw"] = list(asset_rotation_quaternion_xyzw)
        stages["asset_coordinate_contract"] = coordinate_contract_id
        return {
            **corners,
            **primary,
            "coordinate_stages": stages,
            "tags": tags,
        }

    @classmethod
    def _spz_corners_in_viewer_world(cls, corners: dict[str, Any], world_alignment: dict[str, Any]) -> dict[str, Any]:
        """Backward-compatible SPZ contract wrapper."""
        return cls._gaussian_corners_in_viewer_world(
            corners,
            world_alignment,
            SPARK_SPZ_DECODER_ROTATION_XYZW,
            SPARK_SPZ_COORDINATE_CONTRACT,
        )

    @staticmethod
    def _fixed_tag_pose_from_viewer_corners(corners: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Derive one verified ``T_gs_fixedTag`` from its four world corners."""
        family = corners.get("family")
        tag_id = corners.get("id")
        points = corners.get("corners_world_m")
        if not isinstance(family, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", family):
            raise ValueError("spz_fixed_tag_family_invalid")
        if not isinstance(tag_id, int) or tag_id < 0:
            raise ValueError("spz_fixed_tag_id_invalid")
        if not isinstance(points, list) or len(points) != 4:
            raise ValueError("spz_fixed_tag_corners_not_complete")

        by_index: dict[int, list[float]] = {}
        for point in points:
            if not isinstance(point, dict) or not isinstance(point.get("corner_index"), int):
                raise ValueError("spz_fixed_tag_corners_not_complete")
            index = point["corner_index"]
            xyz = point.get("xyz_world_m")
            if index not in {0, 1, 2, 3} or index in by_index or not isinstance(xyz, list) or len(xyz) != 3:
                raise ValueError("spz_fixed_tag_corners_not_complete")
            try:
                values = [float(value) for value in xyz]
            except (TypeError, ValueError) as error:
                raise ValueError("spz_fixed_tag_corners_non_finite") from error
            if not all(math.isfinite(value) for value in values):
                raise ValueError("spz_fixed_tag_corners_non_finite")
            by_index[index] = values
        if set(by_index) != {0, 1, 2, 3}:
            raise ValueError("spz_fixed_tag_corners_not_complete")

        def subtract(left: list[float], right: list[float]) -> list[float]:
            return [left[index] - right[index] for index in range(3)]

        def add(left: list[float], right: list[float]) -> list[float]:
            return [left[index] + right[index] for index in range(3)]

        def scale(vector: list[float], amount: float) -> list[float]:
            return [value * amount for value in vector]

        def cross(left: list[float], right: list[float]) -> list[float]:
            return [
                left[1] * right[2] - left[2] * right[1],
                left[2] * right[0] - left[0] * right[2],
                left[0] * right[1] - left[1] * right[0],
            ]

        def normalize(vector: list[float]) -> list[float]:
            length = math.sqrt(sum(value * value for value in vector))
            if not math.isfinite(length) or length <= 1e-9:
                raise ValueError("spz_fixed_tag_corners_degenerate")
            return scale(vector, 1.0 / length)

        p0, p1, p2, p3 = (by_index[index] for index in range(4))
        x_axis = normalize(scale(add(subtract(p1, p0), subtract(p2, p3)), 0.5))
        y_approximation = normalize(scale(add(subtract(p3, p0), subtract(p2, p1)), 0.5))
        z_axis = normalize(cross(x_axis, y_approximation))
        y_axis = normalize(cross(z_axis, x_axis))
        m00, m01, m02 = x_axis[0], y_axis[0], z_axis[0]
        m10, m11, m12 = x_axis[1], y_axis[1], z_axis[1]
        m20, m21, m22 = x_axis[2], y_axis[2], z_axis[2]
        trace = m00 + m11 + m22
        if trace > 0:
            q_scale = 0.5 / math.sqrt(trace + 1.0)
            quaternion = [(m21 - m12) * q_scale, (m02 - m20) * q_scale, (m10 - m01) * q_scale, 0.25 / q_scale]
        elif m00 > m11 and m00 > m22:
            q_scale = 2.0 * math.sqrt(1.0 + m00 - m11 - m22)
            quaternion = [0.25 * q_scale, (m01 + m10) / q_scale, (m02 + m20) / q_scale, (m21 - m12) / q_scale]
        elif m11 > m22:
            q_scale = 2.0 * math.sqrt(1.0 + m11 - m00 - m22)
            quaternion = [(m01 + m10) / q_scale, 0.25 * q_scale, (m12 + m21) / q_scale, (m02 - m20) / q_scale]
        else:
            q_scale = 2.0 * math.sqrt(1.0 + m22 - m00 - m11)
            quaternion = [(m02 + m20) / q_scale, (m12 + m21) / q_scale, 0.25 * q_scale, (m10 - m01) / q_scale]
        magnitude = math.sqrt(sum(value * value for value in quaternion))
        if not math.isfinite(magnitude) or magnitude <= 1e-12:
            raise ValueError("spz_fixed_tag_rotation_invalid")
        center = scale(add(add(p0, p1), add(p2, p3)), 0.25)
        frame_id = f"apriltag_{family}_{tag_id}"
        return frame_id, {
            "translation_m": center,
            "rotation_xyzw": [value / magnitude for value in quaternion],
        }

    @classmethod
    def _fixed_tag_map_from_viewer_corners(
        cls, corners: dict[str, Any], metric_quality: dict[str, Any]
    ) -> dict[str, Any]:
        """Derive all package fixed-tag poses in the SPZ viewer world.

        The historical single-tag annotation remains accepted.  New MRP
        annotations carry ``tags`` and produce one stable fixed frame for each
        configured AprilTag, allowing the camera-side adapter to switch its
        ``header.frame_id`` without moving the scene coordinate system.
        """
        # Scale is solved from these same corners, so it cannot certify their
        # geometry. Dynamic tracking only accepts the independent quality gate
        # recorded in the Metric Reconstruction Package.
        triangulation = metric_quality.get("quality")
        quality_gate = metric_quality.get("quality_gate")
        if (
            metric_quality.get("status") != "verified"
            or not isinstance(triangulation, dict)
            or triangulation.get("status") != "verified"
            or not isinstance(quality_gate, dict)
            or quality_gate.get("status") != "verified"
        ):
            raise ValueError("spz_fixed_tag_metric_quality_not_verified")
        raw_tags = corners.get("tags")
        tags = raw_tags if isinstance(raw_tags, list) and raw_tags else [corners]
        if not all(isinstance(tag, dict) for tag in tags):
            raise ValueError("spz_fixed_tag_corners_not_complete")
        frames: dict[str, dict[str, Any]] = {}
        for tag in tags:
            frame_id, pose = cls._fixed_tag_pose_from_viewer_corners(tag)
            if frame_id in frames:
                raise ValueError("spz_fixed_tag_frame_duplicate")
            frames[frame_id] = pose
        return {
            "schema_version": "1.0.0",
            "coordinate_frame": "gs_world",
            "unit": "meter",
            "rotation_order": "xyzw",
            "metric_quality": {
                "status": "verified",
                "quality_gate": {
                    "status": "verified",
                    "thresholds": quality_gate.get("thresholds"),
                },
            },
            "frames": frames,
        }

    def _ensure_fixed_tag_map(self, package: Path, fixed_tag_map: dict[str, Any]) -> None:
        """Legacy repair helper retained only for explicit maintenance tooling.

        Normal SPZ import never calls this function.  Scene Package IDs are
        immutable snapshots, so a collision must be reported instead of
        patching a package in place.
        """
        manifest_path = package / "scene_manifest.json"
        manifest = read_json(manifest_path, {})
        annotations = manifest.get("annotations") if isinstance(manifest, dict) else None
        if not isinstance(annotations, dict):
            raise ValueError("scene_package_annotations_invalid")
        if annotations.get("fixed_tag_map"):
            return
        staging = package.with_name(f".{package.name}.fixed-tag-map.{os.getpid()}")
        previous = package.with_name(f".{package.name}.fixed-tag-map.previous.{os.getpid()}")
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(previous, ignore_errors=True)
        try:
            shutil.copytree(package, staging, symlinks=True)
            atomic_json(staging / "annotations" / "fixed_tag_map.json", fixed_tag_map)
            staged_manifest = read_json(staging / "scene_manifest.json", {})
            staged_annotations = staged_manifest.setdefault("annotations", {})
            if not isinstance(staged_annotations, dict):
                raise ValueError("scene_package_annotations_invalid")
            staged_annotations["fixed_tag_map"] = "annotations/fixed_tag_map.json"
            atomic_json(staging / "scene_manifest.json", staged_manifest)
            validation = validate_scene_package(staging, str(staged_manifest.get("scene_id", "")))
            if validation.get("status") != "verified":
                raise ValueError("fixed_tag_map_backfill_validation_failed")
            os.replace(package, previous)
            try:
                os.replace(staging, package)
            except Exception:
                os.replace(previous, package)
                raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(previous, ignore_errors=True)

    def _import_camera_presets(
        self,
        mrp_package: Path,
        world_alignment: dict[str, Any],
        corners: dict[str, Any],
        asset_rotation_quaternion_xyzw: tuple[float, float, float, float] = SPARK_SPZ_DECODER_ROTATION_XYZW,
    ) -> dict[str, Any]:
        """Create one neutral preset from verified reconstruction coordinates."""
        trajectory = read_json(mrp_package / "camera_trajectory.json", {})
        centers = trajectory.get("camera_centers_world", []) if isinstance(trajectory, dict) else []
        transform = world_alignment.get("generated_to_world", {}) if isinstance(world_alignment, dict) else {}
        candidate = centers[0] if isinstance(centers, list) and centers else {}
        values = candidate.get("xyz_aligned", candidate.get("xyz_colmap", [0.0, 1.5, 4.0])) if isinstance(candidate, dict) else [0.0, 1.5, 4.0]
        if not isinstance(values, list) or len(values) != 3:
            values = [0.0, 1.5, 4.0]
        position = self._world_point(
            values,
            transform if isinstance(transform, dict) else {},
            asset_rotation_quaternion_xyzw=asset_rotation_quaternion_xyzw,
        )
        tag_sets = corners.get("tags") if isinstance(corners, dict) else None
        sources = tag_sets if isinstance(tag_sets, list) and tag_sets else [corners]
        coordinates = [
            item.get("xyz_world_m")
            for tag in sources
            if isinstance(tag, dict)
            for item in tag.get("corners_world_m", [])
            if isinstance(item, dict)
        ]
        coordinates = [item for item in coordinates if isinstance(item, list) and len(item) == 3]
        look_at = [sum(float(item[index]) for item in coordinates) / len(coordinates) for index in range(3)] if coordinates else [0.0, 0.0, 0.0]
        return {
            "cameras": [{
                "id": "metric_trajectory_start",
                "name": "Metric trajectory start",
                "position_m": position,
                "look_at_m": look_at,
                "up_world": [0.0, 1.0, 0.0],
                "vertical_fov_deg": 60.0,
                "resolution": [1600, 1000],
            }],
        }

    def import_gaussian_asset(
        self,
        filename: str,
        stream: Any,
        content_length: int,
        scene_id: str,
        reconstruction_id: str,
        coordinate_contract_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomically register a validated Gaussian asset with a verified MRP.

        The imported asset is a display-only Scene Package until a Proxy tied to
        this exact reconstruction is verified.  It never reuses a Proxy from a
        different reconstruction.
        """
        if not SCENE_ID_RE.fullmatch(scene_id) or not SCENE_ID_RE.fullmatch(reconstruction_id):
            raise ValueError("invalid_scene_or_reconstruction_id")
        safe_name = Path(filename).name
        if not filename or safe_name != filename or safe_name in {".", ".."}:
            raise ValueError("gaussian_asset_filename_invalid")
        if Path(safe_name).suffix.lower() not in GAUSSIAN_SUFFIXES:
            raise ValueError("gaussian_asset_extension_invalid")
        asset_format = Path(safe_name).suffix.lower().removeprefix(".")
        if asset_format == "spz":
            coordinate_contract_id = coordinate_contract_id or SPARK_SPZ_COORDINATE_CONTRACT
            if coordinate_contract_id != SPARK_SPZ_COORDINATE_CONTRACT:
                raise ValueError("spz_coordinate_contract_invalid")
        elif coordinate_contract_id is None:
            raise ValueError("gaussian_coordinate_contract_required")
        asset_rotation = GAUSSIAN_COORDINATE_CONTRACTS.get(coordinate_contract_id)
        if asset_rotation is None:
            raise ValueError("gaussian_coordinate_contract_invalid")
        if content_length <= 0:
            raise ValueError("gaussian_asset_empty")
        self._require_storage_headroom(content_length, "gaussian_asset_import")

        mrp_package = self.repository_layout.metric_reconstructions / scene_id / reconstruction_id
        mrp_validation = validate_metric_reconstruction_package(
            mrp_package,
            scene_id,
            reconstruction_id,
            source_root=self.root,
            source_verification="presence",
        )
        if mrp_validation.get("status") != "verified":
            raise ValueError("metric_reconstruction_package_not_verified")
        mrp_manifest = read_json(mrp_package / "manifest.json", {})
        if not isinstance(mrp_manifest, dict):
            raise ValueError("metric_reconstruction_manifest_not_found")
        mrp_provenance = mrp_manifest.get("provenance", {})
        project_name = project_name_from_workspace_uri(
            mrp_provenance.get("source_workspace_uri") if isinstance(mrp_provenance, dict) else None
        )
        # New immutable packages require a durable working-project identifier.
        # Older MRPs can lack workspace provenance; their reconstruction ID is
        # a stable compatibility fallback rather than a mutable scene setting.
        project_id = project_name or reconstruction_id
        coordinate = mrp_manifest.get("coordinate", {})
        if not isinstance(coordinate, dict) or coordinate.get("stage") != "horizontalized_colmap":
            raise ValueError("spz_mrp_coordinate_stage_unverified")
        quality = mrp_manifest.get("artifacts", {}).get("quality", {}) if isinstance(mrp_manifest.get("artifacts"), dict) else {}
        metric_uri = quality.get("metric_uri") if isinstance(quality, dict) else None
        metric_quality = read_json(mrp_package / str(metric_uri), {}) if metric_uri else {}
        corners_uri = metric_quality.get("corners_world_uri") if isinstance(metric_quality, dict) else None
        if not isinstance(corners_uri, str):
            raise ValueError("metric_corners_reference_missing")
        corners_source = self.root / safe_relative(corners_uri, "metric_corners_reference")
        world_alignment_source = mrp_package / "config/world_alignment.json"
        if not corners_source.is_file() or not world_alignment_source.is_file():
            raise ValueError("metric_scene_reference_missing")
        world_alignment = read_json(world_alignment_source, {})
        corners = read_json(corners_source, {})
        if world_alignment.get("status") != "verified" or not isinstance(corners, dict):
            raise ValueError("metric_scene_reference_not_verified")
        viewer_corners = self._gaussian_corners_in_viewer_world(
            corners,
            world_alignment,
            asset_rotation,
            coordinate_contract_id,
        )
        metric_tracking_reason: str | None = None
        try:
            fixed_tag_map = self._fixed_tag_map_from_viewer_corners(
                viewer_corners,
                metric_quality if isinstance(metric_quality, dict) else {},
            )
        except ValueError as error:
            if str(error) != "spz_fixed_tag_metric_quality_not_verified":
                raise
            # Older MRPs can still be imported for static SPZ display, but
            # must not expose a fixed tag pose to dynamic tracking.
            fixed_tag_map = None
            metric_tracking_reason = "apriltag_triangulation_quality_not_independently_verified"

        import_id = uuid.uuid4().hex[:12]
        intake = self.imports_root / import_id
        intake.mkdir(parents=True, exist_ok=False)
        source = intake / safe_name
        temporary = source.with_suffix(source.suffix + ".partial")
        remaining = content_length
        try:
            with temporary.open("wb") as output:
                while remaining:
                    block = stream.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ValueError("gaussian_asset_upload_incomplete")
                    output.write(block)
                    remaining -= len(block)
            os.replace(temporary, source)
            asset_validation = self._validate_gaussian_asset(source)
        except Exception:
            temporary.unlink(missing_ok=True)
            shutil.rmtree(intake, ignore_errors=True)
            raise

        dataset_fingerprint = source_fingerprint(self.root, mrp_package)
        package_id = self._import_package_id(
            reconstruction_id,
            str(asset_validation["sha256"]),
            dataset_fingerprint,
        )
        source_package_ids = related_scene_package_ids(self.root, scene_id, reconstruction_id)
        dataset_bytes = estimate_dataset_bytes(self.root, mrp_package)
        # Runtime publication normally hardlinks the immutable canonical
        # payload.  Reserve one dataset plus the uploaded SPZ; per-file copy
        # fallback is still possible when the filesystem rejects hardlinks.
        self._require_storage_headroom(dataset_bytes + content_length, "self_contained_scene_package")
        canonical = self._managed_scene_package_path(scene_id, package_id)
        published = self.root / "web_viewer/app/public/scene_package/runtime" / package_id
        import_token = uuid.uuid4().hex
        staging = canonical.with_name(f".{package_id}.import.{import_token}")
        publish_staging = published.with_name(f".{package_id}.publish.{import_token}")
        committed_canonical = False
        publication_storage: dict[str, int | str] = {}
        try:
            with self._scene_package_import_lock(reconstruction_id, package_id):
                # Re-check only after acquiring the cross-process lock.  A
                # caller may have waited while another process completed the
                # immutable package, in which case this request must not
                # replace either the canonical package or its derived runtime.
                if canonical.exists():
                    raise FileExistsError(f"scene_package_id_already_exists:{package_id}")
                if published.exists():
                    raise FileExistsError(f"viewer_runtime_already_exists:{package_id}")
                self._copy_file(source, staging / "splats" / safe_name)
                self._copy_file(mrp_package / "manifest.json", staging / "metric_reconstruction" / "manifest.json")
                portable_metric_manifest = staging / "metric_reconstruction" / "manifest.json"
                make_metric_manifest_portable(portable_metric_manifest)
                materialize_scene_dataset(
                    self.root,
                    mrp_package,
                    staging / "dataset",
                    scene_id=scene_id,
                    reconstruction_id=reconstruction_id,
                    scene_package_id=package_id,
                    source_package_ids=source_package_ids,
                )
                dataset_manifest = staging / "dataset" / "manifest.json"
                self._copy_file(world_alignment_source, staging / "config" / "world_alignment.json")
                atomic_json(staging / "annotations" / "apriltag_corners_world.json", viewer_corners)
                if fixed_tag_map is not None:
                    atomic_json(staging / "annotations" / "fixed_tag_map.json", fixed_tag_map)
                (staging / "licenses").mkdir(parents=True, exist_ok=True)
                (staging / "licenses" / "scene_notice.md").write_text("External Gaussian asset imported locally.\n", encoding="utf-8")
                atomic_json(
                    staging / "config" / "camera_presets.json",
                    self._import_camera_presets(
                        mrp_package,
                        world_alignment,
                        viewer_corners,
                        asset_rotation,
                    ),
                )
                atomic_json(staging / "assets" / "gaussian_asset.json", {
                    "schema_version": "1.0.0",
                    "asset_kind": "gaussian_asset",
                    "status": "verified",
                    "source_filename": safe_name,
                    "source_format": asset_format,
                    "runtime_format": asset_format,
                    "coordinate_stage": "horizontalized_lfs",
                    "decoder_coordinate_contract": {
                        "id": coordinate_contract_id,
                        "rotation_quaternion_xyzw": list(asset_rotation),
                    },
                    "validation": asset_validation,
                    "metric_reconstruction": {
                        "scene_id": scene_id,
                        "reconstruction_id": reconstruction_id,
                        "manifest_sha256": self._sha256(portable_metric_manifest),
                    },
                })
                package_created_at = utc_now()
                atomic_json(staging / "scene_manifest.json", {
                    "schema_version": "1.3.0",
                    "created_at": package_created_at,
                    "project_id": project_id,
                    "scene_id": scene_id,
                    "reconstruction_id": reconstruction_id,
                    **({"project_name": project_name} if project_name else {}),
                    "scene_package_id": package_id,
                    "unit": "meter",
                    "coordinate_system": {
                        "right_axis": "X", "up_axis": "Y", "forward_axis": "Z",
                        "ros": {"handedness": "right", "x": "forward", "y": "left", "z": "up"},
                        "viewer": {"handedness": "right", "x": "right", "y": "up", "z": "backward"},
                        # p_viewer = R_viewer_ros * p_ros + t_viewer_ros.
                        # ROS (+X forward,+Y left,+Z up) -> Viewer (+X right,+Y up,+Z backward).
                        "T_viewer_ros": {"translation": [0.0, 0.0, 0.0], "rotation_xyzw": [-0.5, 0.5, 0.5, 0.5]},
                    },
                    "dynamic_overlay": {
                        "topic": "/box/relative_transform",
                        "freshness": {"timeout_ms": 500, "on_timeout": "hide", "ordering": "ros_stamp"},
                    },
                    "metric_reconstruction": {
                        "manifest_uri": "metric_reconstruction/manifest.json",
                        "manifest_sha256": self._sha256(portable_metric_manifest),
                    },
                    "dataset": {
                        "manifest_uri": "dataset/manifest.json",
                        "manifest_sha256": self._sha256(dataset_manifest),
                    },
                    "assets": {
                        "splats": [{
                            "id": "primary",
                            "role": "overview",
                            "uri": f"splats/{safe_name}",
                            "lod": bool(asset_validation.get("paged")),
                            "paged": bool(asset_validation.get("paged")),
                            "source_format": asset_format,
                            "runtime_format": asset_format,
                            "coordinate_stage": "horizontalized_lfs",
                            "sha256": asset_validation["sha256"],
                            "metadata_uri": "assets/gaussian_asset.json",
                        }],
                        "proxy_mesh": None,
                        "human_models": [],
                    },
                    "configs": {
                        "world_alignment": "config/world_alignment.json",
                        "camera_presets": "config/camera_presets.json",
                    },
                    "annotations": {
                        "apriltag_corners_world": "annotations/apriltag_corners_world.json",
                        **({"fixed_tag_map": "annotations/fixed_tag_map.json"} if fixed_tag_map is not None else {}),
                    },
                    "licenses": {"notice": "licenses/scene_notice.md"},
                    "capabilities": {
                        "display": True,
                        "measurement": False,
                        "surface_placement": False,
                        "fov": False,
                        "occlusion": False,
                    },
                    "provenance": {
                        "gaussian": {
                            "source_format": asset_format,
                            "source_sha256": asset_validation["sha256"],
                            "runtime_format": asset_format,
                            "runtime_sha256": asset_validation["sha256"],
                        },
                        "coordinate_stage": "horizontalized_lfs",
                        "decoder_coordinate_contract": {
                            "id": coordinate_contract_id,
                            "rotation_quaternion_xyzw": list(asset_rotation),
                            "annotation_source": "xyz_aligned",
                        },
                        "metric_tracking": {
                            "status": "verified" if fixed_tag_map is not None else "unverified",
                            "reason": metric_tracking_reason,
                        },
                        **({"project_name": project_name} if project_name else {}),
                        "external_stage": "LichtFeld Studio",
                    },
                })
                package_validation = validate_scene_package(staging, scene_id)
                if package_validation.get("status") != "verified":
                    raise ValueError("imported_scene_package_validation_failed:" + ",".join(package_validation.get("failure_reasons", [])))
                canonical.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging, canonical)
                committed_canonical = True

                # Runtime is a newly-created derived hardlink tree.  It is never
                # replaced in place, and the pre-lock existence check above
                # makes a stale/foreign runtime a conflict instead of a
                # source for synchronization.
                publication_storage = publish_scene_package_tree(canonical, publish_staging)
                publish_validation = validate_scene_package(publish_staging, scene_id)
                if publish_validation.get("status") != "verified":
                    raise ValueError("viewer_publish_validation_failed")
                published.parent.mkdir(parents=True, exist_ok=True)
                os.replace(publish_staging, published)
        except Exception:
            # A canonical directory is removed only when this invocation
            # created it and failed before its derived runtime was published.
            # The lock excludes other importers while this cleanup occurs.
            if committed_canonical and canonical.exists() and not published.exists():
                shutil.rmtree(canonical, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(publish_staging, ignore_errors=True)
            shutil.rmtree(intake, ignore_errors=True)

        index_warnings = self._refresh_runtime_index()
        index_warnings.extend(self._refresh_repository_catalog())
        return {
            "status": "verified",
            "scene_id": scene_id,
            "reconstruction_id": reconstruction_id,
            "scene_package_id": package_id,
            "asset": asset_validation,
            "capabilities": {
                "display": True,
                "measurement": False,
                "surface_placement": False,
                "fov": False,
                "occlusion": False,
            },
            "canonical_uri": str(canonical.relative_to(self.root)).replace("\\", "/"),
            "publish_uri": str(published.relative_to(self.root)).replace("\\", "/"),
            "publication_storage": publication_storage,
            "viewer_url": f"/?scene_package={package_id}",
            "warnings": index_warnings,
        }

    def import_spz(
        self,
        filename: str,
        stream: Any,
        content_length: int,
        scene_id: str,
        reconstruction_id: str,
    ) -> dict[str, Any]:
        """Compatibility wrapper for the historical SPZ-only API."""
        return self.import_gaussian_asset(
            filename,
            stream,
            content_length,
            scene_id,
            reconstruction_id,
            SPARK_SPZ_COORDINATE_CONTRACT,
        )

    def _scene_package_manifest_paths(self) -> list[Path]:
        root = self.repository_layout.scene_packages
        runtime_root = self.root / "web_viewer/app/public/scene_package/runtime"
        return sorted({
            *root.glob("*/scene_manifest.json"),
            *root.glob("*/*/scene_manifest.json"),
            *runtime_root.glob("*/scene_manifest.json"),
        })

    def _scene_package_listing_dependencies(
        self,
        manifest_paths: list[Path],
    ) -> tuple[Path, ...]:
        """List the bounded files consulted by the registry projection."""

        dependencies: set[Path] = set()
        for manifest_path in manifest_paths:
            manifest = read_json(manifest_path, {})
            if not isinstance(manifest, dict):
                continue
            package = manifest_path.parent
            assets = manifest.get("assets")
            splats = assets.get("splats") if isinstance(assets, dict) else None
            if isinstance(splats, list):
                for index, splat in enumerate(splats):
                    if not isinstance(splat, dict):
                        continue
                    try:
                        dependencies.add(
                            package / safe_relative(
                                str(splat.get("uri", "")),
                                f"scene_package_splat_{index}",
                            )
                        )
                    except ValueError:
                        continue
            for key, field in (
                ("dataset", "scene_dataset_manifest_uri"),
                ("metric_reconstruction", "scene_metric_reconstruction_manifest_uri"),
            ):
                reference = manifest.get(key)
                uri = reference.get("manifest_uri") if isinstance(reference, dict) else None
                if not isinstance(uri, str):
                    continue
                try:
                    dependencies.add(package / safe_relative(uri, field))
                except ValueError:
                    continue
        return tuple(sorted(dependencies))

    def _scene_package_listing_signature(
        self,
        manifest_paths: list[Path],
        dependencies: tuple[Path, ...],
    ) -> tuple[Any, ...]:
        database = self.repository_layout.site_registry
        mutable_registry_files = (
            database,
            database.with_name(f"{database.name}-wal"),
            database.with_name(f"{database.name}-journal"),
        )
        paths = sorted({*manifest_paths, *dependencies, *mutable_registry_files})
        return tuple(self._path_state_signature(path) for path in paths)

    def scene_packages(self) -> list[dict[str, Any]]:
        """Return a fresh projection while avoiding repeated immutable JSON reads.

        Scene Packages are immutable after their verified publication gate.
        Cache reuse is nevertheless conditional on the exact set and stat
        signatures of every manifest, referenced metadata file, splat, and
        writable Field Memory database. Import/unregister/cleanup also clear
        the cache explicitly through the catalog/runtime refresh boundary.
        """

        with self._scene_package_cache_lock:
            manifest_paths = self._scene_package_manifest_paths()
            signature = self._scene_package_listing_signature(
                manifest_paths,
                self._scene_packages_cache_dependencies,
            )
            if (
                self._scene_packages_cache is not None
                and signature == self._scene_packages_cache_signature
            ):
                return copy.deepcopy(self._scene_packages_cache)

            result: list[dict[str, Any]] = []
            # Retry once if another process publishes a Package or commits the
            # Field Memory registry while the projection is being assembled.
            for _attempt in range(2):
                manifest_paths = self._scene_package_manifest_paths()
                dependencies = self._scene_package_listing_dependencies(manifest_paths)
                before = self._scene_package_listing_signature(manifest_paths, dependencies)
                field_memory_references = self._field_memory_scene_package_reference_counts()
                result = self._build_scene_packages(
                    manifest_paths,
                    field_memory_references,
                )
                after = self._scene_package_listing_signature(manifest_paths, dependencies)
                if before == after:
                    self._scene_packages_cache_dependencies = dependencies
                    self._scene_packages_cache_signature = after
                    self._scene_packages_cache = copy.deepcopy(result)
                    break
            else:
                self._scene_packages_cache_dependencies = ()
                self._scene_packages_cache_signature = None
                self._scene_packages_cache = None
            return copy.deepcopy(result)

    def _build_scene_packages(
        self,
        manifest_paths: list[Path],
        field_memory_references: dict[str, int] | None,
    ) -> list[dict[str, Any]]:
        result: dict[tuple[str, str], dict[str, Any]] = {}
        root = self.repository_layout.scene_packages
        runtime_root = self.root / "web_viewer/app/public/scene_package/runtime"
        for manifest_path in manifest_paths:
            package = manifest_path.parent
            if package.name.startswith(".") or package.parent.name.startswith("."):
                continue
            manifest = read_json(manifest_path, {})
            scene_id = manifest.get("scene_id") if isinstance(manifest, dict) else None
            assets = manifest.get("assets", {}) if isinstance(manifest, dict) else {}
            splats = assets.get("splats", []) if isinstance(assets, dict) else []
            splat = splats[0] if isinstance(splats, list) and splats and isinstance(splats[0], dict) else {}
            splat_uri = splat.get("uri") if isinstance(splat, dict) else None
            try:
                splat_path = package / safe_relative(str(splat_uri), "scene_package_splat_uri") if splat_uri else None
            except ValueError:
                splat_path = None
            package_id = manifest.get("scene_package_id") if isinstance(manifest, dict) else None
            reconstruction_id = manifest.get("reconstruction_id") if isinstance(manifest, dict) else None
            project_id = manifest.get("project_id") if isinstance(manifest, dict) else None
            project_name = manifest.get("project_name") if isinstance(manifest, dict) else None
            provenance = manifest.get("provenance", {}) if isinstance(manifest, dict) else {}
            if not isinstance(project_name, str):
                project_name = (
                    provenance.get("project_name")
                    if isinstance(provenance, dict) and isinstance(provenance.get("project_name"), str)
                    else None
                )
            flat_managed = (
                isinstance(scene_id, str)
                and isinstance(package_id, str)
                and package.parent == root
                and package.name == package_id
                and SCENE_ID_RE.fullmatch(scene_id) is not None
                and SCENE_ID_RE.fullmatch(package_id) is not None
            )
            legacy_nested_managed = (
                isinstance(scene_id, str)
                and isinstance(package_id, str)
                and package.parent.parent == root
                and package.parent.name == scene_id
                and package.name == package_id
                and SCENE_ID_RE.fullmatch(scene_id) is not None
                and SCENE_ID_RE.fullmatch(package_id) is not None
            )
            managed = flat_managed or legacy_nested_managed
            runtime_managed = (
                isinstance(package_id, str)
                and package.parent == runtime_root
                and package.name == package_id
            )
            published = package / "scene_manifest.json" if runtime_managed else (
                self.root / "web_viewer/app/public/scene_package/runtime" / package_id / "scene_manifest.json"
                if managed and isinstance(package_id, str)
                else None
            )
            published_exists = bool(published and published.is_file())
            validation = scene_package_registry_validation(
                package,
                manifest,
                registered=managed or runtime_managed,
                published=published_exists,
            )
            dataset_reference = manifest.get("dataset") if isinstance(manifest, dict) else None
            dataset_manifest = {}
            if isinstance(dataset_reference, dict) and isinstance(dataset_reference.get("manifest_uri"), str):
                try:
                    dataset_manifest = read_json(
                        package / safe_relative(dataset_reference["manifest_uri"], "scene_dataset_manifest_uri"),
                        {},
                    )
                except ValueError:
                    dataset_manifest = {}
            dataset_artifacts = dataset_manifest.get("artifacts") if isinstance(dataset_manifest, dict) else None
            metric_manifest = {}
            metric_reference = manifest.get("metric_reconstruction") if isinstance(manifest, dict) else None
            if isinstance(metric_reference, dict) and isinstance(metric_reference.get("manifest_uri"), str):
                try:
                    metric_manifest = read_json(
                        package / safe_relative(
                            metric_reference["manifest_uri"],
                            "scene_metric_reconstruction_manifest_uri",
                        ),
                        {},
                    )
                except ValueError:
                    metric_manifest = {}
            date_metadata = scene_package_date_metadata(
                manifest_path,
                manifest if isinstance(manifest, dict) else {},
                dataset_manifest if isinstance(dataset_manifest, dict) else {},
                metric_manifest if isinstance(metric_manifest, dict) else {},
            )
            colmap_masks = (
                dataset_artifacts.get("colmap_masks", {})
                if isinstance(dataset_artifacts, dict) else {}
            )
            reconstruction_masks = (
                dataset_artifacts.get("reconstruction_masks", {})
                if isinstance(dataset_artifacts, dict) else {}
            )
            mask_generation = (
                dataset_artifacts.get("mask_generation", {})
                if isinstance(dataset_artifacts, dict) else {}
            )
            reference_count = (
                field_memory_references.get(package_id, 0)
                if field_memory_references is not None and isinstance(package_id, str)
                else None
            )
            entry = {
                "project_id": project_id if isinstance(project_id, str) else (project_name or reconstruction_id),
                "scene_id": scene_id,
                "scene_package_id": package_id,
                "reconstruction_id": reconstruction_id,
                "project_name": project_name,
                **date_metadata,
                "package_uri": str(package.relative_to(self.root)),
                "status": validation.get("status"),
                "validation": validation,
                "managed": managed,
                "published": published_exists,
                "viewer_url": f"/?scene_package={package_id}" if (managed or runtime_managed) else None,
                "schema_version": manifest.get("schema_version") if isinstance(manifest, dict) else None,
                "capabilities": (
                    dict(manifest.get("capabilities", {}))
                    if isinstance(manifest, dict) and isinstance(manifest.get("capabilities"), dict)
                    else {}
                ),
                "field_memory_reference_count": reference_count,
                "field_memory_reference_status": (
                    "referenced" if isinstance(reference_count, int) and reference_count > 0
                    else "unreferenced" if reference_count == 0
                    else "unknown"
                ),
                "dataset": {
                    "schema_version": dataset_manifest.get("schema_version"),
                    "status": dataset_manifest.get("status"),
                    "frame_count": (
                        dataset_artifacts.get("registered_frames", {}).get("file_count")
                        if isinstance(dataset_artifacts, dict) else None
                    ),
                    "mask_count": (
                        dataset_artifacts.get("person_masks", {}).get("file_count")
                        if isinstance(dataset_artifacts, dict) else None
                    ),
                    "colmap_mask_count": (
                        colmap_masks.get("file_count") if isinstance(colmap_masks, dict) else None
                    ),
                    "reconstruction_mask_count": (
                        reconstruction_masks.get("file_count")
                        if isinstance(reconstruction_masks, dict) else None
                    ),
                    "colmap_masks_applied_to_packaged_database": (
                        colmap_masks.get("applied_to_packaged_colmap_database")
                        if isinstance(colmap_masks, dict) else None
                    ),
                    "mask_backend": (
                        mask_generation.get("backend")
                        if isinstance(mask_generation, dict) else None
                    ),
                    "colmap_database": bool(
                        isinstance(dataset_artifacts, dict)
                        and isinstance(dataset_artifacts.get("colmap"), dict)
                        and dataset_artifacts["colmap"].get("database_uri")
                    ),
                    "field_memory": bool(
                        isinstance(dataset_artifacts, dict)
                        and isinstance(dataset_artifacts.get("field_memory"), dict)
                        and dataset_artifacts["field_memory"].get("database_uri")
                    ),
                } if dataset_manifest else None,
                "spz": {
                    "uri": splat_uri,
                    "filename": Path(str(splat_uri or "")).name if splat_uri else None,
                    "size_bytes": splat_path.stat().st_size if splat_path and splat_path.is_file() else splat.get("size_bytes"),
                    "sha256": splat.get("sha256"),
                } if splat else None,
            }
            dynamic_tracking = scene_package_dynamic_tracking_compatibility(manifest)
            entry["dynamic_tracking_compatible"] = dynamic_tracking["compatible"]
            entry["dynamic_tracking_failure_reasons"] = dynamic_tracking["failure_reasons"]
            entry["application_compatibility"] = scene_package_application_compatibility(entry)
            identified = isinstance(scene_id, str) and isinstance(package_id, str)
            identity = (scene_id, package_id) if identified else ("path", str(package))
            existing = result.get(identity)
            if existing is None or flat_managed:
                result[identity] = entry
        return sorted(result.values(), key=lambda item: str(item["package_uri"]))

    def project_assets(self) -> list[dict[str, Any]]:
        """Join each MRP with its registered SPZ Scene Packages.

        MRP is the durable product of App A's COLMAP pipeline.  SPZ packages
        are associated strictly by scene and reconstruction IDs, rather than
        by a filename or a separate mutable registry.
        """
        packages_by_reconstruction: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for package in self.scene_packages():
            scene_id = package.get("scene_id")
            reconstruction_id = package.get("reconstruction_id")
            if isinstance(scene_id, str) and isinstance(reconstruction_id, str):
                packages_by_reconstruction.setdefault((scene_id, reconstruction_id), []).append(package)

        projects: list[dict[str, Any]] = []
        for mrp in self.mrps():
            scene_id = mrp.get("scene_id")
            reconstruction_id = mrp.get("reconstruction_id")
            if not isinstance(scene_id, str) or not isinstance(reconstruction_id, str):
                continue
            manifest = mrp.get("manifest", {})
            provenance = manifest.get("provenance", {}) if isinstance(manifest, dict) else {}
            source_workspace_uri = provenance.get("source_workspace_uri") if isinstance(provenance, dict) else None
            if not isinstance(source_workspace_uri, str) or not source_workspace_uri:
                scene_config = read_json(self._config_path(scene_id), {})
                source_workspace_uri = scene_config.get("workspace") if isinstance(scene_config, dict) else None
            project_name = project_name_from_workspace_uri(source_workspace_uri)
            package_root = self.root / str(mrp.get("package_uri", ""))
            lfs_input = package_root / "lfs_input"
            projects.append({
                "project_id": project_name or reconstruction_id,
                "project_name": project_name or reconstruction_id,
                "scene_id": scene_id,
                "reconstruction_id": reconstruction_id,
                "mrp_uri": mrp.get("package_uri"),
                "source_workspace_uri": source_workspace_uri,
                "lfs_input_uri": str(lfs_input.relative_to(self.root)) if lfs_input.is_dir() else None,
                "status": mrp.get("status", "not_validated"),
                "spz_packages": packages_by_reconstruction.get((scene_id, reconstruction_id), []),
            })
        return sorted(projects, key=lambda item: (str(item["scene_id"]), str(item["reconstruction_id"])))

    def unregister_scene_package(self, scene_id: str, scene_package_id: str) -> dict[str, Any]:
        """Remove a managed SPZ registration without deleting its source MRP.

        The package and Viewer runtime copy are moved to the ignored App A
        work area instead of permanently deleting user data.  This endpoint is
        intentionally unavailable for legacy packages that lack a package ID.
        """
        if not SCENE_ID_RE.fullmatch(scene_id) or not SCENE_ID_RE.fullmatch(scene_package_id):
            raise ValueError("invalid_scene_package_id")
        with scene_package_reference_lock(self.root, scene_package_id):
            references = self._field_memory_scene_package_reference_counts()
            if references is None:
                raise ValueError("scene_package_field_memory_reference_check_failed")
            reference_count = references.get(scene_package_id, 0)
            if reference_count:
                raise ValueError(
                    f"scene_package_referenced_by_field_memory:{scene_package_id}:count={reference_count}"
                )

            canonical = self._managed_scene_package_path(scene_id, scene_package_id)
            manifest = read_json(canonical / "scene_manifest.json")
            if not isinstance(manifest, dict):
                raise FileNotFoundError("scene_package_not_found")
            if manifest.get("scene_id") != scene_id or manifest.get("scene_package_id") != scene_package_id:
                raise ValueError("scene_package_identity_mismatch")

            runtime = self.root / "web_viewer/app/public/scene_package/runtime" / scene_package_id
            trash = self.deleted_scene_packages_root / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}-{scene_package_id}"
            trash.mkdir(parents=True, exist_ok=False)
            moved_canonical = False
            moved_runtime = False
            try:
                shutil.move(str(canonical), str(trash / "canonical"))
                moved_canonical = True
                if runtime.exists():
                    shutil.move(str(runtime), str(trash / "viewer_runtime"))
                    moved_runtime = True
            except Exception:
                if moved_canonical and (trash / "canonical").exists() and not canonical.exists():
                    shutil.move(str(trash / "canonical"), str(canonical))
                if moved_runtime and (trash / "viewer_runtime").exists() and not runtime.exists():
                    shutil.move(str(trash / "viewer_runtime"), str(runtime))
                shutil.rmtree(trash, ignore_errors=True)
                raise

        warnings = [] if moved_runtime else ["viewer_runtime_not_found"]
        warnings.extend(self._refresh_runtime_index())
        warnings.extend(self._refresh_repository_catalog())
        return {
            "status": "verified",
            "operation": "unregistered",
            "scene_id": scene_id,
            "scene_package_id": scene_package_id,
            "trashed_uri": str(trash.relative_to(self.root)),
            "mrp_deleted": False,
            "external_source_deleted": False,
            "viewer_runtime_removed": moved_runtime,
            "warnings": warnings,
        }

    def create_scene_package(self, payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        raise DeprecatedScenePackageApiError(
            "legacy_scene_package_creation_deprecated:use_verified_mrp_and_/api/import-spz"
        )

    def scene_objects(self, scene_id: str) -> dict[str, Any]:
        del scene_id
        raise DeprecatedScenePackageApiError(
            "legacy_scene_objects_api_deprecated:read_annotations_from_selected_scene_package"
        )

    def save_scene_objects(self, scene_id: str, data: dict[str, Any]) -> dict[str, Any]:
        del scene_id, data
        raise ImmutableScenePackageError(
            "scene_package_immutable:new_scene_package_id_required_for_annotation_changes"
        )

    def spatial_summary(self, scene_id: str) -> dict[str, Any]:
        config = self.scene_config(scene_id)
        raw_config = config.get("config", {}) if isinstance(config, dict) else {}
        mrps = [item for item in self.mrps() if item.get("scene_id") == scene_id]
        packages = [item for item in self.scene_packages() if item.get("scene_id") == scene_id]
        # The historical public/scene_package root is an immutable legacy
        # fixture.  It is never a source of truth for a project summary: a
        # summary is derived from a verified canonical Package and its
        # package-ID-addressed runtime copy only.
        viewer_entry = next(
            (
                item for item in packages
                if item.get("status") == "verified" and item.get("published") is True
            ),
            None,
        )
        viewer_package = (
            self.root / str(viewer_entry.get("package_uri"))
            if isinstance(viewer_entry, dict) and isinstance(viewer_entry.get("package_uri"), str)
            else None
        )
        viewer_validation = (
            viewer_entry.get("validation", {})
            if isinstance(viewer_entry, dict) and isinstance(viewer_entry.get("validation"), dict)
            else {"status": "not_available", "failure_reasons": ["verified_scene_package_not_available"]}
        )
        manifest = read_json(viewer_package / "scene_manifest.json", {}) if viewer_package else {}
        proxy_quality = read_json(viewer_package / "geometry/proxy_quality.json", {}) if viewer_package else {}
        manifest_capabilities = manifest.get("capabilities", {}) if isinstance(manifest, dict) else {}
        proxy_capabilities = proxy_quality.get("capabilities", {}) if isinstance(proxy_quality, dict) else {}
        capabilities = {
            "display": viewer_validation.get("status") == "verified",
            "measurement": manifest_capabilities.get("measurement", proxy_capabilities.get("measurement", False)),
            "surface_placement": manifest_capabilities.get("surface_placement", proxy_capabilities.get("surface_placement", False)),
            "fov": manifest_capabilities.get("fov", proxy_capabilities.get("fov", False)),
            "occlusion": manifest_capabilities.get("occlusion", proxy_capabilities.get("occlusion", False)),
        }
        mrp = mrps[0] if mrps else {}
        mrp_manifest = mrp.get("manifest", {}) if isinstance(mrp, dict) else {}
        quality_artifacts = mrp_manifest.get("artifacts", {}).get("quality", {}) if isinstance(mrp_manifest, dict) else {}
        mrp_package = self.root / str(mrp.get("package_uri", ""))

        def quality_status(name: str) -> str:
            uri = quality_artifacts.get(f"{name}_uri") if isinstance(quality_artifacts, dict) else None
            report = read_json(mrp_package / str(uri), {}) if uri else {}
            return str(report.get("status", "not_available"))

        def mask_quality_status() -> str:
            canonical = quality_status("mask")
            if canonical != "not_available":
                return canonical
            person_mask = raw_config.get("person_mask") if isinstance(raw_config, dict) else None
            configured_uri = person_mask.get("report") if isinstance(person_mask, dict) else None
            workspace_uri = raw_config.get("workspace") if isinstance(raw_config, dict) else None
            candidates = [configured_uri]
            if isinstance(workspace_uri, str) and workspace_uri:
                candidates.append(f"{workspace_uri.rstrip('/')}/mask_quality.json")
            for uri in candidates:
                if not isinstance(uri, str) or not uri:
                    continue
                try:
                    report = read_json(self.root / safe_relative(uri, "mask_quality_uri"), {})
                except ValueError:
                    continue
                status = report.get("status") if isinstance(report, dict) else None
                if isinstance(status, str) and status:
                    return status
            return "not_available"

        return {
            "scene_id": scene_id,
            "config": config,
            "mrps": mrps,
            "scene_packages": packages,
            "viewer_package": {
                "scene_package_id": viewer_entry.get("scene_package_id") if isinstance(viewer_entry, dict) else None,
                "status": viewer_validation.get("status"),
                "validation": viewer_validation,
            },
            "quality": {
                "mrp_validation": mrps[0].get("status") if mrps else "not_available",
                "mask_quality": mask_quality_status(),
                "sfm_quality": quality_status("sfm"),
                "metric_quality": quality_status("metric"),
                "gaussian_asset_validation": "verified" if viewer_validation.get("status") == "verified" else "failed",
                "proxy_quality": proxy_quality.get("status", "not_available"),
                "viewer_load_result": viewer_validation.get("status", "failed"),
            },
            "capabilities": {key: bool(capabilities.get(key)) for key in ("display", "measurement", "surface_placement", "fov", "occlusion")},
            "viewer_url": (
                "/?scene_package=" + str(viewer_entry["scene_package_id"])
                if isinstance(viewer_entry, dict) and isinstance(viewer_entry.get("scene_package_id"), str)
                else None
            ),
        }
