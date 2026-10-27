"""Validation and atomic persistence helpers for Scene Package objects."""

from __future__ import annotations

import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0.0"
SCENE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
OBJECT_ID_RE = SCENE_ID_RE
OBJECT_TYPES = {"camera", "person"}
SURFACE_TYPES = {"floor", "wall", "ceiling", "free"}
TARGET_TYPES = {"torso", "head"}
COMMON_KEYS = {"id", "type", "name", "enabled", "position", "quaternion", "created_at", "updated_at", "camera", "person"}
CAMERA_KEYS = {"mount_surface_type", "mount_surface_normal", "surface_offset_m", "vertical_fov_deg", "resolution", "near_m", "far_m", "frustum_visible", "free_placement"}
PERSON_KEYS = {"height_m", "yaw_deg", "front_direction", "target_type"}


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{label}_must_be_finite")
    return float(value)


def _vector(value: Any, length: int, label: str) -> list[float]:
    if not isinstance(value, list) or len(value) != length:
        raise ValueError(f"{label}_must_have_{length}_values")
    return [_finite(item, label) for item in value]


def _safe_string(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"{label}_must_be_string")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or "/" in value or "\\" in value:
        raise ValueError(f"{label}_must_not_be_path")
    return value


def _validate_quaternion(value: Any, label: str = "quaternion") -> list[float]:
    result = _vector(value, 4, label)
    norm = math.sqrt(sum(item * item for item in result))
    if norm <= 1e-9 or abs(norm - 1.0) > 1e-3:
        raise ValueError(f"{label}_must_be_normalized")
    return result


def _validate_camera(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("camera_settings_required")
    unknown = set(value) - CAMERA_KEYS
    if unknown:
        raise ValueError(f"unknown_camera_settings:{sorted(unknown)}")
    required = ("mount_surface_type", "mount_surface_normal", "surface_offset_m", "vertical_fov_deg", "resolution", "frustum_visible")
    missing = [key for key in required if key not in value]
    if missing:
        raise ValueError(f"camera_settings_missing:{','.join(missing)}")
    surface = value["mount_surface_type"]
    if surface not in SURFACE_TYPES:
        raise ValueError("invalid_camera_surface_type")
    normal = _vector(value["mount_surface_normal"], 3, "mount_surface_normal")
    if math.sqrt(sum(item * item for item in normal)) <= 1e-9:
        raise ValueError("mount_surface_normal_zero")
    offset = _finite(value["surface_offset_m"], "surface_offset_m")
    if offset <= 0:
        raise ValueError("surface_offset_m_must_be_positive")
    fov = _finite(value["vertical_fov_deg"], "vertical_fov_deg")
    if not 1.0 < fov < 179.0:
        raise ValueError("vertical_fov_out_of_range")
    resolution = value["resolution"]
    if not isinstance(resolution, list) or len(resolution) != 2 or any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in resolution):
        raise ValueError("camera_resolution_invalid")
    result = dict(value)
    result["mount_surface_normal"] = normal
    result["surface_offset_m"] = offset
    result["vertical_fov_deg"] = fov
    result["near_m"] = _finite(value.get("near_m", 0.01), "near_m")
    result["far_m"] = _finite(value.get("far_m", 1.5), "far_m")
    if result["near_m"] <= 0 or result["far_m"] <= result["near_m"]:
        raise ValueError("camera_clip_range_invalid")
    if not isinstance(value["frustum_visible"], bool):
        raise ValueError("frustum_visible_must_be_boolean")
    if "free_placement" in value and not isinstance(value["free_placement"], bool):
        raise ValueError("free_placement_must_be_boolean")
    return result


def _validate_person(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("person_settings_required")
    unknown = set(value) - PERSON_KEYS
    if unknown:
        raise ValueError(f"unknown_person_settings:{sorted(unknown)}")
    missing = [key for key in ("height_m", "yaw_deg", "front_direction", "target_type") if key not in value]
    if missing:
        raise ValueError(f"person_settings_missing:{','.join(missing)}")
    height = _finite(value["height_m"], "height_m")
    if height <= 0:
        raise ValueError("height_m_must_be_positive")
    yaw = _finite(value["yaw_deg"], "yaw_deg")
    direction = _vector(value["front_direction"], 3, "front_direction")
    if math.sqrt(sum(item * item for item in direction)) <= 1e-9:
        raise ValueError("front_direction_zero")
    if value["target_type"] not in TARGET_TYPES:
        raise ValueError("invalid_person_target_type")
    result = dict(value)
    result["height_m"] = height
    result["yaw_deg"] = yaw
    result["front_direction"] = direction
    return result


def validate_scene_objects(data: Any, expected_scene_id: str | None = None) -> dict[str, Any]:
    failures: list[str] = []
    if not isinstance(data, dict):
        return {"schema_version": SCHEMA_VERSION, "status": "failed", "failure_reasons": ["root_must_be_object"], "warnings": []}
    if data.get("schema_version") != SCHEMA_VERSION:
        failures.append("unsupported_schema_version")
    scene_id = data.get("scene_id")
    if not isinstance(scene_id, str) or not SCENE_ID_RE.fullmatch(scene_id):
        failures.append("invalid_scene_id")
    elif expected_scene_id and scene_id != expected_scene_id:
        failures.append("scene_id_mismatch")
    if data.get("coordinate_space") != "world":
        failures.append("coordinate_space_must_be_world")
    if data.get("unit") != "meter":
        failures.append("unit_must_be_meter")
    if data.get("up_axis") != "+Y":
        failures.append("up_axis_must_be_+Y")
    objects = data.get("objects")
    if not isinstance(objects, list):
        failures.append("objects_must_be_array")
        objects = []
    seen: set[str] = set()
    for index, obj in enumerate(objects):
        prefix = f"objects[{index}]"
        if not isinstance(obj, dict):
            failures.append(f"{prefix}_must_be_object")
            continue
        unknown = set(obj) - COMMON_KEYS
        if unknown:
            failures.append(f"{prefix}_unknown_fields:{sorted(unknown)}")
        try:
            object_id = _safe_string(obj.get("id"), f"{prefix}.id")
            if not OBJECT_ID_RE.fullmatch(object_id):
                raise ValueError(f"{prefix}.id_invalid")
            if object_id in seen:
                raise ValueError("duplicate_object_id")
            seen.add(object_id)
            if obj.get("type") not in OBJECT_TYPES:
                raise ValueError(f"{prefix}.unknown_type")
            _safe_string(obj.get("name"), f"{prefix}.name")
            if not isinstance(obj.get("enabled"), bool):
                raise ValueError(f"{prefix}.enabled_must_be_boolean")
            _vector(obj.get("position"), 3, f"{prefix}.position")
            _validate_quaternion(obj.get("quaternion"), f"{prefix}.quaternion")
            _safe_string(obj.get("created_at"), f"{prefix}.created_at", allow_empty=False)
            _safe_string(obj.get("updated_at"), f"{prefix}.updated_at", allow_empty=False)
            if obj.get("type") == "camera":
                _validate_camera(obj.get("camera"))
                if obj.get("person") is not None:
                    raise ValueError(f"{prefix}.person_must_be_null")
            else:
                _validate_person(obj.get("person"))
                if obj.get("camera") is not None:
                    raise ValueError(f"{prefix}.camera_must_be_null")
        except ValueError as error:
            failures.append(str(error))
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "verified" if not failures else "failed",
        "scene_id": scene_id,
        "object_count": len(objects),
        "failure_reasons": failures,
        "warnings": [],
    }


def empty_scene_objects(scene_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "scene_id": scene_id,
        "coordinate_space": "world",
        "unit": "meter",
        "up_axis": "+Y",
        "objects": [],
    }


def atomic_write_scene_objects(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(__import__("json").dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
