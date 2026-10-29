"""Validated contracts for composing independently distributed Web apps.

The catalog is deliberately closed. Recipes can select registered UI and
backend components, but cannot inject arbitrary images, mounts, commands, or
Compose fragments.
"""

from __future__ import annotations

import re
from typing import Any


PACKAGE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")
SCENE_PACKAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

UI_MODULES: dict[str, dict[str, Any]] = {
    "viewer": {"label": "多機能3Dシーン閲覧", "preview_mode": "packages", "required_backends": ["scene-reader-api"], "needs_camera": False},
    "colmap": {"label": "3D再構成（COLMAP）", "preview_mode": "pipeline", "required_backends": ["pipeline-api"], "needs_camera": False},
    "apriltag-check": {"label": "AprilTagカメラ確認", "preview_mode": "tag-check", "required_backends": ["apriltag-api"], "needs_camera": True},
    "spz-import": {"label": "3DGSデータ登録", "preview_mode": "spatial", "required_backends": ["spz-import-api"], "needs_camera": False},
    "scene-manager": {"label": "シーンデータ管理", "preview_mode": "packages", "required_backends": ["scene-manager-api"], "needs_camera": False},
    "project-manager": {"label": "処理履歴・成果物管理", "preview_mode": "projects", "required_backends": ["project-manager-api"], "needs_camera": False},
    "package-builder": {"label": "配布アプリ作成", "preview_mode": "package-builder", "required_backends": ["package-builder-api"], "needs_camera": False},
    "field-memory": {"label": "現場・設備台帳", "preview_mode": "field-memory", "required_backends": ["field-memory-api"], "needs_camera": False},
    "frame-analysis": {"label": "登録画像AI解析", "preview_mode": "frame-analysis", "required_backends": ["frame-analysis-api", "search-api", "frame-vlm-worker"], "needs_camera": False},
    "site-insights": {
        "label": "現場インサイト",
        "preview_mode": "site-insights",
        "required_backends": [
            "field-memory-api", "search-api", "image-feature-worker",
            "evidence-ai-api", "frame-vlm-worker", "change-review-api",
        ],
        "needs_camera": False,
    },
    "simple-viewer": {"label": "シンプル3Dシーン閲覧", "preview_mode": "packages", "required_backends": ["scene-reader-api"], "needs_camera": False},
}

BACKEND_COMPONENTS: dict[str, dict[str, Any]] = {
    "scene-reader-api": {"label": "Scene Package read-only backend", "adapter": "fastapi-asgi", "api_feature": "scene-reader", "needs_gpu": False, "needs_docker_socket": False, "reads": ["bundled verified Scene Packages"], "writes": []},
    "pipeline-api": {"label": "COLMAP / MRP backend", "adapter": "fastapi-asgi", "api_feature": "pipeline", "needs_gpu": True, "needs_docker_socket": True, "reads": ["data/scene_repository/assets/source_files", "var/spatial/workspaces"], "writes": ["var/spatial", "data/scene_repository/packages/metric_reconstructions", "data/scene_repository/index/catalog.sqlite3"]},
    "apriltag-api": {"label": "AprilTag detection backend", "adapter": "fastapi-asgi", "api_feature": "apriltag-check", "needs_gpu": False, "needs_docker_socket": False, "reads": [], "writes": []},
    "spz-import-api": {"label": "SPZ import backend", "adapter": "fastapi-asgi", "api_feature": "spz-import", "needs_gpu": False, "needs_docker_socket": False, "reads": ["data/scene_repository/packages/metric_reconstructions"], "writes": ["data/scene_repository/packages/scenes", "web_viewer/app/public/scene_package/runtime"]},
    "scene-manager-api": {"label": "Scene Repository management backend", "adapter": "fastapi-asgi", "api_feature": "scene-manager", "needs_gpu": False, "needs_docker_socket": False, "reads": ["data/scene_repository", "web_viewer/app/public/scene_package/runtime", "bundled verified Scene Packages"], "writes": ["var/spatial/trash/repository", "var/spatial/trash/scene_packages", "data/scene_repository/index/catalog.sqlite3", "web_viewer/app/public/scene_package/distribution_index.json"]},
    "project-manager-api": {"label": "Project management backend", "adapter": "fastapi-asgi", "api_feature": "project-manager", "needs_gpu": False, "needs_docker_socket": False, "reads": ["data/scene_repository/index/catalog.sqlite3", "var/spatial/workspaces", "data/scene_repository/packages/metric_reconstructions", "data/scene_repository/packages/scenes"], "writes": ["delete only terminal, unreferenced failed project workspaces", "data/scene_repository/index/catalog.sqlite3"]},
    "package-builder-api": {"label": "Application package builder backend", "adapter": "fastapi-asgi", "api_feature": "package-builder", "needs_gpu": False, "needs_docker_socket": False, "reads": ["application source and distribution contracts", "data/scene_repository/packages/scenes"], "writes": ["data/scene_repository/deliverables/applications"]},
    "field-memory-api": {"label": "Site records registry backend", "adapter": "fastapi-asgi", "api_feature": "field-memory", "needs_gpu": False, "needs_docker_socket": False, "reads": ["data/scene_repository/packages/scenes", "bundled verified Scene Packages", "data/scene_repository/registries/site_records"], "writes": ["data/scene_repository/registries/site_records"]},
    "search-api": {"label": "Spatial search backend", "adapter": "fastapi-asgi", "api_feature": "spatial-search", "needs_gpu": False, "needs_docker_socket": False, "reads": ["data/scene_repository/registries/site_records", "bundled verified Scene Packages"], "writes": ["data/scene_repository/registries/site_records (image index job queue only)"]},
    "image-feature-worker": {"label": "Image feature / FAISS worker", "adapter": "python-worker", "api_feature": None, "needs_gpu": False, "needs_docker_socket": False, "reads": ["data/scene_repository/registries/site_records/attachments"], "writes": ["var/spatial/cache/site_records", "data/scene_repository/registries/site_records (image feature projections and job status)"]},
    "frame-analysis-api": {"label": "Registered frame analysis backend", "adapter": "fastapi-asgi", "api_feature": "frame-analysis", "needs_gpu": False, "needs_docker_socket": False, "reads": ["self-contained Scene Package dataset", "data/scene_repository/registries/site_records writable overlay"], "writes": ["data/scene_repository/registries/site_records"]},
    "frame-vlm-worker": {"label": "Qwen3-VL-8B local frame analysis worker", "adapter": "python-worker", "api_feature": None, "needs_gpu": True, "needs_docker_socket": False, "reads": ["self-contained Scene Package dataset", "reviewed local VLM model"], "writes": ["data/scene_repository/registries/site_records"]},
    "evidence-ai-api": {"label": "Evidence-grounded AI gateway", "adapter": "fastapi-asgi", "api_feature": "evidence-ai", "needs_gpu": False, "needs_docker_socket": False, "reads": ["data/scene_repository/registries/site_records search result snapshots"], "writes": ["data/scene_repository/registries/site_records (evidence inquiry queue, answers, citations, audit)"]},
    "change-review-api": {"label": "SceneVersion change review backend", "adapter": "fastapi-asgi", "api_feature": "change-review", "needs_gpu": False, "needs_docker_socket": False, "reads": ["data/scene_repository/registries/site_records SceneVersions and observations"], "writes": ["data/scene_repository/registries/site_records (change candidates, reviews, confirmed changes, audit)"]},
    "rosbridge": {"label": "ROS 2 rosbridge WebSocket（任意同梱）", "adapter": "rosbridge-websocket", "api_feature": None, "needs_gpu": False, "needs_docker_socket": False, "reads": ["ROS 2 topics"], "writes": ["ROS 2 topics"]},
}

# AprilTag recognition, Scene Package management and static 3DGS viewing all
# work without a bridge. A bundled rosbridge is therefore always an explicit
# operator choice. Dynamic transport may instead use an externally managed
# rosbridge URL, so no UI combination may force this backend into a recipe.
CONDITIONAL_BACKEND_REQUIREMENTS: list[dict[str, Any]] = []

VIEWER_VARIANTS: dict[str, dict[str, str]] = {
    "full": {
        "label": "多機能3DGS Viewer",
        "description": "既存の動的箱・ROS・人物・Proxy解析・編集UIを含むViewer",
    },
    "simple": {
        "label": "シンプル3DGS Viewer",
        "description": "AprilTagデバッグと移動・回転・寸法変更できる3D定規だけを含む閲覧専用Viewer",
    },
}

# These applications consume the registered-frame dataset embedded in a
# self-contained v1.3 Scene Package.  Other compositions only need the
# display/AprilTag projection and must not duplicate COLMAP databases, source
# images, or masks in a delivery ZIP.
SELF_CONTAINED_SCENE_UI_MODULES = frozenset({
    "field-memory", "frame-analysis", "site-insights",
})


def package_catalog() -> dict[str, Any]:
    from .products import product_profiles

    return {
        "schema_version": "1.0.0",
        "ui_modules": [{"id": key, **value} for key, value in UI_MODULES.items()],
        "backend_components": [{"id": key, **value} for key, value in BACKEND_COMPONENTS.items()],
        "viewer_variants": [{"id": key, **value} for key, value in VIEWER_VARIANTS.items()],
        "product_profiles": product_profiles(),
        "conditional_backend_requirements": CONDITIONAL_BACKEND_REQUIREMENTS,
        "constraints": {"bundled_scene_packages": "verified_read_only_snapshots_only", "arbitrary_compose_fragments": False, "arbitrary_container_images": False},
    }


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"package_recipe_{field}_must_be_string_array")
    if len(value) != len(set(value)):
        raise ValueError(f"package_recipe_{field}_contains_duplicates")
    return value


def normalize_package_recipe(value: Any) -> dict[str, Any]:
    from .products import resolve_product_recipe

    if not isinstance(value, dict):
        raise ValueError("package_recipe_must_be_object")
    value = resolve_product_recipe(value)
    package_id = value.get("package_id")
    if not isinstance(package_id, str) or not PACKAGE_ID_RE.fullmatch(package_id):
        raise ValueError("package_recipe_package_id_invalid")
    label = value.get("label", package_id)
    if not isinstance(label, str) or not label.strip() or len(label.strip()) > 80:
        raise ValueError("package_recipe_label_invalid")
    ui_modules = _string_list(value.get("ui_modules"), "ui_modules")
    backend_components = _string_list(value.get("backend_components"), "backend_components")
    scene_package_ids = _string_list(value.get("scene_package_ids", []), "scene_package_ids")
    simple_viewer_selected = "simple-viewer" in ui_modules
    viewer_variant = value.get(
        "viewer_variant",
        "simple" if simple_viewer_selected else "full",
    )
    if not isinstance(viewer_variant, str) or viewer_variant not in VIEWER_VARIANTS:
        raise ValueError("package_recipe_viewer_variant_invalid")
    invalid_scene_packages = [
        item for item in scene_package_ids if SCENE_PACKAGE_ID_RE.fullmatch(item) is None
    ]
    if invalid_scene_packages:
        raise ValueError(
            f"package_recipe_scene_package_id_invalid:{','.join(invalid_scene_packages)}"
        )
    if not ui_modules:
        raise ValueError("package_recipe_ui_module_required")
    deprecated_ui = sorted(set(ui_modules) & {"spatial-search", "evidence-ai", "change-review"})
    if deprecated_ui:
        raise ValueError(
            f"package_recipe_deprecated_ui:{','.join(deprecated_ui)}:use_site-insights"
        )
    unknown_ui = sorted(set(ui_modules) - UI_MODULES.keys())
    if unknown_ui:
        raise ValueError(f"package_recipe_unknown_ui:{','.join(unknown_ui)}")
    unknown_backend = sorted(set(backend_components) - BACKEND_COMPONENTS.keys())
    if unknown_backend:
        raise ValueError(f"package_recipe_unknown_backend:{','.join(unknown_backend)}")
    simple_viewer_compatible_ui = {"simple-viewer", "frame-analysis"}
    if simple_viewer_selected and not set(ui_modules).issubset(simple_viewer_compatible_ui):
        raise ValueError("package_recipe_simple_viewer_must_be_exclusive")
    if simple_viewer_selected and viewer_variant != "simple":
        raise ValueError("package_recipe_simple_viewer_variant_required")
    if simple_viewer_selected and len(scene_package_ids) != 1:
        raise ValueError("package_recipe_simple_viewer_requires_one_scene_package")
    if "viewer" in ui_modules and not scene_package_ids:
        raise ValueError("package_recipe_viewer_requires_scene_package")
    if "frame-analysis" in ui_modules and not scene_package_ids:
        raise ValueError("package_recipe_frame_analysis_requires_scene_package")
    include_active_analysis = value.get("include_active_analysis", True)
    if not isinstance(include_active_analysis, bool):
        raise ValueError("package_recipe_include_active_analysis_must_be_boolean")
    if "frame-analysis" not in ui_modules:
        include_active_analysis = False
    product_profile_id = value.get("product_profile_id")
    product_profile_version = value.get("product_profile_version")
    support_level = "standard" if isinstance(product_profile_id, str) else "experimental"
    release_lifecycle = value.get("release_lifecycle", "candidate" if support_level == "standard" else "draft")
    if release_lifecycle not in {
        "draft", "candidate", "qualified", "released", "superseded",
        "quarantined", "revoked",
    }:
        raise ValueError("package_recipe_release_lifecycle_invalid")
    distribution_mode = value.get("distribution_mode", "split" if support_level == "standard" else "offline")
    if distribution_mode not in {"connected", "split", "offline"}:
        raise ValueError("package_recipe_distribution_mode_invalid")

    required = {backend for ui_id in ui_modules for backend in UI_MODULES[ui_id]["required_backends"]}
    for rule in CONDITIONAL_BACKEND_REQUIREMENTS:
        required_ui = set(rule["all_ui_modules"])
        minimum_packages = int(rule["minimum_scene_packages"])
        if required_ui.issubset(ui_modules) and len(scene_package_ids) >= minimum_packages:
            required.update(str(item) for item in rule["required_backends"])
    missing = sorted(required - set(backend_components))
    if missing:
        raise ValueError(f"package_recipe_missing_backend:{','.join(missing)}")

    api_features = [
        str(BACKEND_COMPONENTS[item]["api_feature"])
        for item in backend_components
        if BACKEND_COMPONENTS[item]["api_feature"] is not None
    ]
    if not api_features:
        raise ValueError("package_recipe_application_api_required")

    def unique_paths(key: str) -> list[str]:
        return list(dict.fromkeys(path for item in backend_components for path in BACKEND_COMPONENTS[item][key]))

    return {
        "schema_version": "1.0.0",
        "package_id": package_id,
        "label": label.strip(),
        "ui_modules": ui_modules,
        "preview_modes": [str(UI_MODULES[item]["preview_mode"]) for item in ui_modules],
        "backend_components": backend_components,
        "api_features": api_features,
        "needs_gpu": any(bool(BACKEND_COMPONENTS[item]["needs_gpu"]) for item in backend_components),
        "needs_docker_socket": any(bool(BACKEND_COMPONENTS[item]["needs_docker_socket"]) for item in backend_components),
        "needs_camera": any(bool(UI_MODULES[item]["needs_camera"]) for item in ui_modules),
        "include_rosbridge": "rosbridge" in backend_components,
        "scene_package_ids": scene_package_ids,
        "viewer_variant": viewer_variant,
        "scene_content_mode": (
            "self-contained"
            if SELF_CONTAINED_SCENE_UI_MODULES.intersection(ui_modules)
            else "display"
        ),
        "include_active_analysis": include_active_analysis,
        "product_profile_id": product_profile_id,
        "product_profile_version": product_profile_version,
        "support_level": support_level,
        "release_lifecycle": release_lifecycle,
        "distribution_mode": distribution_mode,
        "reads": unique_paths("reads"),
        "writes": unique_paths("writes"),
    }
