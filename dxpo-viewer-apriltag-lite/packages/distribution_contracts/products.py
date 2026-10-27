"""Closed Software Product Line profiles layered over component recipes."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any


PROFILE_ROOT = Path(__file__).resolve().parent / "profiles"
PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")
PRODUCT_RELEASE_LIFECYCLE = (
    "draft", "candidate", "qualified", "released", "superseded",
    "quarantined", "revoked",
)


def _validate_profile(value: Any, source: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"product_profile_invalid:{source.name}:object_required")
    metadata = value.get("metadata")
    composition = value.get("composition")
    content = value.get("content")
    acceptance = value.get("acceptance")
    if not isinstance(metadata, dict) or not PROFILE_ID_RE.fullmatch(str(metadata.get("id", ""))):
        raise RuntimeError(f"product_profile_invalid:{source.name}:metadata")
    if not isinstance(metadata.get("version"), str) or not metadata["version"]:
        raise RuntimeError(f"product_profile_invalid:{source.name}:version")
    if not isinstance(composition, dict) or not isinstance(composition.get("ui_modules"), list):
        raise RuntimeError(f"product_profile_invalid:{source.name}:composition")
    if not isinstance(composition.get("backend_components"), list):
        raise RuntimeError(f"product_profile_invalid:{source.name}:backends")
    if not isinstance(content, dict) or not isinstance(content.get("minimum_scene_packages"), int):
        raise RuntimeError(f"product_profile_invalid:{source.name}:content")
    if not isinstance(acceptance, dict) or not isinstance(acceptance.get("suite"), str):
        raise RuntimeError(f"product_profile_invalid:{source.name}:acceptance")
    return value


def product_profiles() -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    for path in sorted(PROFILE_ROOT.glob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        profiles.append(copy.deepcopy(_validate_profile(value, path)))
    return profiles


def load_product_profile(profile_id: str) -> dict[str, Any]:
    if PROFILE_ID_RE.fullmatch(profile_id) is None:
        raise ValueError("product_profile_id_invalid")
    matches = [item for item in product_profiles() if item["metadata"]["id"] == profile_id]
    if len(matches) != 1:
        raise ValueError(f"product_profile_not_found:{profile_id}")
    return matches[0]


def resolve_product_recipe(payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve one standard profile or mark a free composition experimental.

    Standard profiles own their component selection. Callers may only supply
    content selection and materialization choices; conflicting component input
    is rejected instead of silently widening permissions.
    """

    value = copy.deepcopy(payload)
    profile_id = value.get("product_profile_id")
    if profile_id in (None, ""):
        value.pop("product_profile_id", None)
        value.pop("product_profile_version", None)
        return value
    if not isinstance(profile_id, str):
        raise ValueError("product_profile_id_invalid")
    profile = load_product_profile(profile_id)
    composition = profile["composition"]
    expected = {
        "ui_modules": list(composition["ui_modules"]),
        "backend_components": list(composition["backend_components"]),
        "viewer_variant": str(composition["viewer_variant"]),
    }
    for key, expected_value in expected.items():
        supplied = value.get(key)
        if supplied is not None and supplied != expected_value:
            raise ValueError(f"product_profile_composition_mismatch:{profile_id}:{key}")
        value[key] = expected_value
    content = profile["content"]
    scene_ids = value.get("scene_package_ids", [])
    if not isinstance(scene_ids, list):
        raise ValueError("package_recipe_scene_package_ids_must_be_string_array")
    minimum = int(content["minimum_scene_packages"])
    maximum = int(content["maximum_scene_packages"])
    if len(scene_ids) < minimum or len(scene_ids) > maximum:
        raise ValueError(
            f"product_profile_scene_package_count_invalid:{profile_id}:{minimum}:{maximum}"
        )
    value["product_profile_version"] = profile["metadata"]["version"]
    value.setdefault("package_id", profile_id)
    value.setdefault("label", profile["metadata"]["label"])
    value.setdefault("include_active_analysis", bool(content.get("include_active_analysis", False)))
    value.setdefault("distribution_mode", profile["distribution"]["default_mode"])
    if value["distribution_mode"] not in profile["distribution"]["modes"]:
        raise ValueError(
            f"product_profile_distribution_mode_not_supported:{profile_id}:"
            f"{value['distribution_mode']}"
        )
    value.setdefault("release_lifecycle", "candidate")
    return value
