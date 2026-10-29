"""Framework-independent route dispatcher shared by legacy and ASGI adapters."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import unquote

from .features import require_feature


def dispatch_preview_api(
    service: Any,
    method: str,
    path: str,
    payload: dict[str, Any] | None,
    *,
    server_version: str,
    enabled_features: frozenset[str] = frozenset({"suite"}),
) -> Any:
    parts = [unquote(item) for item in path.split("/") if item]
    if parts[:2] != ["api", "health"] and (not parts or parts[0] != "api"):
        raise FileNotFoundError("api_endpoint_not_found")
    if parts == ["api", "health"]:
        return {
            "status": "verified",
            "service": server_version,
            "pid": os.getpid(),
            "started_at": service.started_at,
            "enabled_features": sorted(enabled_features),
        }
    if parts == ["api", "repository"] and method == "GET":
        require_feature(enabled_features, "scene-manager", "project-manager", "pipeline")
        return service.repository_status()
    if parts == ["api", "repository", "rebuild"] and method == "POST":
        require_feature(enabled_features, "scene-manager", "project-manager")
        return service.repository_status(rebuild=True)
    if parts == ["api", "repository", "items"] and method == "GET":
        require_feature(enabled_features, "scene-manager")
        return service.repository_inventory()
    if parts == ["api", "repository", "cleanup-preview"] and method == "POST":
        require_feature(enabled_features, "scene-manager")
        return service.repository_cleanup_preview(payload or {})
    if parts == ["api", "repository", "cleanup"] and method == "POST":
        require_feature(enabled_features, "scene-manager")
        return service.repository_cleanup(payload or {})
    if parts == ["api", "scene-configs"]:
        require_feature(enabled_features, "pipeline")
        return service.scene_configs()
    if parts == ["api", "apriltag-families"] and method == "GET":
        require_feature(enabled_features, "pipeline", "apriltag-check")
        return service.apriltag_families()
    if parts == ["api", "projects"]:
        require_feature(enabled_features, "project-manager")
        return service.project_assets()
    if parts == ["api", "storage"]:
        require_feature(enabled_features, "project-manager")
        return service.storage_status()
    if parts == ["api", "storage", "cleanup-preview"] and method == "POST":
        require_feature(enabled_features, "project-manager")
        return service.storage_cleanup_preview()
    if parts == ["api", "storage", "cleanup"] and method == "POST":
        require_feature(enabled_features, "project-manager")
        return service.storage_cleanup(payload or {})
    if parts == ["api", "app-package-catalog"] and method == "GET":
        require_feature(enabled_features, "package-builder")
        return service.app_package_catalog()
    if parts == ["api", "app-packages"] and method == "GET":
        require_feature(enabled_features, "package-builder")
        return service.app_packages()
    if parts == ["api", "app-packages"] and method == "POST":
        require_feature(enabled_features, "package-builder")
        return service.start_app_package_job(payload or {})
    if parts == ["api", "app-packages", "preflight"] and method == "POST":
        require_feature(enabled_features, "package-builder")
        return service.app_package_preflight(payload or {})
    if parts == ["api", "product-releases"] and method == "GET":
        require_feature(enabled_features, "package-builder")
        return service.product_release_inventory()
    if parts == ["api", "product-releases", "retention-preview"] and method == "GET":
        require_feature(enabled_features, "package-builder")
        return service.product_retention_preview()
    if parts == ["api", "app-package-jobs"] and method == "GET":
        require_feature(enabled_features, "package-builder")
        return service.app_package_jobs()
    if len(parts) >= 3 and parts[:2] == ["api", "app-package-jobs"]:
        require_feature(enabled_features, "package-builder")
        job_id = parts[2]
        if len(parts) == 3 and method == "GET":
            return service.app_package_job(job_id)
        if len(parts) == 4 and parts[3] == "cancel" and method == "POST":
            return service.cancel_app_package_job(job_id)
        if len(parts) == 4 and parts[3] == "retry" and method == "POST":
            return service.retry_app_package_job(job_id)
    if parts == ["api", "project-workspaces"] and method == "GET":
        require_feature(enabled_features, "project-manager")
        return service.project_workspaces()
    if len(parts) == 3 and parts[:2] == ["api", "project-workspaces"] and method == "DELETE":
        require_feature(enabled_features, "project-manager")
        return service.delete_rejected_project(parts[2])
    if len(parts) == 3 and parts[:2] == ["api", "scene-configs"]:
        require_feature(enabled_features, "pipeline")
        return service.scene_config(parts[2])
    if parts == ["api", "jobs"] and method == "GET":
        require_feature(enabled_features, "pipeline")
        return service.jobs()
    if parts == ["api", "jobs"] and method == "POST":
        require_feature(enabled_features, "pipeline")
        return service.start_job(payload or {})
    if len(parts) >= 3 and parts[:2] == ["api", "jobs"]:
        require_feature(enabled_features, "pipeline")
        job_id = parts[2]
        if len(parts) == 3:
            return service._job(job_id)
        if parts[3] == "stages":
            return service.job_stages(job_id)
        if parts[3] == "logs":
            return service.job_log(job_id)
        if parts[3] in {"resume", "reuse"} and method == "POST":
            return service.start_job(service._job(job_id), resume=True, reuse=parts[3] == "reuse")
        if parts[3] == "open-lfs-folder" and method == "POST":
            return service.open_lfs_folder(job_id)
        if parts[3] == "report":
            return service.job_report(job_id)
    if parts == ["api", "mrps"]:
        require_feature(enabled_features, "spz-import")
        return service.mrps()
    if len(parts) == 5 and parts[:2] == ["api", "mrps"] and parts[4] == "manifest":
        require_feature(enabled_features, "spz-import")
        return service.mrp_manifest(parts[2], parts[3])
    if len(parts) == 5 and parts[:2] == ["api", "mrps"] and parts[4] == "validate" and method == "POST":
        require_feature(enabled_features, "spz-import")
        return service.validate_mrp(parts[2], parts[3])
    if parts == ["api", "scene-packages"] and method == "GET":
        require_feature(enabled_features, "scene-manager", "scene-reader", "apriltag-check")
        return service.scene_packages()
    if parts == ["api", "scene-packages"] and method == "POST":
        require_feature(enabled_features, "spz-import")
        return service.create_scene_package(payload or {})
    if len(parts) == 4 and parts[:2] == ["api", "scene-packages"] and method == "DELETE":
        require_feature(enabled_features, "scene-manager")
        return service.unregister_scene_package(parts[2], parts[3])
    if len(parts) == 4 and parts[:2] == ["api", "scene-packages"]:
        require_feature(enabled_features, "scene-manager")
        scene_id = parts[2]
        if parts[3] == "spatial":
            return service.spatial_summary(scene_id)
        if parts[3] == "objects":
            if method == "GET":
                return service.scene_objects(scene_id)
            if method == "POST":
                body = payload or {}
                data = body.get("data") if isinstance(body.get("data"), dict) else body
                if not isinstance(data, dict):
                    raise ValueError("scene_objects_payload_invalid")
                return service.save_scene_objects(scene_id, data)
    raise FileNotFoundError("api_endpoint_not_found")
