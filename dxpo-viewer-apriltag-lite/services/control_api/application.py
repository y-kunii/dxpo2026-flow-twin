"""FastAPI adapter for the existing framework-independent preview service.

The Scene Package/MRP implementation remains owned by ``PreviewService``.
This module owns HTTP concerns only: routing, bounded JSON parsing, streaming
binary bodies, status mapping, OpenAPI, CORS, and request observability.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from functools import partial
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool


ROOT = Path(__file__).resolve().parents[2]
from generation_pipeline.tools.application_preview_api import (
    DeprecatedScenePackageApiError,
    ImmutableScenePackageError,
    MAX_LIVE_FRAME_BYTES,
    PreviewService,
    SceneConfigError,
    json_hashable,
)

from .change_review import ChangeReviewService, dispatch_change_review_api
from .evidence_ai import EvidenceAiService, dispatch_evidence_ai_api
from .features import ApplicationFeatureDisabled, parse_enabled_features, require_feature
from .field_memory import (
    MAX_STORED_DOCUMENT_BYTES,
    FieldMemoryService,
    MAX_STORED_IMAGE_BYTES,
    dispatch_field_memory_api,
)
from .frame_analysis import (
    FrameAnalysisService,
    RegisteredFrameAnalysisEngine,
    dispatch_frame_analysis_api,
)
from .governance import (
    authorize,
    enforce_site_scope,
    filter_site_scoped_collection,
    principal_from_headers,
)
from .image_features import ImageFeatureIndexService, dispatch_image_feature_api
from .spatial_search import SpatialSearchService, dispatch_spatial_search_api
from .observability import (  # noqa: E402
    REQUEST_ID,
    RequestMetrics,
    accepted_request_id,
    log_event,
    resource_fields,
)
from .routes import dispatch_preview_api


SERVER_VERSION = "SpatialControlAPI/2.0"
MAX_JSON_BODY_BYTES = 1_000_000
LOGGER = logging.getLogger("spatial.control_api")
CONTROL_TOKEN_HEADER = "x-spatial-control-token"
MIN_CONTROL_TOKEN_LENGTH = 32


JSON_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", "/api/health"),
    ("GET", "/api/scene-configs"),
    ("GET", "/api/scene-configs/{scene_id}"),
    ("GET", "/api/apriltag-families"),
    ("GET", "/api/projects"),
    ("GET", "/api/storage"),
    ("POST", "/api/storage/cleanup-preview"),
    ("POST", "/api/storage/cleanup"),
    ("GET", "/api/repository/items"),
    ("POST", "/api/repository/cleanup-preview"),
    ("POST", "/api/repository/cleanup"),
    ("GET", "/api/app-package-catalog"),
    ("GET", "/api/app-packages"),
    ("POST", "/api/app-packages"),
    ("POST", "/api/app-packages/preflight"),
    ("GET", "/api/app-package-jobs"),
    ("GET", "/api/app-package-jobs/{job_id}"),
    ("POST", "/api/app-package-jobs/{job_id}/cancel"),
    ("POST", "/api/app-package-jobs/{job_id}/retry"),
    ("GET", "/api/project-workspaces"),
    ("DELETE", "/api/project-workspaces/{project_id}"),
    ("GET", "/api/jobs"),
    ("POST", "/api/jobs"),
    ("GET", "/api/jobs/{job_id}"),
    ("GET", "/api/jobs/{job_id}/stages"),
    ("GET", "/api/jobs/{job_id}/logs"),
    ("GET", "/api/jobs/{job_id}/report"),
    ("POST", "/api/jobs/{job_id}/resume"),
    ("POST", "/api/jobs/{job_id}/reuse"),
    ("POST", "/api/jobs/{job_id}/open-lfs-folder"),
    ("GET", "/api/mrps"),
    ("GET", "/api/mrps/{scene_id}/{reconstruction_id}/manifest"),
    ("POST", "/api/mrps/{scene_id}/{reconstruction_id}/validate"),
    ("GET", "/api/scene-packages"),
    ("POST", "/api/scene-packages"),
    ("DELETE", "/api/scene-packages/{scene_id}/{scene_package_id}"),
    ("GET", "/api/scene-packages/{scene_id}/spatial"),
    ("GET", "/api/scene-packages/{scene_id}/objects"),
    ("POST", "/api/scene-packages/{scene_id}/objects"),
    ("GET", "/api/field-memory/summary"),
    ("GET", "/api/field-memory/verified-scene-packages"),
    ("GET", "/api/field-memory/sites"),
    ("POST", "/api/field-memory/sites"),
    ("GET", "/api/field-memory/sites/{site_id}"),
    ("GET", "/api/field-memory/sites/{site_id}/scene-versions"),
    ("POST", "/api/field-memory/sites/{site_id}/scene-versions"),
    ("GET", "/api/field-memory/sites/{site_id}/assets"),
    ("POST", "/api/field-memory/sites/{site_id}/assets"),
    ("GET", "/api/field-memory/assets/{asset_id}/observations"),
    ("POST", "/api/field-memory/assets/{asset_id}/observations"),
    ("GET", "/api/field-memory/assets/{asset_id}/documents"),
    ("POST", "/api/field-memory/assets/{asset_id}/documents"),
    ("GET", "/api/field-memory/assets/{asset_id}/maintenance-records"),
    ("POST", "/api/field-memory/assets/{asset_id}/maintenance-records"),
    ("GET", "/api/field-memory/sites/{site_id}/captures"),
    ("POST", "/api/field-memory/sites/{site_id}/captures"),
    ("GET", "/api/field-memory/sites/{site_id}/inspections"),
    ("POST", "/api/field-memory/sites/{site_id}/inspections"),
    ("GET", "/api/field-memory/inspections/{inspection_id}"),
    ("POST", "/api/field-memory/inspections/{inspection_id}/transition"),
    ("POST", "/api/field-memory/inspections/{inspection_id}/observations"),
    ("GET", "/api/field-memory/sites/{site_id}/issues"),
    ("POST", "/api/field-memory/sites/{site_id}/issues"),
    ("GET", "/api/field-memory/issues/{issue_id}"),
    ("POST", "/api/field-memory/issues/{issue_id}/transition"),
    ("GET", "/api/field-memory/issues/{issue_id}/actions"),
    ("POST", "/api/field-memory/issues/{issue_id}/actions"),
    ("POST", "/api/field-memory/corrective-actions/{corrective_action_id}/transition"),
    ("POST", "/api/field-memory/corrective-actions/{corrective_action_id}/approvals"),
    ("POST", "/api/field-memory/sites/{site_id}/reports"),
    ("GET", "/api/field-memory/audit-events"),
    ("GET", "/api/spatial-search/sites"),
    ("POST", "/api/spatial-search/query"),
    ("POST", "/api/spatial-search/timeline"),
    ("POST", "/api/spatial-search/image-index/jobs"),
    ("POST", "/api/spatial-search/image-index/jobs/query"),
    ("GET", "/api/frame-analysis/runs"),
    ("POST", "/api/frame-analysis/runs"),
    ("GET", "/api/frame-analysis/policies"),
    ("GET", "/api/frame-analysis/models"),
    ("POST", "/api/frame-analysis/models/install"),
    ("GET", "/api/frame-analysis/scene-versions"),
    ("GET", "/api/frame-analysis/scene-versions/{scene_version_id}/reset-preflight"),
    ("POST", "/api/frame-analysis/scene-versions/{scene_version_id}/reset"),
    ("POST", "/api/frame-analysis/scene-versions/{scene_version_id}/candidates/query"),
    ("POST", "/api/frame-analysis/scene-versions/{scene_version_id}/selected-images"),
    ("POST", "/api/frame-analysis/scene-versions/{scene_version_id}/all-registered-images"),
    ("GET", "/api/frame-analysis/runs/{analysis_run_id}/recent-frames"),
    ("GET", "/api/frame-analysis/runs/{analysis_run_id}/quality-summary"),
    ("GET", "/api/frame-analysis/runs/{analysis_run_id}"),
    ("POST", "/api/frame-analysis/runs/{analysis_run_id}/cancel"),
    ("GET", "/api/frame-analysis/frames/{frame_analysis_id}"),
    ("POST", "/api/frame-analysis/frames/{frame_analysis_id}/reviews"),
    ("POST", "/api/frame-analysis/scene-versions/{scene_version_id}/active-run"),
    ("POST", "/api/evidence-ai/inquiries"),
    ("POST", "/api/evidence-ai/inquiries/query"),
    ("GET", "/api/evidence-ai/inquiries/{inquiry_id}"),
    ("POST", "/api/change-review/detect"),
    ("POST", "/api/change-review/candidates/query"),
    ("GET", "/api/change-review/candidates/{change_candidate_id}"),
    ("POST", "/api/change-review/candidates/{change_candidate_id}/review"),
)


def _failure(status: int, error: BaseException, **extra: Any) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"status": "failed", "failure_reasons": [str(error)], **extra},
    )


def configured_allowed_origins(value: str | None = None) -> tuple[str, ...]:
    """Return an explicit CORS allowlist; wildcard origins are forbidden."""

    configured = os.environ.get("SPATIAL_ALLOWED_ORIGINS", "") if value is None else value
    origins: list[str] = []
    for raw in configured.split(","):
        origin = raw.strip().rstrip("/")
        if not origin:
            continue
        if origin == "*" or not origin.startswith(("http://", "https://")):
            raise ValueError(f"spatial_allowed_origin_invalid:{origin}")
        if origin not in origins:
            origins.append(origin)
    return tuple(origins)


def validated_control_token(value: str | None) -> str | None:
    """Validate an operator-supplied local control token without logging it."""

    if value is None:
        return None
    token = value.strip()
    if len(token) < MIN_CONTROL_TOKEN_LENGTH or any(character.isspace() for character in token):
        raise ValueError("spatial_control_token_invalid")
    return token


async def _json_payload(request: Request) -> dict[str, Any]:
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > MAX_JSON_BODY_BYTES:
                raise ValueError("request_body_too_large")
        except ValueError as error:
            if str(error) == "request_body_too_large":
                raise
            raise ValueError("request_content_length_invalid") from error
    raw = await request.body()
    if len(raw) > MAX_JSON_BODY_BYTES:
        raise ValueError("request_body_too_large")
    try:
        value = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("request_body_invalid_json") from error
    if not isinstance(value, dict):
        raise ValueError("request_body_must_be_object")
    return value


async def _dispatch_json(request: Request) -> JSONResponse:
    payload = await _json_payload(request) if request.method == "POST" else None
    principal = request.state.principal
    enforce_site_scope(principal, payload, {})

    def response(result: Any) -> JSONResponse:
        result = filter_site_scoped_collection(principal, request.url.path, result)
        if request.url.path.startswith(("/api/field-memory/", "/api/spatial-search/", "/api/frame-analysis/", "/api/evidence-ai/", "/api/change-review/")):
            enforce_site_scope(principal, payload, result)
        if request.url.path.startswith(
            (
                "/api/field-memory/",
                "/api/spatial-search/",
                "/api/frame-analysis/",
                "/api/evidence-ai/",
                "/api/change-review/",
            )
        ) and request.url.path != "/api/field-memory/audit-events":
            site_id = None
            if isinstance(result, dict) and isinstance(result.get("site_id"), str):
                site_id = result["site_id"]
            if site_id is None and payload is not None and isinstance(payload.get("site_id"), str):
                site_id = payload["site_id"]
            if site_id is None:
                query_site = request.query_params.get("site_id")
                site_id = query_site if isinstance(query_site, str) and query_site else None
            request.app.state.field_memory_service.append_audit_event(
                actor_id=principal.actor_id,
                role=principal.role,
                site_id=site_id or principal.site_id,
                action=f"{request.method} {request.url.path}",
                target=request.url.path,
                request_id=REQUEST_ID.get(),
                result="success",
                details={
                    "feature_set": sorted(request.app.state.enabled_features),
                    "read_only": request.method in {"GET", "HEAD"},
                },
            )
        return JSONResponse(status_code=HTTPStatus.OK, content=json_hashable(result))

    if request.url.path.startswith("/api/field-memory/"):
        result = await run_in_threadpool(
            partial(
                dispatch_field_memory_api,
                request.app.state.field_memory_service,
                request.method,
                request.url.path,
                payload,
                enabled_features=request.app.state.enabled_features,
                actor_id=principal.actor_id,
            )
        )
        return response(result)
    if request.url.path.startswith("/api/spatial-search/"):
        if request.url.path.startswith("/api/spatial-search/image-index/"):
            result = await run_in_threadpool(
                partial(
                    dispatch_image_feature_api,
                    request.app.state.image_feature_service,
                    request.method,
                    request.url.path,
                    payload,
                    enabled_features=request.app.state.enabled_features,
                )
            )
            return response(result)
        result = await run_in_threadpool(
            partial(
                dispatch_spatial_search_api,
                request.app.state.spatial_search_service,
                request.method,
                request.url.path,
                payload,
                enabled_features=request.app.state.enabled_features,
            )
        )
        return response(result)
    if request.url.path.startswith("/api/frame-analysis/"):
        result = await run_in_threadpool(
            partial(
                dispatch_frame_analysis_api,
                request.app.state.frame_analysis_service,
                request.method,
                request.url.path,
                payload,
                enabled_features=request.app.state.enabled_features,
                actor_id=principal.actor_id,
                actor_role=principal.role,
                request_id=REQUEST_ID.get(),
            )
        )
        return response(result)
    if request.url.path.startswith("/api/evidence-ai/"):
        result = await run_in_threadpool(
            partial(
                dispatch_evidence_ai_api,
                request.app.state.evidence_ai_service,
                request.method,
                request.url.path,
                payload,
                enabled_features=request.app.state.enabled_features,
            )
        )
        return response(result)
    if request.url.path.startswith("/api/change-review/"):
        result = await run_in_threadpool(
            partial(
                dispatch_change_review_api,
                request.app.state.change_review_service,
                request.method,
                request.url.path,
                payload,
                enabled_features=request.app.state.enabled_features,
                actor_id=principal.actor_id,
            )
        )
        return response(result)
    result = await run_in_threadpool(
        partial(
            dispatch_preview_api,
            request.app.state.preview_service,
            request.method,
            request.url.path,
            payload,
            server_version=SERVER_VERSION,
            enabled_features=request.app.state.enabled_features,
        )
    )
    return response(result)


async def _metrics(request: Request) -> Response:
    if request.state.principal.role != "admin":
        return _failure(HTTPStatus.FORBIDDEN, PermissionError("spatial_admin_role_required"))
    return Response(
        request.app.state.request_metrics.prometheus_text(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


async def _stream_to_service(
    request: Request,
    content_length: int,
    callback: Callable[[Any], Any],
) -> Any:
    """Bridge an ASGI body to a synchronous reader without buffering it.

    An OS pipe provides bounded backpressure.  Large video/SPZ uploads are
    therefore written once by the canonical storage adapter rather than copied
    into an additional temporary request file.
    """

    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "rb", buffering=0)
    writer = os.fdopen(write_fd, "wb", buffering=0)

    def invoke() -> Any:
        with reader:
            return callback(reader)

    worker = asyncio.create_task(asyncio.to_thread(invoke))
    total = 0
    producer_error: BaseException | None = None
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            if worker.done():
                return await worker
            total += len(chunk)
            if total > content_length:
                raise ValueError("request_body_exceeds_content_length")
            await asyncio.to_thread(writer.write, chunk)
        if total != content_length:
            raise ValueError("request_body_incomplete")
    except BaseException as error:  # cancellation must close the pipe as well
        producer_error = error
    finally:
        try:
            writer.close()
        except BrokenPipeError:
            pass

    try:
        result = await worker
    except BaseException:
        raise
    if producer_error is not None:
        raise producer_error
    return result


def _content_length(request: Request) -> int:
    value = request.headers.get("content-length", "")
    try:
        length = int(value)
    except ValueError as error:
        raise ValueError("request_content_length_invalid") from error
    if length <= 0:
        raise ValueError("request_content_empty")
    return length


async def _upload_video(request: Request) -> JSONResponse:
    require_feature(request.app.state.enabled_features, "pipeline")
    filename = request.query_params.get("filename", "")
    content_length = _content_length(request)
    service = request.app.state.preview_service
    result = await _stream_to_service(
        request,
        content_length,
        lambda stream: service.store_uploaded_video(filename, stream, content_length),
    )
    return JSONResponse(status_code=HTTPStatus.OK, content=json_hashable(result))


async def _import_spz(request: Request) -> JSONResponse:
    require_feature(request.app.state.enabled_features, "spz-import")
    filename = request.query_params.get("filename", "")
    scene_id = request.query_params.get("scene_id", "")
    reconstruction_id = request.query_params.get("reconstruction_id", "")
    coordinate_contract_id = request.query_params.get("coordinate_contract_id")
    content_length = _content_length(request)
    service = request.app.state.preview_service
    result = await _stream_to_service(
        request,
        content_length,
        lambda stream: service.import_gaussian_asset(
            filename,
            stream,
            content_length,
            scene_id,
            reconstruction_id,
            coordinate_contract_id,
        ),
    )
    return JSONResponse(status_code=HTTPStatus.OK, content=json_hashable(result))


async def _detect_apriltag(request: Request) -> JSONResponse:
    require_feature(request.app.state.enabled_features, "apriltag-check")
    content_length = _content_length(request)
    if content_length > MAX_LIVE_FRAME_BYTES:
        raise ValueError("apriltag_live_frame_size_invalid")
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise ValueError("apriltag_live_frame_content_type_invalid")
    body = await request.body()
    if len(body) != content_length or len(body) > MAX_LIVE_FRAME_BYTES:
        raise ValueError("apriltag_live_frame_size_invalid")
    required = {
        name: request.query_params.get(name, "")
        for name in ("family", "tag_size_m", "fx", "fy", "cx", "cy")
    }
    service = request.app.state.preview_service
    result = await run_in_threadpool(
        service.detect_apriltag_frame,
        body,
        family=required["family"],
        tag_size_m=required["tag_size_m"],
        fx=required["fx"],
        fy=required["fy"],
        cx=required["cx"],
        cy=required["cy"],
        distortion_coefficients=request.query_params.get("distortion_coefficients", ""),
    )
    return JSONResponse(status_code=HTTPStatus.OK, content=json_hashable(result))


async def _upload_field_image(request: Request) -> JSONResponse:
    require_feature(request.app.state.enabled_features, "field-memory")
    filename = request.query_params.get("filename", "")
    title = request.query_params.get("title")
    content_length = _content_length(request)
    if content_length > MAX_STORED_IMAGE_BYTES:
        raise ValueError("field_memory_image_size_invalid")
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise ValueError("field_memory_image_content_type_invalid")
    service = request.app.state.field_memory_service
    principal = request.state.principal
    asset = await run_in_threadpool(service.asset, request.path_params["asset_id"])
    enforce_site_scope(principal, None, asset)
    result = await _stream_to_service(
        request,
        content_length,
        lambda stream: service.store_asset_image(
            request.path_params["asset_id"],
            filename,
            stream,
            content_length,
            title=title,
        ),
    )
    enforce_site_scope(principal, None, asset)
    await run_in_threadpool(
        service.append_audit_event,
        actor_id=principal.actor_id, role=principal.role, site_id=asset["site_id"],
        action="POST /api/field-memory/assets/{asset_id}/images", target=result["document_id"],
        request_id=REQUEST_ID.get(), result="success", details={"content_sha256": result["content_sha256"]},
    )
    return JSONResponse(status_code=HTTPStatus.OK, content=json_hashable(result))


async def _upload_field_document(request: Request) -> JSONResponse:
    require_feature(request.app.state.enabled_features, "field-memory")
    filename = request.query_params.get("filename", "")
    title = request.query_params.get("title")
    content_length = _content_length(request)
    if content_length > MAX_STORED_DOCUMENT_BYTES:
        raise ValueError("field_memory_document_size_invalid")
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type not in {"application/pdf", "image/jpeg", "image/png", "image/webp"}:
        raise ValueError("field_memory_document_content_type_invalid")
    service = request.app.state.field_memory_service
    principal = request.state.principal
    asset = await run_in_threadpool(service.asset, request.path_params["asset_id"])
    enforce_site_scope(principal, None, asset)
    result = await _stream_to_service(
        request,
        content_length,
        lambda stream: service.store_asset_document(
            request.path_params["asset_id"], filename, stream, content_length, title=title,
        ),
    )
    await run_in_threadpool(
        service.append_audit_event,
        actor_id=principal.actor_id, role=principal.role, site_id=asset["site_id"],
        action="POST /api/field-memory/assets/{asset_id}/files", target=result["document_id"],
        request_id=REQUEST_ID.get(), result="success", details={"content_sha256": result["content_sha256"]},
    )
    return JSONResponse(status_code=HTTPStatus.OK, content=json_hashable(result))


async def _query_similar_image(request: Request) -> JSONResponse:
    require_feature(request.app.state.enabled_features, "spatial-search")
    content_length = _content_length(request)
    if content_length > MAX_STORED_IMAGE_BYTES:
        raise ValueError("image_feature_query_size_invalid")
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise ValueError("image_feature_query_content_type_invalid")
    body = await request.body()
    if len(body) != content_length:
        raise ValueError("image_feature_query_size_invalid")
    raw_limit = request.query_params.get("limit", "20")
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError) as error:
        raise ValueError("spatial_search_limit_invalid") from error
    principal = request.state.principal
    site_payload = {"site_id": request.query_params.get("site_id", "")}
    enforce_site_scope(principal, site_payload, {})
    result = await run_in_threadpool(
        request.app.state.image_feature_service.query,
        request.query_params.get("site_id", ""),
        body,
        limit=limit,
    )
    enforce_site_scope(principal, site_payload, result)
    return JSONResponse(status_code=HTTPStatus.OK, content=json_hashable(result))


async def _download_app_package(request: Request) -> FileResponse:
    require_feature(request.app.state.enabled_features, "package-builder")
    path = await run_in_threadpool(
        request.app.state.preview_service.app_package_download,
        request.path_params["filename"],
    )
    return FileResponse(
        path,
        media_type="application/zip",
        filename=path.name,
        headers={"Cache-Control": "no-store"},
    )


async def _download_content_pack(request: Request) -> FileResponse:
    require_feature(request.app.state.enabled_features, "package-builder")
    path = await run_in_threadpool(
        request.app.state.preview_service.content_pack_download,
        request.path_params["content_sha256"],
    )
    return FileResponse(
        path,
        media_type="application/zip",
        filename=path.name,
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


async def _download_field_report(request: Request) -> FileResponse:
    require_feature(request.app.state.enabled_features, "field-memory")
    from .field_reports import resolve_report_file

    service = request.app.state.field_memory_service
    path, media_type, site_id = await run_in_threadpool(
        resolve_report_file,
        service,
        request.path_params["report_id"],
        request.path_params["filename"],
    )
    enforce_site_scope(request.state.principal, {"site_id": site_id}, {})
    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name,
        headers={"Cache-Control": "private, no-store"},
    )


async def _frame_analysis_content(request: Request) -> FileResponse:
    require_feature(request.app.state.enabled_features, "frame-analysis", "spatial-search")
    service = request.app.state.field_memory_service
    digest = request.path_params["content_sha256"]
    site_ids = await run_in_threadpool(service.frame_content_site_ids, digest)
    if not site_ids:
        raise FileNotFoundError("frame_analysis_content_not_found")
    principal = request.state.principal
    if principal.role != "admin" and principal.site_id not in site_ids:
        raise PermissionError("spatial_site_scope_forbidden")
    path = service.content_path(digest)
    if not path.is_file():
        raise FileNotFoundError("frame_analysis_content_not_found")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=31536000, immutable"})


async def _frame_analysis_candidate_preview(request: Request) -> Response:
    """Serve a verified registered image only for the bounded candidate picker."""

    require_feature(request.app.state.enabled_features, "frame-analysis")
    analysis_service = request.app.state.frame_analysis_service
    scene_version_id = request.path_params["scene_version_id"]
    scene = await run_in_threadpool(analysis_service.scene_version, scene_version_id)
    enforce_site_scope(request.state.principal, {"site_id": scene["site_id"]}, scene)
    engine = RegisteredFrameAnalysisEngine(ROOT, analysis_service, None)
    content = await run_in_threadpool(
        engine.candidate_preview,
        scene_version_id,
        request.path_params["candidate_key"],
    )
    return Response(
        content=content,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=60"},
    )


def create_app(
    repo_root: Path = ROOT,
    enabled_features: str | frozenset[str] = "suite",
    *,
    service: Any | None = None,
    field_memory_service: Any | None = None,
    spatial_search_service: Any | None = None,
    image_feature_service: Any | None = None,
    frame_analysis_service: Any | None = None,
    evidence_ai_service: Any | None = None,
    change_review_service: Any | None = None,
    control_token: str | None = None,
    allowed_origins: str | None = None,
) -> FastAPI:
    """Create an independently testable ASGI application."""

    features = (
        parse_enabled_features(enabled_features)
        if isinstance(enabled_features, str)
        else enabled_features
    )
    app = FastAPI(
        title="Spatial 3DGS Control API",
        version="2.0.0",
        description="Local control plane; canonical artifacts remain file-contract owned.",
    )
    app.state.preview_service = service or PreviewService(repo_root)
    app.state.field_memory_service = field_memory_service or FieldMemoryService(
        repo_root,
        scene_packages_provider=app.state.preview_service.scene_packages,
    )
    app.state.spatial_search_service = spatial_search_service or SpatialSearchService(
        app.state.field_memory_service
    )
    app.state.image_feature_service = image_feature_service or ImageFeatureIndexService(
        app.state.field_memory_service
    )
    app.state.frame_analysis_service = frame_analysis_service or FrameAnalysisService(
        app.state.field_memory_service
    )
    app.state.evidence_ai_service = evidence_ai_service or EvidenceAiService(
        app.state.field_memory_service, app.state.spatial_search_service
    )
    app.state.change_review_service = change_review_service or ChangeReviewService(
        app.state.field_memory_service
    )
    app.state.enabled_features = features
    app.state.control_token = validated_control_token(control_token)
    app.state.request_metrics = RequestMetrics()
    origins = configured_allowed_origins(allowed_origins)
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=[
                "Content-Type", "X-Request-ID", "X-Spatial-Control-Token",
                "X-Spatial-Actor-ID", "X-Spatial-Role", "X-Spatial-Site-ID",
            ],
            expose_headers=["X-Request-ID"],
        )

    def protected_openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        components = schema.setdefault("components", {})
        security_schemes = components.setdefault("securitySchemes", {})
        security_schemes["SpatialControlToken"] = {
            "type": "apiKey",
            "in": "header",
            "name": "X-Spatial-Control-Token",
            "description": "Injected by the trusted local Web proxy; never store it in browser code.",
        }
        for path, operations in schema.get("paths", {}).items():
            if path == "/api/health":
                continue
            if isinstance(operations, dict):
                for operation in operations.values():
                    if isinstance(operation, dict) and "responses" in operation:
                        operation["security"] = [{"SpatialControlToken": []}]
        app.openapi_schema = schema
        return schema

    app.openapi = protected_openapi

    @app.middleware("http")
    async def request_observability(request: Request, call_next: Callable[..., Any]) -> Any:
        request_id = accepted_request_id(request.headers.get("x-request-id"))
        token = REQUEST_ID.set(request_id)
        started = time.perf_counter()
        status_code = HTTPStatus.INTERNAL_SERVER_ERROR
        request.app.state.request_metrics.begin()
        try:
            try:
                request.state.principal = principal_from_headers(request.headers)
                if request.url.path.startswith("/api/") and request.url.path != "/api/health":
                    authorize(request.state.principal, request.method, request.url.path)
            except PermissionError as error:
                response = _failure(HTTPStatus.FORBIDDEN, error)
                status_code = response.status_code
                response.headers["X-Request-ID"] = request_id
                return response
            expected_token = request.app.state.control_token
            # Browsers cannot attach the secret header to a CORS preflight.
            # CORSMiddleware validates the requested origin/method/headers;
            # the subsequent state-changing request still requires the token.
            protected_api = (
                request.method != "OPTIONS"
                and request.url.path.startswith("/api/")
                and request.url.path != "/api/health"
            )
            supplied_token = request.headers.get(CONTROL_TOKEN_HEADER, "")
            if protected_api and expected_token is not None and not secrets.compare_digest(supplied_token, expected_token):
                response = _failure(HTTPStatus.UNAUTHORIZED, PermissionError("control_api_token_required"))
            else:
                response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            duration_seconds = time.perf_counter() - started
            route = request.scope.get("route")
            route_template = getattr(route, "path", "unmatched")
            request.app.state.request_metrics.end(
                request.method,
                route_template,
                int(status_code),
                duration_seconds,
            )
            log_event(
                LOGGER,
                logging.INFO,
                "http_request_completed",
                request_id=request_id,
                method=request.method,
                path=request.url.path,
                status_code=int(status_code),
                duration_ms=round(duration_seconds * 1000, 3),
                enabled_features=sorted(features),
                **resource_fields(request.url.path),
            )
            REQUEST_ID.reset(token)

    @app.exception_handler(ImmutableScenePackageError)
    async def immutable_handler(_request: Request, error: ImmutableScenePackageError) -> JSONResponse:
        return _failure(
            HTTPStatus.CONFLICT,
            error,
            immutable=True,
            required_action="create_a_new_scene_package_id_via_verified_mrp_and_spz_import",
        )

    @app.exception_handler(DeprecatedScenePackageApiError)
    async def deprecated_handler(_request: Request, error: DeprecatedScenePackageApiError) -> JSONResponse:
        return _failure(
            HTTPStatus.GONE,
            error,
            required_action="use_verified_mrp_and_spz_import",
        )

    @app.exception_handler(ApplicationFeatureDisabled)
    async def feature_handler(_request: Request, error: ApplicationFeatureDisabled) -> JSONResponse:
        return _failure(HTTPStatus.FORBIDDEN, error)

    @app.exception_handler(PermissionError)
    async def permission_handler(_request: Request, error: PermissionError) -> JSONResponse:
        return _failure(HTTPStatus.FORBIDDEN, error)

    @app.exception_handler(FileExistsError)
    async def conflict_handler(_request: Request, error: FileExistsError) -> JSONResponse:
        return _failure(HTTPStatus.CONFLICT, error, immutable=True)

    @app.exception_handler(FileNotFoundError)
    async def not_found_handler(_request: Request, error: FileNotFoundError) -> JSONResponse:
        return _failure(HTTPStatus.NOT_FOUND, error)

    @app.exception_handler(ValueError)
    @app.exception_handler(SceneConfigError)
    @app.exception_handler(OSError)
    async def bad_request_handler(_request: Request, error: BaseException) -> JSONResponse:
        return _failure(HTTPStatus.BAD_REQUEST, error)

    @app.exception_handler(Exception)
    async def unexpected_handler(request: Request, error: Exception) -> JSONResponse:
        log_event(
            LOGGER,
            logging.ERROR,
            "http_request_failed",
            path=request.url.path,
            error_type=type(error).__name__,
        )
        return _failure(HTTPStatus.INTERNAL_SERVER_ERROR, error)

    @app.get("/", include_in_schema=False)
    @app.get("/app", include_in_schema=False)
    async def redirect_to_viewer(request: Request) -> RedirectResponse:
        host = request.url.hostname or "127.0.0.1"
        return RedirectResponse(f"http://{host}:5173/?preview=pipeline", status_code=HTTPStatus.FOUND)

    router = APIRouter(tags=["control-plane"])
    for method, path in JSON_ROUTES:
        operation_name = method.lower() + "_" + path.strip("/").replace("/", "_").replace("{", "").replace("}", "").replace("-", "_")
        router.add_api_route(
            path,
            _dispatch_json,
            methods=[method],
            name=operation_name,
            operation_id=operation_name,
            response_class=JSONResponse,
        )
    router.add_api_route(
        "/api/metrics",
        _metrics,
        methods=["GET"],
        name="get_api_metrics",
        operation_id="get_api_metrics",
        response_class=Response,
    )
    router.add_api_route(
        "/api/upload-video",
        _upload_video,
        methods=["PUT"],
        name="put_api_upload_video",
        operation_id="put_api_upload_video",
    )
    router.add_api_route(
        "/api/import-gaussian",
        _import_spz,
        methods=["PUT"],
        name="put_api_import_gaussian",
        operation_id="put_api_import_gaussian",
    )
    router.add_api_route(
        "/api/import-spz",
        _import_spz,
        methods=["PUT"],
        name="put_api_import_spz",
        operation_id="put_api_import_spz",
    )
    router.add_api_route(
        "/api/apriltag-detect",
        _detect_apriltag,
        methods=["PUT"],
        name="put_api_apriltag_detect",
        operation_id="put_api_apriltag_detect",
    )
    router.add_api_route(
        "/api/field-memory/assets/{asset_id}/images",
        _upload_field_image,
        methods=["PUT"],
        name="put_api_field_memory_asset_image",
        operation_id="put_api_field_memory_asset_image",
    )
    router.add_api_route(
        "/api/field-memory/assets/{asset_id}/files",
        _upload_field_document,
        methods=["PUT"],
        name="put_api_field_memory_asset_document",
        operation_id="put_api_field_memory_asset_document",
    )
    router.add_api_route(
        "/api/spatial-search/images/query",
        _query_similar_image,
        methods=["PUT"],
        name="put_api_spatial_search_image_query",
        operation_id="put_api_spatial_search_image_query",
    )
    router.add_api_route(
        "/api/app-packages/{filename}/download",
        _download_app_package,
        methods=["GET"],
        name="get_api_app_package_download",
        operation_id="get_api_app_package_download",
    )
    router.add_api_route(
        "/api/content-packs/{content_sha256}/download",
        _download_content_pack,
        methods=["GET"],
        name="get_api_content_pack_download",
        operation_id="get_api_content_pack_download",
    )
    router.add_api_route(
        "/api/field-reports/{report_id}/{filename}",
        _download_field_report,
        methods=["GET"],
        name="get_api_field_report_download",
        operation_id="get_api_field_report_download",
    )
    router.add_api_route(
        "/api/frame-analysis/content/{content_sha256}",
        _frame_analysis_content,
        methods=["GET"],
        name="get_api_frame_analysis_content",
        operation_id="get_api_frame_analysis_content",
    )
    router.add_api_route(
        "/api/frame-analysis/scene-versions/{scene_version_id}/candidates/{candidate_key}/preview",
        _frame_analysis_candidate_preview,
        methods=["GET"],
        name="get_api_frame_analysis_candidate_preview",
        operation_id="get_api_frame_analysis_candidate_preview",
    )
    app.include_router(router)
    return app
