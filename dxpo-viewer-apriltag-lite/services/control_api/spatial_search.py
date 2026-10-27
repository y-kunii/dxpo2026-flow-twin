"""Site-scoped full-text, attribute, time, and spatial search.

The search index is a rebuildable projection of the append-only field-memory
registry.  This adapter never mutates Scene Packages, MRP, SPZ, or Viewer
runtime artifacts.
"""

from __future__ import annotations

import math
import json
import re
from datetime import datetime, timezone
from typing import Any

from .features import require_feature
from .field_memory import FieldMemoryService
from .frame_analysis import FrameAnalysisService


RECORD_TYPES = frozenset(
    {
        "site",
        "scene_version",
        "capture",
        "asset",
        "observation",
        "document",
        "maintenance_record",
        "frame_analysis",
        "change_event",
    }
)
SITE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
SCENE_VERSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
FRAME_REVIEW_STATUSES = frozenset({"unreviewed", "accepted", "corrected", "rejected"})


def _site_id(value: Any) -> str:
    if not isinstance(value, str) or SITE_ID_RE.fullmatch(value) is None:
        raise ValueError("spatial_search_site_id_invalid")
    return value


def _scene_version_id(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or SCENE_VERSION_ID_RE.fullmatch(value) is None:
        raise ValueError("spatial_search_scene_version_id_invalid")
    return value


def _limit(value: Any, default: int = 50) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise ValueError("spatial_search_limit_invalid")
    return value


def _offset(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("spatial_search_offset_invalid")
    return value


def _timestamp(value: Any, field: str) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError(f"spatial_search_{field}_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (
            parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
        )
    except ValueError as error:
        raise ValueError(f"spatial_search_{field}_invalid") from error


def _record_types(value: Any) -> frozenset[str]:
    if value is None:
        return RECORD_TYPES
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("spatial_search_record_types_invalid")
    result = frozenset(value)
    if not result or not result.issubset(RECORD_TYPES):
        raise ValueError("spatial_search_record_types_invalid")
    return result


def _frame_review_status(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or value not in FRAME_REVIEW_STATUSES:
        raise ValueError("spatial_search_frame_review_status_invalid")
    return value


def _finite_optional(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"spatial_search_{field}_invalid")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"spatial_search_{field}_invalid")
    return result


def _spatial_bounds(value: Any) -> dict[str, dict[str, float | None]] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("spatial_search_spatial_bounds_invalid")
    result: dict[str, dict[str, float | None]] = {"min": {}, "max": {}}
    for edge in ("min", "max"):
        raw = value.get(edge, {})
        if not isinstance(raw, dict):
            raise ValueError("spatial_search_spatial_bounds_invalid")
        for axis in "xyz":
            result[edge][axis] = _finite_optional(
                raw.get(axis), f"spatial_{edge}_{axis}"
            )
            low = result["min"].get(axis)
            high = result["max"].get(axis)
            if low is not None and high is not None and low > high:
                raise ValueError(f"spatial_search_spatial_{axis}_range_invalid")
    return result


def _fts_query(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value.strip()) > 256:
        raise ValueError("spatial_search_query_invalid")
    tokens = [token for token in re.split(r"\s+", value.strip()) if token]
    if not tokens:
        return None
    return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def _iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (
            parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
        )
    except ValueError:
        return None


class SpatialSearchService:
    """Read-only query facade over a FieldMemoryService."""

    def __init__(self, field_memory: FieldMemoryService) -> None:
        self.field_memory = field_memory
        self.frame_analysis = FrameAnalysisService(field_memory)

    def sites(self) -> list[dict[str, Any]]:
        return self.field_memory.sites()

    def search(self, payload: dict[str, Any]) -> dict[str, Any]:
        site_id = _site_id(payload.get("site_id"))
        scene_version_id = _scene_version_id(payload.get("scene_version_id"))
        query = _fts_query(payload.get("query"))
        record_types = _record_types(payload.get("record_types"))
        limit = _limit(payload.get("limit"))
        offset = _offset(payload.get("offset"))
        from_time = _timestamp(payload.get("from"), "from")
        to_time = _timestamp(payload.get("to"), "to")
        if from_time is not None and to_time is not None and from_time > to_time:
            raise ValueError("spatial_search_time_range_invalid")
        asset_type = payload.get("asset_type")
        status = payload.get("status")
        frame_review_status = _frame_review_status(payload.get("frame_review_status"))
        if asset_type is not None and (
            not isinstance(asset_type, str) or len(asset_type) > 64
        ):
            raise ValueError("spatial_search_asset_type_invalid")
        if status is not None and (not isinstance(status, str) or len(status) > 64):
            raise ValueError("spatial_search_status_invalid")
        bounds = _spatial_bounds(payload.get("spatial_bounds"))

        with self.field_memory.read_connection() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM sites WHERE site_id = ?", (site_id,)
                ).fetchone()
                is None
            ):
                raise FileNotFoundError(f"field_memory_site_not_found:{site_id}")
            # The registered-frame UI always searches one SceneVersion.  Apply
            # that boundary in SQL before LIMIT so records from a busy site do
            # not crowd the requested SceneVersion out of the candidate set.
            frame_scene_filter = (
                scene_version_id is not None
                and record_types == frozenset({"frame_analysis"})
            )
            if frame_review_status is not None and not frame_scene_filter:
                raise ValueError(
                    "spatial_search_frame_review_status_requires_frame_scene_scope"
                )
            frame_page_filter = (
                frame_scene_filter
                and from_time is None
                and to_time is None
                and asset_type is None
                and status is None
                and bounds is None
            )
            total_count: int | None = None
            review_clause = ""
            review_parameters: tuple[Any, ...] = ()
            if frame_review_status == "unreviewed":
                review_clause = (
                    " AND NOT EXISTS (SELECT 1 FROM frame_analysis_review_events AS reviews "
                    "WHERE reviews.frame_analysis_id = frames.frame_analysis_id)"
                )
            elif frame_review_status is not None:
                review_clause = (
                    " AND (SELECT reviews.decision FROM frame_analysis_review_events AS reviews "
                    "WHERE reviews.frame_analysis_id = frames.frame_analysis_id "
                    "ORDER BY reviews.created_at DESC, reviews.rowid DESC LIMIT 1) = ?"
                )
                review_parameters = (frame_review_status,)
            if query is None and frame_page_filter:
                frame_scope_sql = (
                    "site_id = ? AND record_type = 'frame_analysis' AND record_id IN ("
                    "SELECT frames.frame_analysis_id FROM frame_analyses AS frames "
                    "JOIN scene_analysis_activations AS active "
                    "ON active.scene_version_id = frames.scene_version_id "
                    "AND active.analysis_run_id = frames.analysis_run_id "
                    "WHERE frames.scene_version_id = ?" + review_clause + ")"
                )
                total_count = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM field_memory_fts WHERE {frame_scope_sql}",
                        (site_id, scene_version_id, *review_parameters),
                    ).fetchone()[0]
                )
                rows = connection.execute(
                    "SELECT record_type, record_id, site_id, title, body, 0.0 AS rank "
                    f"FROM field_memory_fts WHERE {frame_scope_sql} "
                    "ORDER BY rowid DESC LIMIT ? OFFSET ?",
                    (site_id, scene_version_id, *review_parameters, limit, offset),
                ).fetchall()
            elif query is not None and frame_page_filter:
                frame_scope_sql = (
                    "field_memory_fts MATCH ? AND site_id = ? "
                    "AND record_type = 'frame_analysis' AND record_id IN ("
                    "SELECT frames.frame_analysis_id FROM frame_analyses AS frames "
                    "JOIN scene_analysis_activations AS active "
                    "ON active.scene_version_id = frames.scene_version_id "
                    "AND active.analysis_run_id = frames.analysis_run_id "
                    "WHERE frames.scene_version_id = ?" + review_clause + ")"
                )
                total_count = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM field_memory_fts WHERE {frame_scope_sql}",
                        (query, site_id, scene_version_id, *review_parameters),
                    ).fetchone()[0]
                )
                rows = connection.execute(
                    "SELECT record_type, record_id, site_id, title, body, "
                    "bm25(field_memory_fts) AS rank FROM field_memory_fts "
                    f"WHERE {frame_scope_sql} ORDER BY rank LIMIT ? OFFSET ?",
                    (
                        query,
                        site_id,
                        scene_version_id,
                        *review_parameters,
                        limit,
                        offset,
                    ),
                ).fetchall()
            elif query is None:
                if offset != 0:
                    raise ValueError("spatial_search_offset_requires_frame_scene_scope")
                rows = connection.execute(
                    "SELECT record_type, record_id, site_id, title, body, 0.0 AS rank "
                    "FROM field_memory_fts WHERE site_id = ? ORDER BY rowid DESC LIMIT 500",
                    (site_id,),
                ).fetchall()
            else:
                if offset != 0:
                    raise ValueError("spatial_search_offset_requires_frame_scene_scope")
                rows = connection.execute(
                    "SELECT record_type, record_id, site_id, title, body, bm25(field_memory_fts) AS rank "
                    "FROM field_memory_fts WHERE field_memory_fts MATCH ? AND site_id = ? "
                    "ORDER BY rank LIMIT 500",
                    (query, site_id),
                ).fetchall()

            results: list[dict[str, Any]] = []
            for row in rows:
                if row["record_type"] not in record_types:
                    continue
                item = self._hydrate(connection, row["record_type"], row["record_id"])
                if item is None or item["site_id"] != site_id:
                    continue
                if (
                    scene_version_id is not None
                    and item.get("scene_version_id") != scene_version_id
                ):
                    continue
                if asset_type is not None and item.get("asset_type") != asset_type:
                    continue
                if status is not None and item.get("asset_status") != status:
                    continue
                created = _iso_datetime(item.get("timestamp"))
                if from_time is not None and (created is None or created < from_time):
                    continue
                if to_time is not None and (created is None or created > to_time):
                    continue
                if bounds is not None and not self._inside_bounds(
                    item.get("focus"), bounds
                ):
                    continue
                item["rank"] = float(row["rank"])
                results.append(item)
                if total_count is None and len(results) >= limit:
                    break

        if total_count is None:
            total_count = len(results)

        return {
            "site_id": site_id,
            "scene_version_id": scene_version_id,
            "query": payload.get("query", ""),
            "count": len(results),
            "total_count": total_count,
            "offset": offset,
            "limit": limit,
            "results": results,
        }

    def timeline(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = dict(payload)
        request["query"] = ""
        request.setdefault(
            "record_types",
            [
                "scene_version",
                "capture",
                "asset",
                "observation",
                "document",
                "maintenance_record",
                "frame_analysis",
                "change_event",
            ],
        )
        request.setdefault("limit", 100)
        result = self.search(request)
        result["results"].sort(
            key=lambda item: item.get("timestamp") or "", reverse=True
        )
        result["timeline"] = result.pop("results")
        return result

    @staticmethod
    def _inside_bounds(
        focus: dict[str, float] | None,
        bounds: dict[str, dict[str, float | None]],
    ) -> bool:
        if focus is None:
            return False
        for axis in "xyz":
            value = focus[axis]
            low = bounds["min"][axis]
            high = bounds["max"][axis]
            if low is not None and value < low:
                return False
            if high is not None and value > high:
                return False
        return True

    def _hydrate(
        self, connection: Any, record_type: str, record_id: str
    ) -> dict[str, Any] | None:
        if record_type == "site":
            row = connection.execute(
                "SELECT * FROM sites WHERE site_id = ?", (record_id,)
            ).fetchone()
            return (
                None
                if row is None
                else {
                    "record_type": "site",
                    "record_id": record_id,
                    "site_id": record_id,
                    "title": row["name"],
                    "summary": row["description"],
                    "timestamp": row["created_at"],
                }
            )
        if record_type == "scene_version":
            row = connection.execute(
                "SELECT * FROM scene_versions WHERE scene_version_id = ?", (record_id,)
            ).fetchone()
            return (
                None
                if row is None
                else {
                    "record_type": "scene_version",
                    "record_id": record_id,
                    "site_id": row["site_id"],
                    "title": row["label"],
                    "summary": row["scene_package_id"],
                    "scene_package_id": row["scene_package_id"],
                    "timestamp": row["captured_at"] or row["created_at"],
                    "viewer_url": f"/?scene_package={row['scene_package_id']}",
                }
            )
        if record_type == "capture":
            row = connection.execute(
                "SELECT * FROM captures WHERE capture_id = ?", (record_id,)
            ).fetchone()
            return (
                None
                if row is None
                else {
                    "record_type": "capture",
                    "record_id": record_id,
                    "site_id": row["site_id"],
                    "title": row["source_type"],
                    "summary": row["memo"],
                    "scene_version_id": row["scene_version_id"],
                    "timestamp": row["captured_at"],
                }
            )

        if record_type == "asset":
            row = connection.execute(
                "SELECT * FROM assets WHERE asset_id = ?", (record_id,)
            ).fetchone()
            if row is None:
                return None
            observation = connection.execute(
                "SELECT observations.*, scene_versions.scene_package_id FROM asset_observations AS observations "
                "JOIN scene_versions USING(scene_version_id) WHERE observations.asset_id = ? "
                "ORDER BY observations.created_at DESC LIMIT 1",
                (record_id,),
            ).fetchone()
            return SpatialSearchService._asset_item(row, observation)

        if record_type == "observation":
            row = connection.execute(
                "SELECT observations.*, assets.site_id, assets.name, assets.asset_type, assets.status, "
                "scene_versions.scene_package_id FROM asset_observations AS observations "
                "JOIN assets USING(asset_id) JOIN scene_versions USING(scene_version_id) "
                "WHERE observations.observation_id = ?",
                (record_id,),
            ).fetchone()
            if row is None:
                return None
            focus = {axis: float(row[f"position_{axis}"]) for axis in "xyz"}
            return {
                "record_type": "observation",
                "record_id": record_id,
                "site_id": row["site_id"],
                "title": f"{row['name']} position observation",
                "summary": row["source"],
                "asset_id": row["asset_id"],
                "asset_type": row["asset_type"],
                "asset_status": row["status"],
                "scene_version_id": row["scene_version_id"],
                "scene_package_id": row["scene_package_id"],
                "timestamp": row["created_at"],
                "focus": focus,
                "viewer_url": SpatialSearchService._viewer_url(
                    row["scene_package_id"], focus
                ),
            }

        if record_type == "document":
            row = connection.execute(
                "SELECT documents.*, assets.site_id, assets.name, assets.asset_type, assets.status "
                "FROM asset_documents AS documents JOIN assets USING(asset_id) WHERE documents.document_id = ?",
                (record_id,),
            ).fetchone()
            if row is None:
                return None
            observation = connection.execute(
                "SELECT observations.*, scene_versions.scene_package_id FROM asset_observations AS observations "
                "JOIN scene_versions USING(scene_version_id) WHERE observations.asset_id = ? "
                "ORDER BY observations.created_at DESC LIMIT 1",
                (row["asset_id"],),
            ).fetchone()
            result = {
                "record_type": "document",
                "record_id": record_id,
                "site_id": row["site_id"],
                "title": row["title"],
                "summary": row["memo"],
                "uri": row["uri"],
                "kind": row["kind"],
                "asset_id": row["asset_id"],
                "asset_type": row["asset_type"],
                "asset_status": row["status"],
                "timestamp": row["created_at"],
            }
            SpatialSearchService._add_observation_link(result, observation)
            return result
        if record_type == "maintenance_record":
            row = connection.execute(
                "SELECT records.*, assets.site_id, assets.name, assets.asset_type, assets.status AS asset_status "
                "FROM maintenance_records AS records JOIN assets USING(asset_id) "
                "WHERE records.maintenance_record_id = ?",
                (record_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "record_type": "maintenance_record",
                "record_id": record_id,
                "site_id": row["site_id"],
                "title": row["summary"],
                "summary": row["status"],
                "asset_id": row["asset_id"],
                "asset_type": row["asset_type"],
                "asset_status": row["asset_status"],
                "timestamp": row["performed_at"],
            }
        if record_type == "frame_analysis":
            row = connection.execute(
                "SELECT frames.frame_analysis_id "
                "FROM frame_analyses AS frames "
                "JOIN scene_analysis_activations AS active "
                "ON active.scene_version_id = frames.scene_version_id "
                "AND active.analysis_run_id = frames.analysis_run_id "
                "WHERE frames.frame_analysis_id = ?",
                (record_id,),
            ).fetchone()
            if row is None:
                # Search only exposes the explicitly activated analysis run.
                return None
            frame = self.frame_analysis.frame(record_id)
            pose = frame["pose"]
            focus = dict(pose["position"])
            return {
                "record_type": "frame_analysis",
                "record_id": record_id,
                "site_id": frame["site_id"],
                "title": frame["image_name"],
                "summary": frame["summary"],
                "keywords": frame["keywords"],
                "model_summary": frame["model_summary"],
                "extra": frame.get("extra", {}),
                "analysis_policy_id": frame.get("analysis_policy_id"),
                "output_schema_version": frame.get("output_schema_version"),
                "observation_items": frame.get("observation_items", []),
                "review": frame.get("review"),
                "review_status": frame.get("review_status", "unreviewed"),
                "analysis_run_id": frame["analysis_run_id"],
                "scene_version_id": frame["scene_version_id"],
                "scene_package_id": frame["scene_package_id"],
                "source_frame_index": int(frame["source_frame_index"]),
                "source_time_sec": float(frame["source_time_sec"]),
                "camera_id": frame["camera_id"],
                "timestamp": frame["created_at"],
                "focus": focus,
                "pose": pose,
                "projection": frame["projection"],
                "image_url": frame["image_url"],
                "thumbnail_url": frame["thumbnail_url"],
                "viewer_url": frame["viewer_url"],
                "pose_coordinate_contract": frame["pose_coordinate_contract"],
                "pose_compatibility_adjusted": frame["pose_compatibility_adjusted"],
            }
        if record_type == "change_event":
            row = connection.execute(
                "SELECT events.*, assets.name, assets.asset_type, assets.status AS asset_status "
                "FROM confirmed_change_events AS events JOIN assets USING(asset_id) "
                "WHERE events.change_event_id = ?",
                (record_id,),
            ).fetchone()
            if row is None:
                return None
            change = json.loads(row["change_json"])
            return {
                "record_type": "change_event",
                "record_id": record_id,
                "site_id": row["site_id"],
                "title": f"{row['name']} confirmed change",
                "summary": json.dumps(change, ensure_ascii=False),
                "asset_id": row["asset_id"],
                "asset_type": row["asset_type"],
                "asset_status": row["asset_status"],
                "timestamp": row["confirmed_at"],
            }
        return None

    @staticmethod
    def _asset_item(row: Any, observation: Any | None) -> dict[str, Any]:
        result = {
            "record_type": "asset",
            "record_id": row["asset_id"],
            "site_id": row["site_id"],
            "title": row["name"],
            "summary": row["description"],
            "asset_id": row["asset_id"],
            "asset_type": row["asset_type"],
            "asset_status": row["status"],
            "timestamp": row["created_at"],
        }
        SpatialSearchService._add_observation_link(result, observation)
        return result

    @staticmethod
    def _add_observation_link(result: dict[str, Any], observation: Any | None) -> None:
        if observation is None:
            return
        focus = {axis: float(observation[f"position_{axis}"]) for axis in "xyz"}
        result["focus"] = focus
        result["scene_version_id"] = observation["scene_version_id"]
        result["scene_package_id"] = observation["scene_package_id"]
        result["viewer_url"] = SpatialSearchService._viewer_url(
            observation["scene_package_id"], focus
        )

    @staticmethod
    def _viewer_url(scene_package_id: str, focus: dict[str, float]) -> str:
        coordinates = ",".join(format(focus[axis], ".12g") for axis in "xyz")
        return f"/?scene_package={scene_package_id}&focus={coordinates}"


def dispatch_spatial_search_api(
    service: SpatialSearchService,
    method: str,
    path: str,
    payload: dict[str, Any] | None,
    *,
    enabled_features: frozenset[str],
) -> Any:
    """Dispatch the closed, read-only spatial-search route family."""

    require_feature(enabled_features, "spatial-search")
    body = payload or {}
    if method == "GET" and path == "/api/spatial-search/sites":
        return service.sites()
    if method == "POST" and path == "/api/spatial-search/query":
        return service.search(body)
    if method == "POST" and path == "/api/spatial-search/timeline":
        return service.timeline(body)
    raise FileNotFoundError(f"api_endpoint_not_found:{method}:{path}")
