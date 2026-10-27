"""Registered COLMAP-frame analysis in immutable Viewer world coordinates.

The service owns only analysis metadata and evidence attachments below the
Scene Repository's ``registries/site_records`` area. Scene Packages, MRP, COLMAP output and Viewer runtime
are read-only inputs.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol

from PIL import Image, ImageOps

from services.vlm_model_catalog import DEFAULT_VLM, reviewed_vlm
from services.vlm_model_manager import VlmModelManager
from packages.scene_repository import SceneRepositoryLayout

from .features import require_feature
from .frame_analysis_policies import (
    DEFAULT_ANALYSIS_POLICY_ID,
    LEGACY_ANALYSIS_POLICY_ID,
    analysis_policy_catalog,
    normalize_policy_extra,
    normalized_observation_items,
    reviewed_analysis_policy,
)
from .field_memory import (
    SHA256_RE,
    FieldMemoryService,
    _finite_number,
    _identifier,
    _now,
    _pose,
    _row,
    _text,
)


DEFAULT_INTERVAL_SEC = 3.0
DEFAULT_SIMILARITY_THRESHOLD = 0.95
DEFAULT_PROMPT_VERSION = "spatial-observation-v2"
DEFAULT_MODEL_ID = DEFAULT_VLM.model_id
SELECTION_POLICY_VERSION = "interval-dhash-v1"
SELECTED_IMAGES_POLICY_VERSION = "selected-images-v1"
REGISTERED_CANDIDATES_POLICY_VERSION = "registered-candidates-v1"
ALL_REGISTERED_IMAGES_POLICY_VERSION = "all-registered-images-v1"
REVIEW_DECISIONS = frozenset({"accepted", "corrected", "rejected"})
PERSON_MASK_DATASET_SCHEMA_VERSION = "1.1.0"
POSE_COORDINATE_CONTRACT_VERSION = 1
POSE_COORDINATE_STAGE = "viewer_world"
FRAME_NAME_RE = re.compile(r"(?:^|/)frame_(\d+)\.[A-Za-z0-9]+$")


class FrameAnalysisCancelled(RuntimeError):
    """Raised by a worker when an operator stops an analysis run."""


def _positive_number(value: Any, field: str, default: float) -> float:
    if value is None:
        return default
    result = _finite_number(value, f"frame_analysis_{field}")
    if result <= 0:
        raise ValueError(f"frame_analysis_{field}_must_be_positive")
    return result


def _threshold(value: Any) -> float:
    result = (
        DEFAULT_SIMILARITY_THRESHOLD
        if value is None
        else _finite_number(value, "frame_analysis_similarity_threshold")
    )
    if not 0.0 <= result <= 1.0:
        raise ValueError("frame_analysis_similarity_threshold_invalid")
    return result


def _json_object(
    value: Any, field: str, *, default: dict[str, Any] | None = None
) -> dict[str, Any]:
    if value is None:
        return dict(default or {})
    if not isinstance(value, dict):
        raise ValueError(f"frame_analysis_{field}_must_be_object")
    # Round-trip also rejects values that cannot be persisted reproducibly.
    try:
        json.dumps(value, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as error:
        raise ValueError(f"frame_analysis_{field}_invalid") from error
    return dict(value)


def _json_list_of_strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"frame_analysis_{field}_invalid")
    result: list[str] = []
    for item in value:
        normalized = item.strip()
        if normalized and normalized not in result:
            result.append(normalized[:120])
    return result[:64]


def _optional_analysis_text(value: Any, field: str, *, maximum: int = 512) -> str:
    if value in (None, ""):
        return ""
    return _text(value, field, required=False, maximum=maximum)


def _structured_observation_extra(
    value: Any,
    analysis_policy_id: str = DEFAULT_ANALYSIS_POLICY_ID,
) -> dict[str, Any]:
    """Normalize VLM output into evidence-oriented, non-authoritative observations."""
    return normalize_policy_extra(analysis_policy_id, value)


_ANALYSIS_SUMMARY_TEXT_KEYS = (
    "summary",
    "text",
    "content",
    "description",
    "caption",
)


def _analysis_summary(value: Any) -> str:
    """Accept a textual summary or a known textual wrapper from the VLM.

    Some reviewed VLMs return the requested ``summary`` as a nested JSON object
    even though the application contract stores it as text.  Unknown objects and
    arrays remain schema errors; only explicitly named text fields are flattened.
    """

    if isinstance(value, str):
        return _text(value, "analysis_summary", maximum=8000)
    if not isinstance(value, dict):
        return _text(value, "analysis_summary", maximum=8000)

    pieces: list[str] = []

    def collect(candidate: Any) -> None:
        if isinstance(candidate, str):
            normalized = candidate.strip()
            if normalized and normalized not in pieces:
                pieces.append(normalized)
            return
        if isinstance(candidate, dict):
            for key in _ANALYSIS_SUMMARY_TEXT_KEYS:
                if key in candidate:
                    collect(candidate[key])
            return
        if isinstance(candidate, (list, tuple)):
            for item in candidate:
                collect(item)

    collect(value)
    if not pieces:
        return _text(value, "analysis_summary", maximum=8000)
    return _text("\n".join(pieces), "analysis_summary", maximum=8000)


def _selected_image_names(value: Any) -> list[str]:
    """Validate an explicit, append-only selection of registered source images."""

    if not isinstance(value, list) or not value:
        raise ValueError("frame_analysis_selected_image_names_invalid")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("frame_analysis_selected_image_names_invalid")
        normalized = item.strip()
        if not normalized or len(normalized) > 1024 or normalized in result:
            raise ValueError("frame_analysis_selected_image_names_invalid")
        result.append(normalized)
    if len(result) > 64:
        raise ValueError("frame_analysis_selected_image_names_too_many")
    return result


def _strict_unit_quaternion_xyzw(
    value: Any, field: str
) -> tuple[float, float, float, float]:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError(f"frame_analysis_{field}_quaternion_invalid")
    quaternion = tuple(
        _finite_number(component, f"frame_analysis_{field}_quaternion")
        for component in value
    )
    norm = math.sqrt(sum(component * component for component in quaternion))
    if norm == 0.0 or abs(norm - 1.0) > 1e-6:
        raise ValueError(f"frame_analysis_{field}_quaternion_not_unit:tolerance=1e-6")
    return quaternion


@dataclass(frozen=True)
class ScenePoseContract:
    scene_package_id: str
    scene_id: str
    reconstruction_id: str
    package_root: Path
    decoder_id: str
    decoder_quaternion_xyzw: tuple[float, float, float, float]


class FrameAnalysisService:
    """Append-only AnalysisRun/FrameAnalysis registry and active-run switch."""

    def __init__(self, field_memory: FieldMemoryService) -> None:
        self.field_memory = field_memory
        self._pose_contract_cache: dict[str, ScenePoseContract] = {}
        self.model_manager = VlmModelManager(field_memory.repo_root)

    def create_run(
        self,
        payload: dict[str, Any],
        *,
        selection_policy_version: str | None = None,
        base_analysis_run_id: str | None = None,
        inherit_active_frames: bool = False,
    ) -> dict[str, Any]:
        scene_version_id = _identifier(
            _text(payload.get("scene_version_id"), "scene_version_id", maximum=96),
            "scene_version_id",
            prefix="scene",
        )
        analysis_run_id = _identifier(
            payload.get("analysis_run_id"), "analysis_run_id", prefix="analysis"
        )
        model_id = _text(
            payload.get("model_id", DEFAULT_MODEL_ID), "analysis_model_id", maximum=512
        )
        model = reviewed_vlm(model_id)
        model_revision = _text(
            payload.get("model_revision", model.revision),
            "analysis_model_revision",
            maximum=160,
        )
        if model_revision != model.revision:
            raise ValueError(
                f"frame_analysis_model_revision_not_reviewed:{model_id}@{model_revision}"
            )
        analysis_policy = reviewed_analysis_policy(
            payload.get("analysis_policy_id", DEFAULT_ANALYSIS_POLICY_ID)
        )
        requested_prompt_version = payload.get("prompt_version")
        if requested_prompt_version not in (None, analysis_policy.prompt_version):
            raise ValueError("frame_analysis_prompt_version_policy_mismatch")
        prompt_version = analysis_policy.prompt_version
        interval_sec = _positive_number(
            payload.get("interval_sec"), "interval_sec", DEFAULT_INTERVAL_SEC
        )
        similarity_threshold = _threshold(payload.get("similarity_threshold"))
        parameters: dict[str, Any] = {"neutral_background_rgb": [127, 127, 127]}
        parameters.update(_json_object(payload.get("parameters"), "parameters"))
        selected_image_names = payload.get("selected_image_names")
        if selection_policy_version is not None and selected_image_names is not None:
            raise ValueError(
                "frame_analysis_selection_policy_conflicts_with_explicit_selection"
            )
        if selected_image_names is not None:
            parameters["selected_image_names"] = _selected_image_names(
                selected_image_names
            )
        # Contract versions are implementation-owned provenance, not client options.
        parameters["selection_policy_version"] = (
            selection_policy_version
            if selection_policy_version is not None
            else SELECTED_IMAGES_POLICY_VERSION
            if selected_image_names is not None
            else SELECTION_POLICY_VERSION
        )
        parameters["analysis_policy_id"] = analysis_policy.analysis_policy_id
        parameters["prompt_template_id"] = analysis_policy.prompt_template_id
        parameters["prompt_template_sha256"] = analysis_policy.prompt_template_sha256
        parameters["output_schema_version"] = analysis_policy.output_schema_version
        parameters["downstream_uses"] = list(analysis_policy.downstream_uses)
        created_at = _now()
        with self.field_memory._connection(write=True) as connection:
            scene = connection.execute(
                "SELECT site_id, scene_package_id FROM scene_versions WHERE scene_version_id = ?",
                (scene_version_id,),
            ).fetchone()
            if scene is None:
                raise FileNotFoundError(
                    f"field_memory_scene_version_not_found:{scene_version_id}"
                )
            resolved_base_id = base_analysis_run_id
            if resolved_base_id is not None:
                _identifier(resolved_base_id, "base_analysis_run_id", prefix="analysis")
            elif inherit_active_frames:
                active = connection.execute(
                    "SELECT analysis_run_id FROM scene_analysis_activations "
                    "WHERE scene_version_id = ?",
                    (scene_version_id,),
                ).fetchone()
                resolved_base_id = (
                    str(active["analysis_run_id"]) if active is not None else None
                )
            base = None
            if resolved_base_id is not None:
                base = connection.execute(
                    "SELECT runs.analysis_run_id, runs.scene_version_id, runs.model_id, "
                    "runs.model_revision, runs.prompt_version, runs.parameters_json, runs.status, "
                    "COUNT(frames.frame_analysis_id) AS frame_count "
                    "FROM analysis_runs AS runs LEFT JOIN frame_analyses AS frames "
                    "ON frames.analysis_run_id = runs.analysis_run_id "
                    "WHERE runs.analysis_run_id = ? GROUP BY runs.analysis_run_id",
                    (resolved_base_id,),
                ).fetchone()
                if base is None:
                    raise FileNotFoundError(
                        f"frame_analysis_base_run_not_found:{resolved_base_id}"
                    )
                if base["scene_version_id"] != scene_version_id:
                    raise ValueError("frame_analysis_base_run_scene_version_mismatch")
                if base["status"] not in {"completed", "failed", "cancelled"}:
                    raise ValueError("frame_analysis_base_run_not_usable")
                if int(base["frame_count"]) < 1:
                    raise ValueError("frame_analysis_base_run_empty")
                base_parameters = json.loads(base["parameters_json"])
                base_policy_id = base_parameters.get("analysis_policy_id")
                if (
                    base_policy_id != analysis_policy.analysis_policy_id
                    or base_parameters.get("output_schema_version")
                    != analysis_policy.output_schema_version
                    or base["prompt_version"] != analysis_policy.prompt_version
                    or base["model_id"] != model_id
                    or base["model_revision"] != model_revision
                ):
                    raise ValueError("frame_analysis_base_run_contract_mismatch")
                incompatible_count = 0
                for inherited_extra in connection.execute(
                    "SELECT extra_json FROM frame_analyses WHERE analysis_run_id = ?",
                    (base["analysis_run_id"],),
                ):
                    try:
                        raw_extra = json.loads(inherited_extra["extra_json"])
                    except json.JSONDecodeError:
                        incompatible_count += 1
                        continue
                    if (
                        not isinstance(raw_extra, dict)
                        or raw_extra.get("schema_version")
                        != analysis_policy.output_schema_version
                    ):
                        incompatible_count += 1
                if incompatible_count:
                    raise ValueError(
                        f"frame_analysis_base_run_output_contract_mismatch:{incompatible_count}"
                    )
                parameters["continuation"] = {
                    "policy_version": "inherit-existing-and-analyze-missing-v1",
                    "base_analysis_run_id": base["analysis_run_id"],
                    "base_model_id": base["model_id"],
                    "base_model_revision": base["model_revision"],
                    "analysis_policy_id": analysis_policy.analysis_policy_id,
                    "output_schema_version": analysis_policy.output_schema_version,
                    "inherited_output_count": int(base["frame_count"]),
                }
            connection.execute(
                "INSERT INTO analysis_runs(analysis_run_id, site_id, scene_version_id, model_id, "
                "model_revision, prompt_version, status, interval_sec, similarity_threshold, parameters_json, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
                (
                    analysis_run_id,
                    scene["site_id"],
                    scene_version_id,
                    model_id,
                    model_revision,
                    prompt_version,
                    interval_sec,
                    similarity_threshold,
                    json.dumps(
                        parameters, ensure_ascii=False, allow_nan=False, sort_keys=True
                    ),
                    created_at,
                ),
            )
            connection.execute(
                "INSERT INTO analysis_run_contracts(analysis_run_id, analysis_policy_id, "
                "prompt_template_id, prompt_version, prompt_template_sha256, "
                "output_schema_version, downstream_uses_json, review_required, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    analysis_run_id,
                    analysis_policy.analysis_policy_id,
                    analysis_policy.prompt_template_id,
                    analysis_policy.prompt_version,
                    analysis_policy.prompt_template_sha256,
                    analysis_policy.output_schema_version,
                    json.dumps(
                        list(analysis_policy.downstream_uses), ensure_ascii=False
                    ),
                    int(analysis_policy.review_required),
                    created_at,
                ),
            )
            if base is not None:
                connection.execute(
                    "INSERT INTO frame_analyses("
                    "frame_analysis_id, analysis_run_id, site_id, scene_version_id, image_name, "
                    "source_frame_index, source_time_sec, camera_id, image_content_sha256, "
                    "vlm_input_content_sha256, thumbnail_content_sha256, summary, keywords_json, "
                    "extra_json, position_x, position_y, position_z, rotation_x, rotation_y, "
                    "rotation_z, rotation_w, projection_json, created_at) "
                    "SELECT 'frame_' || lower(hex(randomblob(16))), ?, site_id, scene_version_id, "
                    "image_name, source_frame_index, source_time_sec, camera_id, image_content_sha256, "
                    "vlm_input_content_sha256, thumbnail_content_sha256, summary, keywords_json, "
                    "extra_json, position_x, position_y, position_z, rotation_x, rotation_y, "
                    "rotation_z, rotation_w, projection_json, created_at "
                    "FROM frame_analyses WHERE analysis_run_id = ?",
                    (analysis_run_id, base["analysis_run_id"]),
                )
                connection.execute(
                    "INSERT INTO frame_analysis_review_events("
                    "review_event_id, frame_analysis_id, decision, effective_summary, "
                    "effective_keywords_json, effective_extra_json, reason, actor_id, "
                    "inherited_from_review_event_id, created_at) "
                    "SELECT 'review_' || lower(hex(randomblob(16))), inherited.frame_analysis_id, "
                    "reviews.decision, reviews.effective_summary, reviews.effective_keywords_json, "
                    "reviews.effective_extra_json, reviews.reason, 'system-continuation', "
                    "reviews.review_event_id, ? "
                    "FROM frame_analyses AS inherited "
                    "JOIN frame_analyses AS source ON source.analysis_run_id = ? "
                    "AND source.image_name = inherited.image_name "
                    "JOIN frame_analysis_review_events AS reviews "
                    "ON reviews.frame_analysis_id = source.frame_analysis_id "
                    "WHERE inherited.analysis_run_id = ? AND reviews.rowid = ("
                    "SELECT latest.rowid FROM frame_analysis_review_events AS latest "
                    "WHERE latest.frame_analysis_id = source.frame_analysis_id "
                    "ORDER BY latest.created_at DESC, latest.rowid DESC LIMIT 1)",
                    (created_at, base["analysis_run_id"], analysis_run_id),
                )
                connection.execute(
                    "UPDATE analysis_runs SET output_count = ? WHERE analysis_run_id = ?",
                    (int(base["frame_count"]), analysis_run_id),
                )
                for inherited in connection.execute(
                    "SELECT frames.frame_analysis_id, frames.extra_json, reviews.review_event_id, "
                    "reviews.effective_extra_json, reviews.created_at AS review_created_at "
                    "FROM frame_analyses AS frames "
                    "LEFT JOIN frame_analysis_review_events AS reviews ON reviews.rowid = ("
                    "SELECT latest.rowid FROM frame_analysis_review_events AS latest "
                    "WHERE latest.frame_analysis_id = frames.frame_analysis_id "
                    "ORDER BY latest.created_at DESC, latest.rowid DESC LIMIT 1) "
                    "WHERE frames.analysis_run_id = ?",
                    (analysis_run_id,),
                ):
                    self._insert_observation_set(
                        connection,
                        frame_analysis_id=str(inherited["frame_analysis_id"]),
                        source_type="model",
                        source_ref_id=str(inherited["frame_analysis_id"]),
                        review_event_id=None,
                        analysis_policy_id=analysis_policy.analysis_policy_id,
                        extra=json.loads(inherited["extra_json"]),
                        created_at=created_at,
                    )
                    if inherited["review_event_id"] is not None:
                        self._insert_observation_set(
                            connection,
                            frame_analysis_id=str(inherited["frame_analysis_id"]),
                            source_type="review",
                            source_ref_id=str(inherited["review_event_id"]),
                            review_event_id=str(inherited["review_event_id"]),
                            analysis_policy_id=analysis_policy.analysis_policy_id,
                            extra=json.loads(inherited["effective_extra_json"]),
                            created_at=str(inherited["review_created_at"]),
                        )
        return self.run(analysis_run_id)

    def create_all_registered_run(
        self, scene_version_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Queue every registered COLMAP/LFS image without a client-side selection.

        This creates a new immutable snapshot. By default it inherits the
        current active snapshot and the worker analyzes only image names that
        are not already present. ``fresh_start`` explicitly disables that
        continuation behavior. Queuing never changes the active run.
        """

        if "image_names" in payload or "selected_image_names" in payload:
            raise ValueError(
                "frame_analysis_all_registered_images_selection_not_allowed"
            )
        request = dict(payload)
        fresh_start = request.pop("fresh_start", False)
        if not isinstance(fresh_start, bool):
            raise ValueError("frame_analysis_fresh_start_invalid")
        base_analysis_run_id = request.pop("base_analysis_run_id", None)
        if base_analysis_run_id is not None:
            base_analysis_run_id = _identifier(
                base_analysis_run_id, "base_analysis_run_id", prefix="analysis"
            )
        if fresh_start and base_analysis_run_id is not None:
            raise ValueError("frame_analysis_fresh_start_base_run_conflict")
        request["scene_version_id"] = scene_version_id
        return self.create_run(
            request,
            selection_policy_version=ALL_REGISTERED_IMAGES_POLICY_VERSION,
            base_analysis_run_id=base_analysis_run_id,
            inherit_active_frames=not fresh_start and base_analysis_run_id is None,
        )

    def scene_versions(self) -> list[dict[str, Any]]:
        """Return immutable SceneVersion inputs available to this analysis feature."""
        with self.field_memory._connection() as connection:
            rows = connection.execute(
                "SELECT versions.scene_version_id, versions.site_id, sites.name AS site_name, "
                "versions.scene_package_id, versions.label, versions.captured_at, versions.created_at "
                "FROM scene_versions AS versions JOIN sites USING(site_id) "
                "ORDER BY versions.created_at DESC"
            )
            result: list[dict[str, Any]] = []
            for row in rows:
                item = _row(row)
                ready, reason = self._person_mask_analysis_readiness(
                    item["scene_package_id"]
                )
                item["analysis_ready"] = ready
                item["analysis_readiness_reason"] = reason
                result.append(item)
            return result

    def _person_mask_analysis_readiness(
        self, scene_package_id: str
    ) -> tuple[bool, str | None]:
        """Check whether a Package has the semantic person-mask dataset required by bulk AI."""

        try:
            package_root = self.scene_pose_contract(scene_package_id).package_root
            dataset_path = package_root / "dataset" / "manifest.json"
            if not dataset_path.is_file():
                return False, "person_mask_dataset_missing"
            dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
            if (
                not isinstance(dataset, dict)
                or dataset.get("schema_version") != PERSON_MASK_DATASET_SCHEMA_VERSION
                or dataset.get("status") != "verified"
            ):
                return False, "person_mask_dataset_v1_1_required"
            artifacts = dataset.get("artifacts")
            if not isinstance(artifacts, dict):
                return False, "person_mask_artifacts_missing"
            person_masks = artifacts.get("person_masks")
            colmap_masks = artifacts.get("colmap_masks")
            generation = artifacts.get("mask_generation")
            if (
                not isinstance(person_masks, dict)
                or person_masks.get("convention") != "white_is_person"
                or not isinstance(colmap_masks, dict)
                or colmap_masks.get("convention") != "white_is_retained"
                or not isinstance(generation, dict)
                or generation.get("status") != "verified"
            ):
                return False, "person_mask_contract_invalid"
            report_uri = generation.get("report_uri")
            if not isinstance(report_uri, str) or not report_uri:
                return False, "person_mask_report_missing"
            report_path = (dataset_path.parent / report_uri).resolve()
            report_path.relative_to(package_root.resolve())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if not isinstance(report, dict) or report.get("status") != "verified":
                return False, "person_mask_report_unverified"
            return True, None
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            return False, "person_mask_contract_invalid"

    def scene_version(self, scene_version_id: str) -> dict[str, Any]:
        """Return one SceneVersion for scoped, read-only candidate previews."""

        _identifier(scene_version_id, "scene_version_id", prefix="scene")
        with self.field_memory._connection() as connection:
            row = connection.execute(
                "SELECT versions.scene_version_id, versions.site_id, sites.name AS site_name, "
                "versions.scene_package_id, versions.label, versions.captured_at, versions.created_at "
                "FROM scene_versions AS versions JOIN sites USING(site_id) "
                "WHERE versions.scene_version_id = ?",
                (scene_version_id,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(
                    f"field_memory_scene_version_not_found:{scene_version_id}"
                )
            return _row(row)

    def runs(self, scene_version_id: str | None = None) -> list[dict[str, Any]]:
        params: tuple[Any, ...] = ()
        clause = ""
        if scene_version_id is not None:
            _identifier(scene_version_id, "scene_version_id", prefix="scene")
            clause = " WHERE runs.scene_version_id = ?"
            params = (scene_version_id,)
        with self.field_memory._connection() as connection:
            rows = connection.execute(
                "SELECT runs.*, activations.analysis_run_id IS NOT NULL AS is_active, "
                "(SELECT COUNT(*) FROM frame_analyses AS persisted "
                "WHERE persisted.analysis_run_id = runs.analysis_run_id) AS persisted_output_count "
                "FROM analysis_runs AS runs LEFT JOIN scene_analysis_activations AS activations "
                "ON activations.analysis_run_id = runs.analysis_run_id"
                + clause
                + " ORDER BY runs.created_at DESC",
                params,
            )
            return [self._run(row) for row in rows]

    def run(self, analysis_run_id: str) -> dict[str, Any]:
        _identifier(analysis_run_id, "analysis_run_id", prefix="analysis")
        with self.field_memory._connection() as connection:
            row = connection.execute(
                "SELECT runs.*, activations.analysis_run_id IS NOT NULL AS is_active, "
                "(SELECT COUNT(*) FROM frame_analyses AS persisted "
                "WHERE persisted.analysis_run_id = runs.analysis_run_id) AS persisted_output_count "
                "FROM analysis_runs AS runs LEFT JOIN scene_analysis_activations AS activations "
                "ON activations.analysis_run_id = runs.analysis_run_id WHERE runs.analysis_run_id = ?",
                (analysis_run_id,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(
                    f"frame_analysis_run_not_found:{analysis_run_id}"
                )
            result = self._run(row)
            result["frames"] = [
                self._frame(frame, include_extra=True)
                for frame in connection.execute(
                    "SELECT frames.*, scene_versions.scene_package_id, runs.parameters_json, "
                    "reviews.review_event_id, reviews.decision AS review_decision, "
                    "reviews.effective_summary AS review_effective_summary, "
                    "reviews.effective_keywords_json AS review_effective_keywords_json, "
                    "reviews.effective_extra_json AS review_effective_extra_json, "
                    "reviews.reason AS review_reason, reviews.actor_id AS review_actor_id, "
                    "reviews.inherited_from_review_event_id, reviews.created_at AS review_created_at "
                    "FROM frame_analyses AS frames JOIN scene_versions USING(scene_version_id) "
                    "JOIN analysis_runs AS runs USING(analysis_run_id) "
                    "LEFT JOIN frame_analysis_review_events AS reviews ON reviews.rowid = ("
                    "SELECT latest.rowid FROM frame_analysis_review_events AS latest "
                    "WHERE latest.frame_analysis_id = frames.frame_analysis_id "
                    "ORDER BY latest.created_at DESC, latest.rowid DESC LIMIT 1) "
                    "WHERE frames.analysis_run_id = ? "
                    "ORDER BY source_time_sec, image_name",
                    (analysis_run_id,),
                )
            ]
            return result

    def recent_frames(
        self, analysis_run_id: str, limit: int | str = 12
    ) -> dict[str, Any]:
        """Return a bounded latest-first view of frames persisted by a running analysis."""

        _identifier(analysis_run_id, "analysis_run_id", prefix="analysis")
        if isinstance(limit, bool):
            raise ValueError("frame_analysis_recent_frames_limit_invalid")
        try:
            parsed_limit = int(limit)
        except (TypeError, ValueError) as error:
            raise ValueError("frame_analysis_recent_frames_limit_invalid") from error
        if parsed_limit < 1 or parsed_limit > 24:
            raise ValueError("frame_analysis_recent_frames_limit_invalid")
        with self.field_memory._connection() as connection:
            run = connection.execute(
                "SELECT analysis_run_id FROM analysis_runs WHERE analysis_run_id = ?",
                (analysis_run_id,),
            ).fetchone()
            if run is None:
                raise FileNotFoundError(
                    f"frame_analysis_run_not_found:{analysis_run_id}"
                )
            output_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM frame_analyses WHERE analysis_run_id = ?",
                    (analysis_run_id,),
                ).fetchone()[0]
            )
            frames = [
                self._frame(frame, include_extra=True)
                for frame in connection.execute(
                    "SELECT frames.*, scene_versions.scene_package_id, runs.parameters_json, "
                    "reviews.review_event_id, reviews.decision AS review_decision, "
                    "reviews.effective_summary AS review_effective_summary, "
                    "reviews.effective_keywords_json AS review_effective_keywords_json, "
                    "reviews.effective_extra_json AS review_effective_extra_json, "
                    "reviews.reason AS review_reason, reviews.actor_id AS review_actor_id, "
                    "reviews.inherited_from_review_event_id, reviews.created_at AS review_created_at "
                    "FROM frame_analyses AS frames JOIN scene_versions USING(scene_version_id) "
                    "JOIN analysis_runs AS runs USING(analysis_run_id) "
                    "LEFT JOIN frame_analysis_review_events AS reviews ON reviews.rowid = ("
                    "SELECT latest.rowid FROM frame_analysis_review_events AS latest "
                    "WHERE latest.frame_analysis_id = frames.frame_analysis_id "
                    "ORDER BY latest.created_at DESC, latest.rowid DESC LIMIT 1) "
                    "WHERE frames.analysis_run_id = ? "
                    "ORDER BY frames.created_at DESC, frames.image_name DESC LIMIT ?",
                    (analysis_run_id, parsed_limit),
                )
            ]
            return {
                "analysis_run_id": analysis_run_id,
                "output_count": output_count,
                "frames": frames,
            }

    def frame(self, frame_analysis_id: str) -> dict[str, Any]:
        _identifier(frame_analysis_id, "frame_analysis_id", prefix="frame")
        with self.field_memory._connection() as connection:
            row = connection.execute(
                "SELECT frames.*, scene_versions.scene_package_id, "
                "activations.analysis_run_id = frames.analysis_run_id AS is_active, runs.parameters_json, "
                "reviews.review_event_id, reviews.decision AS review_decision, "
                "reviews.effective_summary AS review_effective_summary, "
                "reviews.effective_keywords_json AS review_effective_keywords_json, "
                "reviews.effective_extra_json AS review_effective_extra_json, "
                "reviews.reason AS review_reason, reviews.actor_id AS review_actor_id, "
                "reviews.inherited_from_review_event_id, reviews.created_at AS review_created_at "
                "FROM frame_analyses AS frames JOIN scene_versions USING(scene_version_id) "
                "JOIN analysis_runs AS runs USING(analysis_run_id) "
                "LEFT JOIN scene_analysis_activations AS activations USING(scene_version_id) "
                "LEFT JOIN frame_analysis_review_events AS reviews ON reviews.rowid = ("
                "SELECT latest.rowid FROM frame_analysis_review_events AS latest "
                "WHERE latest.frame_analysis_id = frames.frame_analysis_id "
                "ORDER BY latest.created_at DESC, latest.rowid DESC LIMIT 1) "
                "WHERE frames.frame_analysis_id = ?",
                (frame_analysis_id,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(
                    f"frame_analysis_frame_not_found:{frame_analysis_id}"
                )
            result = self._frame(row, include_extra=True)
            source_type = "review" if result.get("review") else "model"
            source_ref_id = (
                result["review"]["review_event_id"]
                if result.get("review")
                else frame_analysis_id
            )
            observation_set = connection.execute(
                "SELECT * FROM frame_observation_sets WHERE frame_analysis_id = ? "
                "AND source_type = ? AND source_ref_id = ?",
                (frame_analysis_id, source_type, source_ref_id),
            ).fetchone()
            if observation_set is None:
                result["observation_set"] = None
                result["observation_items"] = []
            else:
                result["observation_set"] = _row(observation_set)
                result["observation_items"] = [
                    {
                        **_row(item),
                        "attributes": json.loads(item["attributes_json"]),
                    }
                    for item in connection.execute(
                        "SELECT * FROM frame_observation_items WHERE observation_set_id = ? "
                        "ORDER BY ordinal",
                        (observation_set["observation_set_id"],),
                    )
                ]
                for item in result["observation_items"]:
                    item.pop("attributes_json", None)
            return result

    def review_frame(
        self,
        frame_analysis_id: str,
        payload: dict[str, Any],
        *,
        actor_id: str,
    ) -> dict[str, Any]:
        """Append a human decision without mutating model-generated evidence."""

        current = self.frame(frame_analysis_id)
        decision = _text(
            payload.get("decision"), "analysis_review_decision", maximum=32
        )
        if decision not in REVIEW_DECISIONS:
            raise ValueError("frame_analysis_review_decision_invalid")
        reason = _optional_analysis_text(
            payload.get("reason"), "analysis_review_reason", maximum=2000
        )
        if decision == "rejected" and not reason:
            raise ValueError("frame_analysis_review_rejection_reason_required")
        if decision == "corrected" and not reason:
            raise ValueError("frame_analysis_review_correction_reason_required")
        effective_summary = current["summary"]
        effective_keywords = current["keywords"]
        effective_extra = current.get("extra", {})
        analysis_policy_id = current.get(
            "analysis_policy_id", LEGACY_ANALYSIS_POLICY_ID
        )
        if decision == "corrected":
            effective_summary = _analysis_summary(payload.get("summary"))
            if "keywords" in payload:
                effective_keywords = _json_list_of_strings(
                    payload.get("keywords"), "keywords"
                )
            if "extra" in payload:
                if analysis_policy_id == LEGACY_ANALYSIS_POLICY_ID:
                    raise ValueError(
                        "frame_analysis_legacy_review_structured_correction_not_allowed"
                    )
                effective_extra = _structured_observation_extra(
                    payload.get("extra"), analysis_policy_id
                )
        review_event_id = _identifier(
            payload.get("review_event_id"), "review_event_id", prefix="review"
        )
        created_at = _now()
        with self.field_memory._connection(write=True) as connection:
            connection.execute(
                "INSERT INTO frame_analysis_review_events("
                "review_event_id, frame_analysis_id, decision, effective_summary, "
                "effective_keywords_json, effective_extra_json, reason, actor_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    review_event_id,
                    frame_analysis_id,
                    decision,
                    effective_summary,
                    json.dumps(effective_keywords, ensure_ascii=False, allow_nan=False),
                    json.dumps(
                        effective_extra,
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                    ),
                    reason,
                    _text(actor_id, "analysis_review_actor_id", maximum=128),
                    created_at,
                ),
            )
            if analysis_policy_id != LEGACY_ANALYSIS_POLICY_ID:
                self._insert_observation_set(
                    connection,
                    frame_analysis_id=frame_analysis_id,
                    source_type="review",
                    source_ref_id=review_event_id,
                    review_event_id=review_event_id,
                    analysis_policy_id=analysis_policy_id,
                    extra=effective_extra,
                    created_at=created_at,
                )
        return self.frame(frame_analysis_id)

    def quality_summary(self, analysis_run_id: str) -> dict[str, Any]:
        """Summarize observation quality and latest human review state for one run."""

        _identifier(analysis_run_id, "analysis_run_id", prefix="analysis")
        counts = {
            "total_records": 0,
            "structured_records": 0,
            "usable_records": 0,
            "limited_records": 0,
            "attention_records": 0,
            "critical_records": 0,
            "unreviewed_records": 0,
            "accepted_records": 0,
            "corrected_records": 0,
            "rejected_records": 0,
            "ready_for_downstream_records": 0,
        }
        with self.field_memory._connection() as connection:
            run_row = connection.execute(
                "SELECT parameters_json FROM analysis_runs WHERE analysis_run_id = ?",
                (analysis_run_id,),
            ).fetchone()
            if run_row is None:
                raise FileNotFoundError(
                    f"frame_analysis_run_not_found:{analysis_run_id}"
                )
            run_parameters = json.loads(run_row["parameters_json"])
            analysis_policy_id = run_parameters.get(
                "analysis_policy_id", LEGACY_ANALYSIS_POLICY_ID
            )
            policy = None
            if analysis_policy_id != LEGACY_ANALYSIS_POLICY_ID:
                policy = reviewed_analysis_policy(analysis_policy_id)
            rows = connection.execute(
                "SELECT frames.extra_json, reviews.decision, reviews.effective_extra_json "
                "FROM frame_analyses AS frames "
                "LEFT JOIN frame_analysis_review_events AS reviews ON reviews.rowid = ("
                "SELECT latest.rowid FROM frame_analysis_review_events AS latest "
                "WHERE latest.frame_analysis_id = frames.frame_analysis_id "
                "ORDER BY latest.created_at DESC, latest.rowid DESC LIMIT 1) "
                "WHERE frames.analysis_run_id = ?",
                (analysis_run_id,),
            )
            for row in rows:
                counts["total_records"] += 1
                decision = row["decision"] or "unreviewed"
                counts[f"{decision}_records"] += 1
                raw_extra = row["effective_extra_json"] or row["extra_json"]
                try:
                    extra = json.loads(raw_extra)
                except json.JSONDecodeError:
                    extra = {}
                structured = (
                    policy is not None
                    and isinstance(extra, dict)
                    and extra.get("schema_version") == policy.output_schema_version
                )
                if structured:
                    counts["structured_records"] += 1
                    quality = extra.get("image_quality", {})
                    usable = isinstance(quality, dict) and quality.get("usable") is True
                    counts["usable_records" if usable else "limited_records"] += 1
                    severities = {
                        item["severity"]
                        for item in normalized_observation_items(
                            analysis_policy_id, extra
                        )
                    }
                    if "critical" in severities:
                        counts["critical_records"] += 1
                    elif "attention" in severities:
                        counts["attention_records"] += 1
                    if (
                        usable
                        and decision in {"accepted", "corrected"}
                        and "grounded-ai" in policy.downstream_uses
                    ):
                        counts["ready_for_downstream_records"] += 1
                else:
                    counts["limited_records"] += 1
        return {
            "analysis_run_id": analysis_run_id,
            "analysis_policy_id": analysis_policy_id,
            "output_schema_version": policy.output_schema_version if policy else None,
            **counts,
        }

    @staticmethod
    def _reset_snapshot(
        connection: Any, scene_version_id: str
    ) -> dict[str, Any]:
        """Return the bounded state used by reset preflight and TOCTOU checks."""

        scene = connection.execute(
            "SELECT scene_version_id, site_id, scene_package_id, label "
            "FROM scene_versions WHERE scene_version_id = ?",
            (scene_version_id,),
        ).fetchone()
        if scene is None:
            raise FileNotFoundError(
                f"field_memory_scene_version_not_found:{scene_version_id}"
            )
        run_rows = [
            dict(row)
            for row in connection.execute(
                "SELECT analysis_run_id, status, output_count, created_at, completed_at "
                "FROM analysis_runs WHERE scene_version_id = ? "
                "ORDER BY analysis_run_id",
                (scene_version_id,),
            )
        ]

        def count(statement: str, parameters: tuple[Any, ...] = ()) -> int:
            return int(connection.execute(statement, parameters).fetchone()[0])

        counts = {
            "analysis_runs": len(run_rows),
            "active_analysis_runs": sum(
                1 for row in run_rows if row["status"] in {"queued", "running"}
            ),
            "frame_analyses": count(
                "SELECT COUNT(*) FROM frame_analyses WHERE scene_version_id = ?",
                (scene_version_id,),
            ),
            "legacy_frame_analyses": count(
                "SELECT COUNT(*) FROM frame_analyses AS frames "
                "LEFT JOIN analysis_run_contracts AS contracts "
                "ON contracts.analysis_run_id = frames.analysis_run_id "
                "WHERE frames.scene_version_id = ? AND contracts.analysis_run_id IS NULL",
                (scene_version_id,),
            ),
            "review_events": count(
                "SELECT COUNT(*) FROM frame_analysis_review_events AS reviews "
                "JOIN frame_analyses AS frames USING(frame_analysis_id) "
                "WHERE frames.scene_version_id = ?",
                (scene_version_id,),
            ),
            "external_review_inheritance_refs": count(
                "SELECT COUNT(*) FROM frame_analysis_review_events AS reviews "
                "JOIN frame_analyses AS frames USING(frame_analysis_id) "
                "WHERE frames.scene_version_id <> ? "
                "AND reviews.inherited_from_review_event_id IN ("
                "SELECT target_reviews.review_event_id "
                "FROM frame_analysis_review_events AS target_reviews "
                "JOIN frame_analyses AS target_frames USING(frame_analysis_id) "
                "WHERE target_frames.scene_version_id = ?)",
                (scene_version_id, scene_version_id),
            ),
            "observation_sets": count(
                "SELECT COUNT(*) FROM frame_observation_sets AS sets "
                "JOIN frame_analyses AS frames USING(frame_analysis_id) "
                "WHERE frames.scene_version_id = ?",
                (scene_version_id,),
            ),
            "observation_items": count(
                "SELECT COUNT(*) FROM frame_observation_items AS items "
                "JOIN frame_observation_sets AS sets USING(observation_set_id) "
                "JOIN frame_analyses AS frames USING(frame_analysis_id) "
                "WHERE frames.scene_version_id = ?",
                (scene_version_id,),
            ),
            "activation_events": count(
                "SELECT COUNT(*) FROM scene_analysis_activation_events "
                "WHERE scene_version_id = ?",
                (scene_version_id,),
            ),
            "active_evidence_inquiries": count(
                "SELECT COUNT(*) FROM evidence_inquiries "
                "WHERE site_id = ? AND status IN ('queued', 'running')",
                (scene["site_id"],),
            ),
            "evidence_citations": count(
                "SELECT COUNT(*) FROM evidence_citations AS citations "
                "WHERE citations.record_type = 'frame_analysis' AND citations.record_id IN ("
                "SELECT frame_analysis_id FROM frame_analyses WHERE scene_version_id = ?)",
                (scene_version_id,),
            ),
        }
        token_payload = {
            "scene_version_id": scene_version_id,
            "runs": run_rows,
            "counts": counts,
            "active_run_id": (
                connection.execute(
                    "SELECT analysis_run_id FROM scene_analysis_activations "
                    "WHERE scene_version_id = ?",
                    (scene_version_id,),
                ).fetchone() or {"analysis_run_id": None}
            )["analysis_run_id"],
        }
        state_token = hashlib.sha256(
            json.dumps(
                token_payload, ensure_ascii=False, allow_nan=False, sort_keys=True
            ).encode("utf-8")
        ).hexdigest()
        return {
            "scene_version_id": scene_version_id,
            "scene_package_id": str(scene["scene_package_id"]),
            "site_id": str(scene["site_id"]),
            "label": str(scene["label"]),
            "counts": counts,
            "state_token": state_token,
        }

    def reset_preflight(self, scene_version_id: str) -> dict[str, Any]:
        """Describe a SceneVersion-scoped analysis reset without changing data."""

        _identifier(scene_version_id, "scene_version_id", prefix="scene")
        with self.field_memory._connection() as connection:
            snapshot = self._reset_snapshot(connection, scene_version_id)
        counts = snapshot["counts"]
        blockers: list[str] = []
        if counts["analysis_runs"] == 0:
            blockers.append("frame_analysis_reset_nothing_to_delete")
        if counts["active_analysis_runs"]:
            blockers.append("frame_analysis_reset_active_run_exists")
        if counts["active_evidence_inquiries"]:
            blockers.append("frame_analysis_reset_active_evidence_inquiry_exists")
        if counts["evidence_citations"]:
            blockers.append("frame_analysis_reset_evidence_citations_exist")
        if counts["external_review_inheritance_refs"]:
            blockers.append("frame_analysis_reset_external_review_reference_exists")
        return {
            **snapshot,
            "status": "ready" if not blockers else "blocked",
            "can_reset": not blockers,
            "blockers": blockers,
            "confirmation_phrase": f"RESET {scene_version_id}",
            "backup_required": True,
            "attachments_retained": True,
            "preserved": [
                "scene_package",
                "scene_version",
                "site_and_asset_records",
                "evidence_inquiries_and_citations",
                "content_addressed_attachments",
                "audit_events",
            ],
        }

    def reset_scene_analysis(
        self,
        scene_version_id: str,
        payload: dict[str, Any],
        *,
        actor_id: str,
        actor_role: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Delete one SceneVersion's analysis graph after a verified backup.

        Scene Packages and the rest of the site registry are deliberately out
        of scope. Referenced evidence and in-flight work fail closed.
        """

        if actor_role != "admin":
            raise PermissionError("spatial_admin_role_required")
        reason = _text(
            payload.get("reason"), "frame_analysis_reset_reason", maximum=1000
        )
        preflight = self.reset_preflight(scene_version_id)
        if not preflight["can_reset"]:
            raise ValueError(
                "frame_analysis_reset_blocked:" + ",".join(preflight["blockers"])
            )
        if payload.get("confirmation") != preflight["confirmation_phrase"]:
            raise ValueError("frame_analysis_reset_confirmation_mismatch")
        if payload.get("state_token") != preflight["state_token"]:
            raise ValueError("frame_analysis_reset_state_token_mismatch")

        backup_path = self.field_memory.create_database_backup(
            f"analysis-reset-{preflight['state_token'][:12]}"
        )
        try:
            backup_uri = backup_path.resolve().relative_to(
                self.field_memory.repo_root
            ).as_posix()
        except ValueError:
            backup_uri = backup_path.as_posix()

        counts = dict(preflight["counts"])
        created_at = _now()
        audit_event_id = f"audit_{uuid.uuid4().hex}"
        with self.field_memory._connection(write=True) as connection:
            current = self._reset_snapshot(connection, scene_version_id)
            if current["state_token"] != preflight["state_token"]:
                raise ValueError("frame_analysis_reset_state_changed_retry_preflight")
            connection.execute(
                "DELETE FROM scene_analysis_activations WHERE scene_version_id = ?",
                (scene_version_id,),
            )
            connection.execute(
                "DELETE FROM scene_analysis_activation_events WHERE scene_version_id = ?",
                (scene_version_id,),
            )
            connection.execute(
                "DELETE FROM frame_observation_items WHERE observation_set_id IN ("
                "SELECT sets.observation_set_id FROM frame_observation_sets AS sets "
                "JOIN frame_analyses AS frames USING(frame_analysis_id) "
                "WHERE frames.scene_version_id = ?)",
                (scene_version_id,),
            )
            connection.execute(
                "DELETE FROM frame_observation_sets WHERE frame_analysis_id IN ("
                "SELECT frame_analysis_id FROM frame_analyses WHERE scene_version_id = ?)",
                (scene_version_id,),
            )
            connection.execute(
                "UPDATE frame_analysis_review_events SET inherited_from_review_event_id = NULL "
                "WHERE frame_analysis_id IN (SELECT frame_analysis_id FROM frame_analyses "
                "WHERE scene_version_id = ?)",
                (scene_version_id,),
            )
            connection.execute(
                "DELETE FROM frame_analysis_review_events WHERE frame_analysis_id IN ("
                "SELECT frame_analysis_id FROM frame_analyses WHERE scene_version_id = ?)",
                (scene_version_id,),
            )
            connection.execute(
                "DELETE FROM frame_analyses WHERE scene_version_id = ?",
                (scene_version_id,),
            )
            connection.execute(
                "DELETE FROM analysis_run_contracts WHERE analysis_run_id IN ("
                "SELECT analysis_run_id FROM analysis_runs WHERE scene_version_id = ?)",
                (scene_version_id,),
            )
            connection.execute(
                "DELETE FROM analysis_runs WHERE scene_version_id = ?",
                (scene_version_id,),
            )
            connection.execute(
                "INSERT INTO audit_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    audit_event_id,
                    _text(actor_id, "analysis_reset_actor_id", maximum=128),
                    actor_role,
                    current["site_id"],
                    "frame_analysis_scene_reset",
                    scene_version_id,
                    request_id,
                    "success",
                    json.dumps(
                        {
                            "reason": reason,
                            "backup_uri": backup_uri,
                            "deleted": counts,
                            "attachments_retained": True,
                            "scene_package_preserved": True,
                        },
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                    ),
                    created_at,
                ),
            )
        return {
            "status": "reset",
            "scene_version_id": scene_version_id,
            "scene_package_id": preflight["scene_package_id"],
            "site_id": preflight["site_id"],
            "deleted": counts,
            "backup_uri": backup_uri,
            "attachments_retained": True,
            "audit_event_id": audit_event_id,
            "reset_at": created_at,
        }

    def activate(self, scene_version_id: str, analysis_run_id: str) -> dict[str, Any]:
        _identifier(scene_version_id, "scene_version_id", prefix="scene")
        _identifier(analysis_run_id, "analysis_run_id", prefix="analysis")
        activated_at = _now()
        with self.field_memory._connection(write=True) as connection:
            run = connection.execute(
                "SELECT scene_version_id, status, ("
                "SELECT COUNT(*) FROM frame_analyses WHERE analysis_run_id = analysis_runs.analysis_run_id"
                ") AS persisted_output_count FROM analysis_runs WHERE analysis_run_id = ?",
                (analysis_run_id,),
            ).fetchone()
            if run is None:
                raise FileNotFoundError(
                    f"frame_analysis_run_not_found:{analysis_run_id}"
                )
            if run["scene_version_id"] != scene_version_id:
                raise ValueError("frame_analysis_activation_scene_version_mismatch")
            is_completed = run["status"] == "completed"
            is_partial = (
                run["status"] in {"failed", "cancelled"}
                and int(run["persisted_output_count"]) > 0
            )
            if not (is_completed or is_partial):
                raise ValueError("frame_analysis_activation_run_not_usable")
            connection.execute(
                "INSERT INTO scene_analysis_activations(scene_version_id, analysis_run_id, activated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(scene_version_id) DO UPDATE SET "
                "analysis_run_id = excluded.analysis_run_id, activated_at = excluded.activated_at",
                (scene_version_id, analysis_run_id, activated_at),
            )
            connection.execute(
                "INSERT INTO scene_analysis_activation_events(activation_id, scene_version_id, "
                "analysis_run_id, activated_at) VALUES (?, ?, ?, ?)",
                (
                    f"activation_{uuid.uuid4().hex}",
                    scene_version_id,
                    analysis_run_id,
                    activated_at,
                ),
            )
        return {
            "scene_version_id": scene_version_id,
            "analysis_run_id": analysis_run_id,
            "activated_at": activated_at,
        }

    def claim_next_run(self) -> dict[str, Any] | None:
        with self.field_memory._connection(write=True) as connection:
            row = connection.execute(
                "SELECT analysis_run_id FROM analysis_runs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            started_at = _now()
            changed = connection.execute(
                "UPDATE analysis_runs SET status = 'running', started_at = ? "
                "WHERE analysis_run_id = ? AND status = 'queued'",
                (started_at, row["analysis_run_id"]),
            ).rowcount
            if changed != 1:
                return None
            claimed = connection.execute(
                "SELECT runs.*, 0 AS is_active FROM analysis_runs AS runs WHERE analysis_run_id = ?",
                (row["analysis_run_id"],),
            ).fetchone()
            assert claimed is not None
            return self._run(claimed)

    def cancellation_requested(self, analysis_run_id: str) -> bool:
        """Return whether a worker must stop this run at its next safe boundary."""

        _identifier(analysis_run_id, "analysis_run_id", prefix="analysis")
        with self.field_memory._connection() as connection:
            row = connection.execute(
                "SELECT status, failure_reason FROM analysis_runs WHERE analysis_run_id = ?",
                (analysis_run_id,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(
                    f"frame_analysis_run_not_found:{analysis_run_id}"
                )
            return row["status"] == "cancelled"

    def cancel_run(self, analysis_run_id: str) -> dict[str, Any]:
        """Stop a queued or running run and retain every persisted frame.

        Cancellation is a first-class terminal state. A non-empty stopped run
        becomes the active immutable snapshot; a zero-output stop preserves the
        previously active run.
        """

        _identifier(analysis_run_id, "analysis_run_id", prefix="analysis")
        already_cancelled = False
        with self.field_memory._connection(write=True) as connection:
            row = connection.execute(
                "SELECT scene_version_id, status FROM analysis_runs WHERE analysis_run_id = ?",
                (analysis_run_id,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(
                    f"frame_analysis_run_not_found:{analysis_run_id}"
                )
            if row["status"] == "cancelled":
                already_cancelled = True
            if not already_cancelled and row["status"] not in {"queued", "running"}:
                raise ValueError("frame_analysis_run_not_cancellable")
            if not already_cancelled:
                count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM frame_analyses WHERE analysis_run_id = ?",
                        (analysis_run_id,),
                    ).fetchone()[0]
                )
                cancelled_at = _now()
                connection.execute(
                    "UPDATE analysis_runs SET status = 'cancelled', output_count = ?, "
                    "failure_reason = 'cancelled_by_operator', completed_at = ? "
                    "WHERE analysis_run_id = ?",
                    (count, cancelled_at, analysis_run_id),
                )
                if count > 0:
                    connection.execute(
                        "INSERT INTO scene_analysis_activations(scene_version_id, analysis_run_id, activated_at) "
                        "VALUES (?, ?, ?) ON CONFLICT(scene_version_id) DO UPDATE SET "
                        "analysis_run_id = excluded.analysis_run_id, activated_at = excluded.activated_at",
                        (row["scene_version_id"], analysis_run_id, cancelled_at),
                    )
                    connection.execute(
                        "INSERT INTO scene_analysis_activation_events(activation_id, scene_version_id, "
                        "analysis_run_id, activated_at) VALUES (?, ?, ?, ?)",
                        (
                            f"activation_{uuid.uuid4().hex}",
                            row["scene_version_id"],
                            analysis_run_id,
                            cancelled_at,
                        ),
                    )
        return self.run(analysis_run_id)

    def record_model_provenance(
        self, analysis_run_id: str, evidence: dict[str, Any]
    ) -> dict[str, Any]:
        """Fix the resolved local model evidence before inference starts."""
        _identifier(analysis_run_id, "analysis_run_id", prefix="analysis")
        normalized = _json_object(evidence, "model_evidence")
        digest = normalized.get("tree_sha256")
        file_count = normalized.get("file_count")
        total_bytes = normalized.get("total_bytes")
        if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            raise ValueError("frame_analysis_model_evidence_sha256_invalid")
        if (
            isinstance(file_count, bool)
            or not isinstance(file_count, int)
            or file_count < 1
            or isinstance(total_bytes, bool)
            or not isinstance(total_bytes, int)
            or total_bytes < 1
        ):
            raise ValueError("frame_analysis_model_evidence_size_invalid")
        with self.field_memory._connection(write=True) as connection:
            row = connection.execute(
                "SELECT parameters_json, status FROM analysis_runs WHERE analysis_run_id = ?",
                (analysis_run_id,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(
                    f"frame_analysis_run_not_found:{analysis_run_id}"
                )
            if row["status"] != "running":
                raise ValueError("frame_analysis_run_not_running")
            parameters = json.loads(row["parameters_json"])
            if "model_evidence" in parameters:
                raise ValueError("frame_analysis_model_evidence_already_recorded")
            parameters["model_evidence"] = normalized
            connection.execute(
                "UPDATE analysis_runs SET parameters_json = ? WHERE analysis_run_id = ?",
                (
                    json.dumps(
                        parameters, ensure_ascii=False, allow_nan=False, sort_keys=True
                    ),
                    analysis_run_id,
                ),
            )
        return self.run(analysis_run_id)

    def record_source_provenance(
        self, analysis_run_id: str, evidence: dict[str, Any]
    ) -> dict[str, Any]:
        """Persist verified immutable-input evidence before VLM inference."""
        _identifier(analysis_run_id, "analysis_run_id", prefix="analysis")
        normalized = _json_object(evidence, "source_evidence")
        required_hashes = (
            "mrp_manifest_sha256",
            "world_alignment_sha256",
            "camera_trajectory_sha256",
            "lfs_manifest_sha256",
            "colmap_model_sha256",
        )
        if any(
            not isinstance(normalized.get(field), str)
            or SHA256_RE.fullmatch(str(normalized[field])) is None
            for field in required_hashes
        ):
            raise ValueError("frame_analysis_source_evidence_hash_invalid")
        with self.field_memory._connection(write=True) as connection:
            row = connection.execute(
                "SELECT parameters_json, status FROM analysis_runs WHERE analysis_run_id = ?",
                (analysis_run_id,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(
                    f"frame_analysis_run_not_found:{analysis_run_id}"
                )
            if row["status"] != "running":
                raise ValueError("frame_analysis_run_not_running")
            parameters = json.loads(row["parameters_json"])
            if "source_evidence" in parameters:
                raise ValueError("frame_analysis_source_evidence_already_recorded")
            parameters["source_evidence"] = normalized
            connection.execute(
                "UPDATE analysis_runs SET parameters_json = ? WHERE analysis_run_id = ?",
                (
                    json.dumps(
                        parameters, ensure_ascii=False, allow_nan=False, sort_keys=True
                    ),
                    analysis_run_id,
                ),
            )
        return self.run(analysis_run_id)

    @staticmethod
    def _insert_observation_set(
        connection: Any,
        *,
        frame_analysis_id: str,
        source_type: str,
        source_ref_id: str,
        review_event_id: str | None,
        analysis_policy_id: str,
        extra: dict[str, Any],
        created_at: str,
    ) -> str:
        """Persist one validated observation snapshot as normalized query rows."""

        policy = reviewed_analysis_policy(analysis_policy_id)
        normalized = normalize_policy_extra(analysis_policy_id, extra)
        items = normalized_observation_items(analysis_policy_id, normalized)
        observation_set_id = f"obset_{uuid.uuid4().hex}"
        connection.execute(
            "INSERT INTO frame_observation_sets(observation_set_id, frame_analysis_id, "
            "source_type, source_ref_id, review_event_id, analysis_policy_id, "
            "output_schema_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                observation_set_id,
                frame_analysis_id,
                source_type,
                source_ref_id,
                review_event_id,
                analysis_policy_id,
                policy.output_schema_version,
                created_at,
            ),
        )
        connection.executemany(
            "INSERT INTO frame_observation_items(observation_item_id, observation_set_id, "
            "ordinal, item_kind, subject, category, observation, evidence, severity, "
            "confidence, attributes_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    f"obitem_{uuid.uuid4().hex}",
                    observation_set_id,
                    ordinal,
                    item["item_kind"],
                    item["subject"],
                    item["category"],
                    item["observation"],
                    item["evidence"],
                    item["severity"],
                    item["confidence"],
                    json.dumps(
                        item["attributes"],
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                    ),
                    created_at,
                )
                for ordinal, item in enumerate(items)
            ],
        )
        return observation_set_id

    def add_frame(
        self, analysis_run_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        _identifier(analysis_run_id, "analysis_run_id", prefix="analysis")
        frame_analysis_id = _identifier(
            payload.get("frame_analysis_id"), "frame_analysis_id", prefix="frame"
        )
        image_name = _text(
            payload.get("image_name"), "analysis_image_name", maximum=1024
        )
        source_frame_index = payload.get("source_frame_index")
        if (
            isinstance(source_frame_index, bool)
            or not isinstance(source_frame_index, int)
            or source_frame_index < 0
        ):
            raise ValueError("frame_analysis_source_frame_index_invalid")
        source_time_sec = _finite_number(
            payload.get("source_time_sec"), "frame_analysis_source_time_sec"
        )
        if source_time_sec < 0:
            raise ValueError("frame_analysis_source_time_sec_invalid")
        camera_id = _text(
            str(payload.get("camera_id", "")), "analysis_camera_id", maximum=96
        )
        hashes: list[str] = []
        for field in (
            "image_content_sha256",
            "vlm_input_content_sha256",
            "thumbnail_content_sha256",
        ):
            value = payload.get(field)
            if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
                raise ValueError(f"frame_analysis_{field}_invalid")
            if not self.field_memory.content_path(value).is_file():
                raise FileNotFoundError(f"frame_analysis_content_missing:{value}")
            hashes.append(value.lower())
        summary = _analysis_summary(payload.get("summary"))
        keywords = _json_list_of_strings(payload.get("keywords", []), "keywords")
        extra = _json_object(payload.get("extra"), "extra")
        projection = _json_object(payload.get("projection"), "projection")
        px, py, pz, qx, qy, qz, qw = _pose(payload.get("pose"))
        created_at = _now()
        with self.field_memory._connection(write=True) as connection:
            run = connection.execute(
                "SELECT runs.site_id, runs.scene_version_id, runs.status, "
                "contracts.analysis_policy_id FROM analysis_runs AS runs "
                "JOIN analysis_run_contracts AS contracts USING(analysis_run_id) "
                "WHERE runs.analysis_run_id = ?",
                (analysis_run_id,),
            ).fetchone()
            if run is None:
                raise FileNotFoundError(
                    f"frame_analysis_run_not_found:{analysis_run_id}"
                )
            if run["status"] != "running":
                raise ValueError("frame_analysis_run_not_running")
            connection.execute(
                "INSERT INTO frame_analyses("
                "frame_analysis_id, analysis_run_id, site_id, scene_version_id, image_name, "
                "source_frame_index, source_time_sec, camera_id, image_content_sha256, "
                "vlm_input_content_sha256, thumbnail_content_sha256, summary, keywords_json, "
                "extra_json, position_x, position_y, position_z, rotation_x, rotation_y, "
                "rotation_z, rotation_w, projection_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    frame_analysis_id,
                    analysis_run_id,
                    run["site_id"],
                    run["scene_version_id"],
                    image_name,
                    source_frame_index,
                    source_time_sec,
                    camera_id,
                    *hashes,
                    summary,
                    json.dumps(keywords, ensure_ascii=False, allow_nan=False),
                    json.dumps(
                        extra, ensure_ascii=False, allow_nan=False, sort_keys=True
                    ),
                    px,
                    py,
                    pz,
                    qx,
                    qy,
                    qz,
                    qw,
                    json.dumps(
                        projection, ensure_ascii=False, allow_nan=False, sort_keys=True
                    ),
                    created_at,
                ),
            )
            self._insert_observation_set(
                connection,
                frame_analysis_id=frame_analysis_id,
                source_type="model",
                source_ref_id=frame_analysis_id,
                review_event_id=None,
                analysis_policy_id=str(run["analysis_policy_id"]),
                extra=extra,
                created_at=created_at,
            )
            connection.execute(
                "UPDATE analysis_runs SET output_count = ("
                "SELECT COUNT(*) FROM frame_analyses WHERE analysis_run_id = ?"
                ") WHERE analysis_run_id = ?",
                (analysis_run_id, analysis_run_id),
            )
        return self.frame(frame_analysis_id)

    def persisted_image_names(self, analysis_run_id: str) -> set[str]:
        """Return image identities already present in an immutable run snapshot."""

        _identifier(analysis_run_id, "analysis_run_id", prefix="analysis")
        with self.field_memory._connection() as connection:
            exists = connection.execute(
                "SELECT 1 FROM analysis_runs WHERE analysis_run_id = ?",
                (analysis_run_id,),
            ).fetchone()
            if exists is None:
                raise FileNotFoundError(
                    f"frame_analysis_run_not_found:{analysis_run_id}"
                )
            return {
                str(row["image_name"])
                for row in connection.execute(
                    "SELECT image_name FROM frame_analyses WHERE analysis_run_id = ?",
                    (analysis_run_id,),
                )
            }

    def complete_run(
        self, analysis_run_id: str, input_manifest_sha256: str
    ) -> dict[str, Any]:
        if (
            not isinstance(input_manifest_sha256, str)
            or SHA256_RE.fullmatch(input_manifest_sha256) is None
        ):
            raise ValueError("frame_analysis_input_manifest_sha256_invalid")
        completed_at = _now()
        with self.field_memory._connection(write=True) as connection:
            run = connection.execute(
                "SELECT scene_version_id FROM analysis_runs WHERE analysis_run_id = ?",
                (analysis_run_id,),
            ).fetchone()
            if run is None:
                raise FileNotFoundError(
                    f"frame_analysis_run_not_found:{analysis_run_id}"
                )
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM frame_analyses WHERE analysis_run_id = ?",
                    (analysis_run_id,),
                ).fetchone()[0]
            )
            changed = connection.execute(
                "UPDATE analysis_runs SET status = 'completed', input_manifest_sha256 = ?, "
                "output_count = ?, completed_at = ?, failure_reason = NULL "
                "WHERE analysis_run_id = ? AND status = 'running'",
                (input_manifest_sha256.lower(), count, completed_at, analysis_run_id),
            ).rowcount
            if changed != 1:
                raise ValueError("frame_analysis_run_not_running")
            if count > 0:
                connection.execute(
                    "INSERT INTO scene_analysis_activations(scene_version_id, analysis_run_id, activated_at) "
                    "VALUES (?, ?, ?) ON CONFLICT(scene_version_id) DO UPDATE SET "
                    "analysis_run_id = excluded.analysis_run_id, activated_at = excluded.activated_at",
                    (run["scene_version_id"], analysis_run_id, completed_at),
                )
                connection.execute(
                    "INSERT INTO scene_analysis_activation_events(activation_id, scene_version_id, "
                    "analysis_run_id, activated_at) VALUES (?, ?, ?, ?)",
                    (
                        f"activation_{uuid.uuid4().hex}",
                        run["scene_version_id"],
                        analysis_run_id,
                        completed_at,
                    ),
                )
        return self.run(analysis_run_id)

    def fail_run(self, analysis_run_id: str, reason: str) -> dict[str, Any]:
        failure_reason = _text(reason, "analysis_failure_reason", maximum=4000)
        with self.field_memory._connection(write=True) as connection:
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM frame_analyses WHERE analysis_run_id = ?",
                    (analysis_run_id,),
                ).fetchone()[0]
            )
            changed = connection.execute(
                "UPDATE analysis_runs SET status = 'failed', output_count = ?, "
                "failure_reason = ?, completed_at = ? "
                "WHERE analysis_run_id = ? AND status = 'running'",
                (count, failure_reason, _now(), analysis_run_id),
            ).rowcount
            if changed != 1:
                raise ValueError("frame_analysis_run_not_running")
        return self.run(analysis_run_id)

    @staticmethod
    def _run(row: Any) -> dict[str, Any]:
        value = _row(row)
        persisted_count = int(value.pop("persisted_output_count", 0) or 0)
        value["output_count"] = max(
            int(value.get("output_count", 0) or 0), persisted_count
        )
        value["parameters"] = json.loads(value.pop("parameters_json"))
        value["analysis_policy_id"] = value["parameters"].get(
            "analysis_policy_id", LEGACY_ANALYSIS_POLICY_ID
        )
        value["output_schema_version"] = value["parameters"].get(
            "output_schema_version"
        )
        source_evidence = value["parameters"].get("source_evidence", {})
        target_count: int | None = None
        if isinstance(source_evidence, dict):
            policy = value["parameters"].get("selection_policy_version")
            candidates = (
                ("all_registered_count", "registered_candidate_count")
                if policy == "all-registered-images-v1"
                else (
                    "explicit_selected_count",
                    "interval_selected_count",
                    "registered_candidate_count",
                )
            )
            for field in candidates:
                candidate = source_evidence.get(field)
                if (
                    isinstance(candidate, int)
                    and not isinstance(candidate, bool)
                    and candidate >= 0
                ):
                    target_count = candidate
                    break
        value["target_count"] = target_count
        value["progress_percent"] = (
            min(100.0, round(value["output_count"] * 100.0 / target_count, 1))
            if target_count
            else None
        )
        value["is_active"] = bool(value.get("is_active", False))
        return value

    def _frame(self, row: Any, *, include_extra: bool) -> dict[str, Any]:
        value = _row(row)
        result = {
            key: value[key]
            for key in (
                "frame_analysis_id",
                "analysis_run_id",
                "site_id",
                "scene_version_id",
                "image_name",
                "source_frame_index",
                "source_time_sec",
                "camera_id",
                "image_content_sha256",
                "vlm_input_content_sha256",
                "thumbnail_content_sha256",
                "summary",
                "created_at",
            )
        }
        result["model_summary"] = value["summary"]
        result["scene_package_id"] = value.get("scene_package_id")
        raw_parameters = value.get("parameters_json")
        try:
            run_parameters = (
                json.loads(raw_parameters) if isinstance(raw_parameters, str) else {}
            )
        except json.JSONDecodeError:
            run_parameters = {}
        result["analysis_policy_id"] = run_parameters.get(
            "analysis_policy_id", LEGACY_ANALYSIS_POLICY_ID
        )
        result["output_schema_version"] = run_parameters.get("output_schema_version")
        model_keywords = json.loads(value["keywords_json"])
        model_extra = json.loads(value["extra_json"])
        review = None
        if value.get("review_event_id"):
            review = {
                "review_event_id": value["review_event_id"],
                "decision": value["review_decision"],
                "reason": value.get("review_reason") or "",
                "actor_id": value.get("review_actor_id"),
                "created_at": value.get("review_created_at"),
                "inherited_from_review_event_id": value.get(
                    "inherited_from_review_event_id"
                ),
            }
            result["summary"] = value["review_effective_summary"]
            result["keywords"] = json.loads(value["review_effective_keywords_json"])
            effective_extra = json.loads(value["review_effective_extra_json"])
        else:
            result["keywords"] = model_keywords
            effective_extra = model_extra
        result["model_keywords"] = model_keywords
        result["review"] = review
        result["review_status"] = review["decision"] if review else "unreviewed"
        pose = {
            "position": {axis: value[f"position_{axis}"] for axis in "xyz"},
            "rotation": {axis: value[f"rotation_{axis}"] for axis in "xyzw"},
        }
        pose, compatibility_adjusted, pose_contract = self._compatible_pose(value, pose)
        result["pose"] = pose
        result["pose_coordinate_contract"] = pose_contract
        result["pose_compatibility_adjusted"] = compatibility_adjusted
        result["projection"] = json.loads(value["projection_json"])
        result["image_url"] = (
            f"/api/frame-analysis/content/{value['image_content_sha256']}"
        )
        result["thumbnail_url"] = (
            f"/api/frame-analysis/content/{value['thumbnail_content_sha256']}"
        )
        result["viewer_url"] = (
            f"/?scene_package={value['scene_package_id']}&frame_analysis={value['frame_analysis_id']}"
            if value.get("scene_package_id")
            else None
        )
        if "is_active" in value:
            result["is_active"] = bool(value["is_active"])
        if include_extra:
            result["extra"] = effective_extra
            result["model_extra"] = model_extra
        return result

    def _compatible_pose(
        self,
        row: dict[str, Any],
        pose: dict[str, Any],
    ) -> tuple[dict[str, Any], bool, dict[str, Any] | None]:
        raw_parameters = row.get("parameters_json")
        if not isinstance(raw_parameters, str):
            return pose, False, None
        try:
            parameters = json.loads(raw_parameters)
        except json.JSONDecodeError as error:
            raise ValueError("frame_analysis_parameters_json_invalid") from error
        source_evidence = (
            parameters.get("source_evidence") if isinstance(parameters, dict) else None
        )
        if not isinstance(source_evidence, dict):
            return pose, False, None
        recorded_contract = source_evidence.get("pose_coordinate_contract")
        if isinstance(recorded_contract, dict):
            return pose, False, dict(recorded_contract)
        # Only producer-generated records carry this immutable source marker.
        # Manually seeded/legacy acceptance records remain untouched because
        # their original coordinate stage cannot be inferred safely.
        if not isinstance(source_evidence.get("registered_candidate_count"), int):
            return pose, False, None
        package_id = row.get("scene_package_id")
        if not isinstance(package_id, str):
            return pose, False, None
        contract = self.scene_pose_contract(package_id)
        corrected = self._upgrade_legacy_viewer_pose(pose, contract)
        return corrected, True, self._pose_contract_metadata(contract)

    def scene_pose_contract(self, scene_package_id: str) -> ScenePoseContract:
        cached = self._pose_contract_cache.get(scene_package_id)
        if cached is not None:
            return cached
        manifest_path = self._scene_manifest_path(scene_package_id)
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(
                f"frame_analysis_scene_manifest_invalid:{scene_package_id}"
            ) from error
        if (
            not isinstance(manifest, dict)
            or manifest.get("scene_package_id") != scene_package_id
        ):
            raise ValueError(
                f"frame_analysis_scene_manifest_identity_mismatch:{scene_package_id}"
            )
        scene_id = manifest.get("scene_id")
        reconstruction_id = manifest.get("reconstruction_id")
        provenance = manifest.get("provenance")
        decoder = (
            provenance.get("decoder_coordinate_contract")
            if isinstance(provenance, dict)
            else None
        )
        if not isinstance(scene_id, str) or not isinstance(reconstruction_id, str):
            raise ValueError("frame_analysis_scene_package_mrp_identity_missing")
        if not isinstance(decoder, dict) or not isinstance(decoder.get("id"), str):
            raise ValueError("frame_analysis_decoder_coordinate_contract_missing")
        quaternion = _strict_unit_quaternion_xyzw(
            decoder.get("rotation_quaternion_xyzw"), "decoder_coordinate_contract"
        )
        contract = ScenePoseContract(
            scene_package_id=scene_package_id,
            scene_id=scene_id,
            reconstruction_id=reconstruction_id,
            package_root=manifest_path.parent.resolve(),
            decoder_id=decoder["id"],
            decoder_quaternion_xyzw=quaternion,
        )
        self._pose_contract_cache[scene_package_id] = contract
        return contract

    def _scene_manifest_path(self, scene_package_id: str) -> Path:
        root = self.field_memory.repo_root
        layout = SceneRepositoryLayout.from_root(root)
        candidates = (
            layout.scene_packages / scene_package_id / "scene_manifest.json",
            root
            / "web_viewer"
            / "app"
            / "public"
            / "scene_package"
            / "runtime"
            / scene_package_id
            / "scene_manifest.json",
            root
            / "web_viewer"
            / "app"
            / "public"
            / "scene_package"
            / scene_package_id
            / "scene_manifest.json",
        )
        for path in candidates:
            if path.is_file():
                return path
        scene_root = layout.scene_packages
        if scene_root.is_dir():
            for path in scene_root.rglob("scene_manifest.json"):
                if path.parent.name == scene_package_id:
                    return path
        raise FileNotFoundError(
            f"frame_analysis_scene_manifest_not_found:{scene_package_id}"
        )

    def _upgrade_legacy_viewer_pose(
        self,
        pose: dict[str, Any],
        contract: ScenePoseContract,
    ) -> dict[str, Any]:
        packaged_alignment = (
            contract.package_root
            / "dataset"
            / "metric_reconstruction"
            / "config"
            / "world_alignment.json"
        )
        alignment_path = (
            packaged_alignment
            if packaged_alignment.is_file()
            else (
                SceneRepositoryLayout.from_root(
                    self.field_memory.repo_root
                ).metric_reconstructions
                / contract.scene_id
                / contract.reconstruction_id
                / "config"
                / "world_alignment.json"
            )
        )
        try:
            alignment = json.loads(alignment_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("frame_analysis_world_alignment_invalid") from error
        transform = (
            alignment.get("generated_to_world") if isinstance(alignment, dict) else None
        )
        if not isinstance(transform, dict):
            raise ValueError("frame_analysis_world_alignment_missing")
        scale = _positive_number(transform.get("scale"), "world_alignment_scale", 1.0)
        qx, qy, qz, qw = _strict_unit_quaternion_xyzw(
            transform.get("rotation_quaternion_xyzw"), "world_alignment"
        )
        translation = transform.get("translation_m")
        if not isinstance(translation, list) or len(translation) != 3:
            raise ValueError("frame_analysis_world_alignment_translation_invalid")
        translation_m = [
            _finite_number(value, "frame_analysis_world_alignment_translation")
            for value in translation
        ]
        alignment_rotation = RegisteredFrameAnalysisEngine._quaternion_matrix(
            qx, qy, qz, qw
        )
        inverse_alignment = RegisteredFrameAnalysisEngine._transpose(alignment_rotation)
        dqx, dqy, dqz, dqw = contract.decoder_quaternion_xyzw
        decoder_rotation = RegisteredFrameAnalysisEngine._quaternion_matrix(
            dqx, dqy, dqz, dqw
        )
        position = [
            _finite_number(pose["position"][axis], "frame_analysis_position")
            for axis in "xyz"
        ]
        generated = RegisteredFrameAnalysisEngine._matvec(
            inverse_alignment,
            [(position[index] - translation_m[index]) / scale for index in range(3)],
        )
        corrected_generated = RegisteredFrameAnalysisEngine._matvec(
            decoder_rotation, generated
        )
        corrected_aligned = RegisteredFrameAnalysisEngine._matvec(
            alignment_rotation, corrected_generated
        )
        corrected_position = [
            scale * corrected_aligned[index] + translation_m[index]
            for index in range(3)
        ]
        rotation = pose["rotation"]
        rqx, rqy, rqz, rqw = _strict_unit_quaternion_xyzw(
            [rotation[axis] for axis in "xyzw"], "stored_pose"
        )
        old_viewer_from_camera = RegisteredFrameAnalysisEngine._quaternion_matrix(
            rqx, rqy, rqz, rqw
        )
        delta = RegisteredFrameAnalysisEngine._matmul(
            RegisteredFrameAnalysisEngine._matmul(alignment_rotation, decoder_rotation),
            inverse_alignment,
        )
        corrected_rotation = RegisteredFrameAnalysisEngine._matrix_quaternion(
            RegisteredFrameAnalysisEngine._matmul(delta, old_viewer_from_camera)
        )
        return {
            "position": dict(zip("xyz", corrected_position, strict=True)),
            "rotation": dict(zip("xyzw", corrected_rotation, strict=True)),
        }

    @staticmethod
    def _pose_contract_metadata(contract: ScenePoseContract) -> dict[str, Any]:
        return {
            "version": POSE_COORDINATE_CONTRACT_VERSION,
            "stage": POSE_COORDINATE_STAGE,
            "decoder_coordinate_contract_id": contract.decoder_id,
        }


@dataclass(frozen=True)
class RegisteredFrame:
    image_name: str
    image_path: Path
    mask_path: Path | None
    mask_convention: str
    source_frame_index: int
    source_time_sec: float
    camera_id: str
    pose: dict[str, Any]
    projection: dict[str, Any]


class FrameAnalyzer(Protocol):
    def analyze(
        self,
        image: Image.Image,
        analysis_policy_id: str,
        prompt_version: str,
    ) -> dict[str, Any]: ...


class TransformersLocalVlmAnalyzer:
    """Lazy local-files-only Transformers adapter; it never downloads a model."""

    OUTPUT_CONTRACT_ATTEMPTS = 3

    def __init__(self, model_path: str, *, max_new_tokens: int = 384) -> None:
        path = Path(model_path).expanduser().resolve()
        if not path.is_dir():
            raise FileNotFoundError(f"frame_analysis_local_model_not_found:{path}")
        self.model_path = path
        self.max_new_tokens = max_new_tokens
        self._model: Any | None = None
        self._processor: Any | None = None

    def release(self) -> None:
        """Release a loaded model before the worker switches model sizes."""

        self._model = None
        self._processor = None
        try:
            import gc
            import torch

            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            # Test/API-only environments do not necessarily install torch.
            return

    def load(self) -> None:
        """Load the local model now so startup cost can be measured separately."""

        self._ensure_loaded()

    def analyze(
        self,
        image: Image.Image,
        analysis_policy_id: str,
        prompt_version: str,
    ) -> dict[str, Any]:
        self._ensure_loaded()
        assert self._model is not None and self._processor is not None
        policy = reviewed_analysis_policy(analysis_policy_id)
        if prompt_version != policy.prompt_version:
            raise ValueError("frame_analysis_prompt_version_policy_mismatch")
        prompt = f"{policy.prompt}\nプロンプト契約: {prompt_version}。"
        rgb_image = image.convert("RGB")
        validation_error: ValueError | None = None
        for attempt in range(self.OUTPUT_CONTRACT_ATTEMPTS):
            contract_reminder = (
                ""
                if attempt == 0
                else (
                    "\n前回の応答は出力契約に違反していました。"
                    "summary は文字列、keywords は文字列配列、extra は指定した構造にし、"
                    "説明やMarkdownを付けずJSONオブジェクトだけを返してください。"
                )
            )
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "image": rgb_image},
                        {"type": "text", "text": prompt + contract_reminder},
                    ],
                }
            ]
            text = self._generate(messages)
            try:
                return self._parse_analysis_output(text, analysis_policy_id)
            except ValueError as error:
                validation_error = error

        assert validation_error is not None
        raise validation_error

    def _parse_analysis_output(
        self,
        text: str,
        analysis_policy_id: str = DEFAULT_ANALYSIS_POLICY_ID,
    ) -> dict[str, Any]:
        try:
            parsed = json.loads(self._json_text(text))
        except json.JSONDecodeError as error:
            raise ValueError("frame_analysis_vlm_output_not_json") from error
        if not isinstance(parsed, dict):
            raise ValueError("frame_analysis_vlm_output_not_object")
        return {
            "summary": _analysis_summary(parsed.get("summary")),
            "keywords": _json_list_of_strings(parsed.get("keywords", []), "keywords"),
            "extra": _structured_observation_extra(
                parsed.get("extra"), analysis_policy_id
            ),
        }

    def answer_evidence(
        self, question: str, snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        """Answer using only the immutable search-result snapshot."""

        self._ensure_loaded()
        records = snapshot.get("records", [])
        if not records:
            return {
                "answer": "根拠が不足しているため回答できません。",
                "is_unknown": True,
                "citations": [],
            }
        prompt = (
            "あなたは現場記憶の根拠限定AIです。以下のEVIDENCE以外の知識を使わず日本語で回答してください。"
            "根拠不足ならis_unknown=trueとし、断定しないでください。JSON以外は出力禁止です。"
            '形式は {"answer":"回答","is_unknown":false,"citations":['
            '{"record_type":"...","record_id":"...","excerpt":"根拠要約"}]}。'
            "citationsにはEVIDENCE内に実在するrecord_typeとrecord_idだけを書いてください。\n"
            f"QUESTION:\n{question}\nEVIDENCE:\n"
            + json.dumps(records, ensure_ascii=False, allow_nan=False, sort_keys=True)
        )
        parsed = json.loads(
            self._json_text(
                self._generate(
                    [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": prompt}],
                        }
                    ]
                )
            )
        )
        if not isinstance(parsed, dict):
            raise ValueError("evidence_ai_vlm_output_not_object")
        return parsed

    def _ensure_loaded(self) -> None:
        if self._model is None or self._processor is None:
            try:
                import torch
                from transformers import AutoModelForImageTextToText, AutoProcessor
            except ImportError as error:
                raise RuntimeError(
                    "frame_analysis_transformers_not_installed"
                ) from error
            if not torch.cuda.is_available():
                raise RuntimeError("frame_analysis_cuda_not_available")
            # Keep the model fully offline and follow the model author's
            # documented chat-template path.  The generic pipeline adapter in
            # Transformers 5.x forwards generation options to the processor,
            # which can silently ignore deterministic decoding arguments.
            self._processor = AutoProcessor.from_pretrained(
                str(self.model_path),
                local_files_only=True,
            )
            self._model = AutoModelForImageTextToText.from_pretrained(
                str(self.model_path),
                device_map="auto",
                dtype=torch.bfloat16,
                local_files_only=True,
            )

    def _generate(self, messages: list[dict[str, Any]]) -> str:
        assert self._model is not None and self._processor is not None
        inputs = self._processor.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=True,
            return_dict=True,
            return_tensors="pt",
        )
        inputs = inputs.to(self._model.device)
        generated_ids = self._model.generate(
            **inputs,
            max_new_tokens=self.max_new_tokens,
            do_sample=False,
        )
        generated_ids_trimmed = [
            output_ids[len(input_ids) :]
            for input_ids, output_ids in zip(
                inputs.input_ids, generated_ids, strict=True
            )
        ]
        decoded = self._processor.batch_decode(
            generated_ids_trimmed,
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        )
        return decoded[0] if decoded else ""

    @staticmethod
    def _json_text(text: str) -> str:
        cleaned = (
            text.strip()
            .removeprefix("```json")
            .removeprefix("```")
            .removesuffix("```")
            .strip()
        )
        decoder = json.JSONDecoder()
        for offset, character in enumerate(cleaned):
            if character != "{":
                continue
            try:
                _, end = decoder.raw_decode(cleaned[offset:])
                return cleaned[offset : offset + end]
            except json.JSONDecodeError:
                continue
        return cleaned

    @staticmethod
    def _generated_text(output: Any) -> str:
        value = output[0] if isinstance(output, list) and output else output
        if isinstance(value, dict):
            value = value.get("generated_text", value.get("text"))
        if isinstance(value, list) and value:
            last = value[-1]
            value = last.get("content") if isinstance(last, dict) else last
        if isinstance(value, list):
            value = "".join(
                str(item.get("text", "")) if isinstance(item, dict) else str(item)
                for item in value
            )
        if not isinstance(value, str):
            raise ValueError("frame_analysis_vlm_output_missing")
        return value


class RegisteredFrameAnalysisEngine:
    """Read verified MRP references, select frames and append local VLM output."""

    def __init__(
        self,
        repo_root: Path,
        service: FrameAnalysisService,
        analyzer: FrameAnalyzer | None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.service = service
        self.analyzer = analyzer

    def process(self, run: dict[str, Any]) -> dict[str, Any]:
        self._raise_if_cancelled(run["analysis_run_id"])
        frames, source_evidence = self._registered_frames(run)
        self._raise_if_cancelled(run["analysis_run_id"])
        run = self.service.record_source_provenance(
            run["analysis_run_id"], source_evidence
        )
        self._raise_if_cancelled(run["analysis_run_id"])
        selected: list[dict[str, Any]] = []
        persisted_image_names = self.service.persisted_image_names(
            run["analysis_run_id"]
        )
        last_hash_by_camera: dict[str, int] = {}
        threshold = float(run["similarity_threshold"])
        deduplicate = (
            run["parameters"].get("selection_policy_version")
            == SELECTION_POLICY_VERSION
        )
        if self.analyzer is None:
            raise RuntimeError("frame_analysis_analyzer_unavailable")
        neutral = run["parameters"].get("neutral_background_rgb", [127, 127, 127])
        if (
            not isinstance(neutral, list)
            or len(neutral) != 3
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= 255
                for value in neutral
            )
        ):
            raise ValueError("frame_analysis_neutral_background_rgb_invalid")
        for frame in frames:
            self._raise_if_cancelled(run["analysis_run_id"])
            if frame.image_name in persisted_image_names:
                continue
            with Image.open(frame.image_path) as source:
                source.load()
                mask = None
                if frame.mask_path is not None:
                    with Image.open(frame.mask_path) as mask_source:
                        mask_source.load()
                        mask = mask_source.convert("L")
                composite = self._composite(
                    source, tuple(neutral), mask, mask_convention=frame.mask_convention
                )
                if deduplicate:
                    perceptual_hash = self._difference_hash(composite)
                    previous = last_hash_by_camera.get(frame.camera_id)
                    if (
                        previous is not None
                        and self._hash_similarity(previous, perceptual_hash)
                        >= threshold
                    ):
                        continue
                    last_hash_by_camera[frame.camera_id] = perceptual_hash
                analysis_policy_id = run["parameters"].get(
                    "analysis_policy_id", DEFAULT_ANALYSIS_POLICY_ID
                )
                policy = reviewed_analysis_policy(analysis_policy_id)
                if (
                    run["parameters"].get("output_schema_version")
                    != policy.output_schema_version
                ):
                    raise ValueError("frame_analysis_run_output_contract_mismatch")
                analysis = self.analyzer.analyze(
                    composite,
                    analysis_policy_id,
                    run["prompt_version"],
                )
                analysis = {
                    "summary": _analysis_summary(analysis.get("summary")),
                    "keywords": _json_list_of_strings(
                        analysis.get("keywords", []), "keywords"
                    ),
                    "extra": _structured_observation_extra(
                        analysis.get("extra"), analysis_policy_id
                    ),
                }
                self._raise_if_cancelled(run["analysis_run_id"])
                source_bytes = frame.image_path.read_bytes()
                source_content = self.service.field_memory.store_managed_image_bytes(
                    source_bytes
                )
                vlm_content = self.service.field_memory.store_managed_image_bytes(
                    self._encode_jpeg(composite, maximum=None)
                )
                thumbnail_content = self.service.field_memory.store_managed_image_bytes(
                    self._encode_jpeg(composite, maximum=640)
                )
                self._raise_if_cancelled(run["analysis_run_id"])
                record = self.service.add_frame(
                    run["analysis_run_id"],
                    {
                        "image_name": frame.image_name,
                        "source_frame_index": frame.source_frame_index,
                        "source_time_sec": frame.source_time_sec,
                        "camera_id": frame.camera_id,
                        "image_content_sha256": source_content["content_sha256"],
                        "vlm_input_content_sha256": vlm_content["content_sha256"],
                        "thumbnail_content_sha256": thumbnail_content["content_sha256"],
                        "summary": analysis["summary"],
                        "keywords": analysis.get("keywords", []),
                        "extra": analysis.get("extra", {}),
                        "pose": frame.pose,
                        "projection": frame.projection,
                    },
                )
                selected.append(
                    {
                        "image_name": frame.image_name,
                        "source_sha256": source_content["content_sha256"],
                        "vlm_input_sha256": vlm_content["content_sha256"],
                        "frame_analysis_id": record["frame_analysis_id"],
                    }
                )
        manifest = {
            "analysis_run_id": run["analysis_run_id"],
            "scene_version_id": run["scene_version_id"],
            "model_id": run["model_id"],
            "model_revision": run["model_revision"],
            "prompt_version": run["prompt_version"],
            "interval_sec": run["interval_sec"],
            "similarity_threshold": threshold,
            "parameters": run["parameters"],
            "source_evidence": source_evidence,
            "selected": selected,
        }
        digest = hashlib.sha256(
            json.dumps(
                manifest,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        self._raise_if_cancelled(run["analysis_run_id"])
        return self.service.complete_run(run["analysis_run_id"], digest)

    def _raise_if_cancelled(self, analysis_run_id: str) -> None:
        if self.service.cancellation_requested(analysis_run_id):
            raise FrameAnalysisCancelled("frame_analysis_cancelled_by_operator")

    @staticmethod
    def candidate_key(image_name: str) -> str:
        return urlsafe_b64encode(image_name.encode("utf-8")).decode("ascii").rstrip("=")

    @staticmethod
    def candidate_name(candidate_key: str) -> str:
        if (
            not candidate_key
            or len(candidate_key) > 2048
            or not re.fullmatch(r"[A-Za-z0-9_-]+", candidate_key)
        ):
            raise ValueError("frame_analysis_candidate_key_invalid")
        try:
            padded = candidate_key + "=" * (-len(candidate_key) % 4)
            result = urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        except (UnicodeDecodeError, ValueError) as error:
            raise ValueError("frame_analysis_candidate_key_invalid") from error
        if not result or len(result) > 1024:
            raise ValueError("frame_analysis_candidate_key_invalid")
        return result

    def candidates(
        self,
        scene_version_id: str,
        *,
        query: str = "",
        offset: int = 0,
        limit: int = 24,
    ) -> dict[str, Any]:
        """List every registered masked virtual-camera image without writing data."""

        _identifier(scene_version_id, "scene_version_id", prefix="scene")
        if not isinstance(query, str) or len(query) > 256:
            raise ValueError("frame_analysis_candidate_query_invalid")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("frame_analysis_candidate_offset_invalid")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 64
        ):
            raise ValueError("frame_analysis_candidate_limit_invalid")
        frames, _ = self._registered_frames(
            {
                "scene_version_id": scene_version_id,
                "interval_sec": DEFAULT_INTERVAL_SEC,
                "parameters": {
                    "selection_policy_version": REGISTERED_CANDIDATES_POLICY_VERSION
                },
            }
        )
        needle = query.strip().casefold()
        filtered = [
            frame
            for frame in frames
            if not needle
            or needle in frame.image_name.casefold()
            or needle in frame.camera_id.casefold()
        ]
        return {
            "scene_version_id": scene_version_id,
            "count": len(filtered),
            "offset": offset,
            "limit": limit,
            "results": [
                self._candidate_metadata(scene_version_id, frame)
                for frame in filtered[offset : offset + limit]
            ],
        }

    def candidate_preview(self, scene_version_id: str, candidate_key: str) -> bytes:
        """Read one verified input image for the candidate picker; never persists it."""

        image_name = self.candidate_name(candidate_key)
        # Use the same complete, verified candidate collection used by the picker.
        # A preview is intentionally read-only and does not require an individual
        # frame to be present in the sparse camera-trajectory export.  Reusing the
        # explicit-selection policy here incorrectly rejected otherwise valid
        # registered virtual-camera images when their per-image trajectory entry
        # was omitted.
        frames, _ = self._registered_frames(
            {
                "scene_version_id": scene_version_id,
                "interval_sec": DEFAULT_INTERVAL_SEC,
                "parameters": {
                    "selection_policy_version": REGISTERED_CANDIDATES_POLICY_VERSION,
                },
            }
        )
        matching = [frame for frame in frames if frame.image_name == image_name]
        if len(matching) != 1:
            raise FileNotFoundError("frame_analysis_candidate_not_found")
        frame = matching[0]
        with Image.open(frame.image_path) as image:
            image.load()
            mask = None
            if frame.mask_path is not None:
                with Image.open(frame.mask_path) as mask_source:
                    mask_source.load()
                    mask = mask_source.convert("L")
            return self._encode_jpeg(
                self._composite(
                    image, (127, 127, 127), mask, mask_convention=frame.mask_convention
                ),
                maximum=768,
            )

    def _candidate_metadata(
        self, scene_version_id: str, frame: RegisteredFrame
    ) -> dict[str, Any]:
        key = self.candidate_key(frame.image_name)
        return {
            "image_name": frame.image_name,
            "image_key": key,
            "thumbnail_url": (
                f"/api/frame-analysis/scene-versions/{scene_version_id}/candidates/{key}/preview"
            ),
            "source_frame_index": frame.source_frame_index,
            "source_time_sec": frame.source_time_sec,
            "camera_id": frame.camera_id,
            "pose": frame.pose,
            "projection": frame.projection,
        }

    def _registered_frames(
        self, run: dict[str, Any]
    ) -> tuple[list[RegisteredFrame], dict[str, Any]]:
        scene, package = self._scene_and_package(run["scene_version_id"])
        pose_contract = self.service.scene_pose_contract(package["scene_package_id"])
        parameters = run.get("parameters")
        if not isinstance(parameters, dict):
            raise ValueError("frame_analysis_parameters_invalid")
        policy = parameters.get("selection_policy_version", SELECTION_POLICY_VERSION)
        person_only_required = policy == ALL_REGISTERED_IMAGES_POLICY_VERSION
        scene_id = package.get("scene_id")
        reconstruction_id = package.get("reconstruction_id")
        if not isinstance(scene_id, str) or not isinstance(reconstruction_id, str):
            raise ValueError("frame_analysis_scene_package_mrp_identity_missing")
        if (
            pose_contract.scene_id != scene_id
            or pose_contract.reconstruction_id != reconstruction_id
        ):
            raise ValueError(
                "frame_analysis_scene_package_pose_contract_identity_mismatch"
            )
        dataset_manifest_path = pose_contract.package_root / "dataset" / "manifest.json"
        dataset_manifest_hash: str | None = None
        masks_root: Path | None = None
        mask_convention = "white_is_retained"
        person_mask_report_hash: str | None = None
        rejected_person_mask_names: set[str] = set()
        if dataset_manifest_path.is_file():
            dataset = self._read_json(dataset_manifest_path)
            if (
                dataset.get("status") != "verified"
                or dataset.get("scene_id") != scene_id
                or dataset.get("reconstruction_id") != reconstruction_id
                or dataset.get("scene_package_id") != pose_contract.scene_package_id
            ):
                raise ValueError("frame_analysis_scene_dataset_identity_invalid")
            dataset_artifacts = dataset.get("artifacts")
            if not isinstance(dataset_artifacts, dict):
                raise ValueError("frame_analysis_scene_dataset_artifacts_missing")
            metric_dataset = dataset_artifacts.get("metric_reconstruction")
            frame_dataset = dataset_artifacts.get("registered_frames")
            person_mask_dataset = dataset_artifacts.get("person_masks")
            colmap_dataset = dataset_artifacts.get("colmap")
            if not all(
                isinstance(item, dict)
                for item in (
                    metric_dataset,
                    frame_dataset,
                    person_mask_dataset,
                    colmap_dataset,
                )
            ):
                raise ValueError("frame_analysis_scene_dataset_artifacts_invalid")
            if (
                person_only_required
                and dataset.get("schema_version") != PERSON_MASK_DATASET_SCHEMA_VERSION
            ):
                raise ValueError("frame_analysis_person_mask_dataset_v1_1_required")
            if dataset.get("schema_version") == PERSON_MASK_DATASET_SCHEMA_VERSION:
                colmap_mask_dataset = dataset_artifacts.get("colmap_masks")
                mask_generation = dataset_artifacts.get("mask_generation")
                if (
                    person_mask_dataset.get("convention") != "white_is_person"
                    or not isinstance(colmap_mask_dataset, dict)
                    or colmap_mask_dataset.get("convention") != "white_is_retained"
                    or not isinstance(mask_generation, dict)
                    or mask_generation.get("status") != "verified"
                ):
                    raise ValueError("frame_analysis_person_mask_contract_invalid")
                report_uri = mask_generation.get("report_uri")
                expected_report_hash = mask_generation.get("report_sha256")
                if (
                    not isinstance(report_uri, str)
                    or not isinstance(expected_report_hash, str)
                    or SHA256_RE.fullmatch(expected_report_hash) is None
                ):
                    raise ValueError(
                        "frame_analysis_person_mask_report_descriptor_invalid"
                    )
                report_path = (dataset_manifest_path.parent / report_uri).resolve()
                try:
                    report_path.relative_to(pose_contract.package_root.resolve())
                except ValueError as error:
                    raise ValueError(
                        "frame_analysis_person_mask_report_path_invalid"
                    ) from error
                if (
                    not report_path.is_file()
                    or self._sha256_file(report_path) != expected_report_hash
                ):
                    raise ValueError("frame_analysis_person_mask_report_hash_mismatch")
                mask_report = self._read_json(report_path)
                records = mask_report.get("records")
                if mask_report.get("status") != "verified" or not isinstance(
                    records, list
                ):
                    raise ValueError("frame_analysis_person_mask_report_unverified")
                for record in records:
                    if not isinstance(record, dict) or not isinstance(
                        record.get("relative_image"), str
                    ):
                        raise ValueError(
                            "frame_analysis_person_mask_report_records_invalid"
                        )
                    if record.get("coverage_rejected") is True:
                        rejected_person_mask_names.add(record["relative_image"])
                person_mask_report_hash = expected_report_hash
                mask_convention = "white_is_person"
            elif person_only_required:
                raise ValueError("frame_analysis_person_mask_dataset_v1_1_required")
            dataset_root = dataset_manifest_path.parent
            mrp_root = self._dataset_directory(
                pose_contract.package_root,
                dataset_root,
                metric_dataset.get("uri"),
                "metric_reconstruction",
            )
            images_root = self._dataset_directory(
                pose_contract.package_root,
                dataset_root,
                frame_dataset.get("uri"),
                "registered_frames",
            )
            masks_root = self._dataset_directory(
                pose_contract.package_root,
                dataset_root,
                person_mask_dataset.get("uri"),
                "person_masks",
            )
            text_root = self._dataset_directory(
                pose_contract.package_root,
                dataset_root,
                colmap_dataset.get("text_uri"),
                "colmap_text",
            )
            source_interval = _positive_number(
                dataset.get("frame_interval_sec"), "source_frame_interval_sec", 1.0
            )
            dataset_manifest_hash = self._sha256_file(dataset_manifest_path)
        else:
            if person_only_required:
                raise ValueError("frame_analysis_person_mask_dataset_v1_1_required")
            mrp_root = (
                SceneRepositoryLayout.from_root(self.repo_root).metric_reconstructions
                / scene_id
                / reconstruction_id
            ).resolve()
            self._inside_repo(mrp_root)
            lfs_root = self._source_path(mrp_root / "lfs_input" / ".source.json")
            text_root = self._source_path(mrp_root / "colmap" / "text" / ".source.json")
            images_root = lfs_root / "images"
            source_interval = self._source_interval(
                reconstruction_id, run["parameters"]
            )
        manifest_path = mrp_root / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(
                f"frame_analysis_source_mrp_unavailable:{scene_id}/{reconstruction_id}"
            )
        manifest = self._read_json(manifest_path)
        if (
            manifest.get("scene_id") != scene_id
            or manifest.get("reconstruction_id") != reconstruction_id
        ):
            raise ValueError("frame_analysis_mrp_identity_mismatch")
        if manifest.get("coordinate") != {
            "stage": "horizontalized_colmap",
            "unit": "meter",
            "up_axis": "Y",
        }:
            raise ValueError("frame_analysis_mrp_coordinate_contract_invalid")
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, dict):
            raise ValueError("frame_analysis_mrp_artifacts_missing")
        world_alignment_hash = self._verify_artifact_hash(
            mrp_root, artifacts.get("world_alignment"), "uri", "world_alignment"
        )
        trajectory_hash = self._verify_artifact_hash(
            mrp_root, artifacts.get("camera_trajectory"), "uri", "camera_trajectory"
        )
        lfs_manifest_hash = self._verify_artifact_hash(
            mrp_root, artifacts.get("lfs_input"), "manifest_uri", "lfs_input_manifest"
        )
        sfm = artifacts.get("sfm")
        text_source = self._read_json(mrp_root / "colmap" / "text" / ".source.json")
        if not isinstance(sfm, dict) or text_source.get("source_sha256") != sfm.get(
            "model_sha256"
        ):
            raise ValueError("frame_analysis_colmap_source_hash_mismatch")
        lfs_manifest = self._read_json(mrp_root / "lfs_input" / "manifest.json")
        if (
            lfs_manifest.get("status") != "verified"
            or lfs_manifest.get("source_reconstruction_id") != reconstruction_id
        ):
            raise ValueError("frame_analysis_lfs_manifest_identity_invalid")
        lfs_images = lfs_manifest.get("images")
        if not isinstance(lfs_images, dict) or not isinstance(
            lfs_images.get("files"), list
        ):
            raise ValueError("frame_analysis_lfs_image_manifest_invalid")
        if not all(isinstance(name, str) for name in lfs_images["files"]):
            raise ValueError("frame_analysis_lfs_image_manifest_invalid")
        registered_image_files = set(lfs_images["files"])
        cameras = self._parse_cameras(text_root / "cameras.txt")
        poses = self._parse_images(text_root / "images.txt")
        alignment = self._read_json(mrp_root / "config" / "world_alignment.json")
        trajectory = self._trajectory_centers(mrp_root / "camera_trajectory.json")
        transform = alignment.get("generated_to_world")
        if not isinstance(transform, dict):
            raise ValueError("frame_analysis_world_alignment_missing")
        scale = _positive_number(transform.get("scale"), "world_alignment_scale", 1.0)
        qx, qy, qz, qw = _strict_unit_quaternion_xyzw(
            transform.get("rotation_quaternion_xyzw"), "world_alignment"
        )
        translation = transform.get("translation_m")
        if not isinstance(translation, list) or len(translation) != 3:
            raise ValueError("frame_analysis_world_alignment_translation_invalid")
        translation_m = [
            _finite_number(value, "frame_analysis_world_alignment_translation")
            for value in translation
        ]
        alignment_rotation = self._quaternion_matrix(qx, qy, qz, qw)
        dqx, dqy, dqz, dqw = pose_contract.decoder_quaternion_xyzw
        decoder_rotation = self._quaternion_matrix(dqx, dqy, dqz, dqw)
        candidates: list[tuple[int, str, dict[str, Any]]] = []
        for name, pose in poses.items():
            match = FRAME_NAME_RE.search(name)
            if match and name not in registered_image_files:
                raise ValueError(
                    f"frame_analysis_registered_image_not_in_lfs_manifest:{name}"
                )
            if match and (images_root / name).is_file():
                candidates.append((int(match.group(1)), name, pose))
        if not candidates:
            raise ValueError("frame_analysis_registered_masked_images_empty")
        raw_registered_candidate_count = len(candidates)
        if rejected_person_mask_names:
            candidates = [
                item for item in candidates if item[1] not in rejected_person_mask_names
            ]
        selected_names: set[str] | None = None
        selected_indices: set[int] | None = None
        if policy == SELECTED_IMAGES_POLICY_VERSION:
            selected_names = set(
                _selected_image_names(parameters.get("selected_image_names"))
            )
            available_names = {name for _, name, _ in candidates}
            missing = sorted(selected_names - available_names)
            if missing:
                raise ValueError(
                    f"frame_analysis_selected_image_not_registered:{missing[0]}"
                )
        elif policy in {
            REGISTERED_CANDIDATES_POLICY_VERSION,
            ALL_REGISTERED_IMAGES_POLICY_VERSION,
        }:
            selected_names = {name for _, name, _ in candidates}
        elif policy == SELECTION_POLICY_VERSION:
            selected_indices = set()
            last_time = -math.inf
            for index in sorted({item[0] for item in candidates}):
                timestamp = max(0, index - 1) * source_interval
                if timestamp + 1e-9 >= last_time + float(run["interval_sec"]):
                    selected_indices.add(index)
                    last_time = timestamp
        else:
            raise ValueError("frame_analysis_selection_policy_invalid")
        result: list[RegisteredFrame] = []
        for index, name, colmap_pose in sorted(candidates):
            if selected_names is not None and name not in selected_names:
                continue
            if selected_indices is not None and index not in selected_indices:
                continue
            camera_id = colmap_pose["camera_id"]
            camera = cameras.get(camera_id)
            if camera is None:
                raise ValueError(f"frame_analysis_camera_not_found:{camera_id}")
            position, rotation = self._viewer_pose(
                colmap_pose["qvec"],
                colmap_pose["tvec"],
                scale,
                alignment_rotation,
                decoder_rotation,
                translation_m,
            )
            expected_center = trajectory.get(name)
            if expected_center is not None:
                actual_center = self._colmap_camera_center(
                    colmap_pose["qvec"], colmap_pose["tvec"]
                )
                if (
                    max(abs(actual_center[i] - expected_center[i]) for i in range(3))
                    > 1e-6
                ):
                    raise ValueError(
                        f"frame_analysis_camera_trajectory_mismatch:{name}"
                    )
            result.append(
                RegisteredFrame(
                    image_name=name,
                    image_path=(images_root / name).resolve(),
                    mask_path=self._dataset_mask_path(masks_root, name)
                    if masks_root is not None
                    else None,
                    mask_convention=mask_convention,
                    source_frame_index=index,
                    source_time_sec=max(0, index - 1) * source_interval,
                    camera_id=camera_id,
                    pose={
                        "position": dict(zip("xyz", position, strict=True)),
                        "rotation": dict(zip("xyzw", rotation, strict=True)),
                    },
                    projection=camera,
                )
            )
        # Representative runs require trajectory overlap as a source-integrity check.
        # Explicit user selection is allowed for any verified COLMAP/LFS image: the
        # image's COLMAP pose is still transformed through the verified MRP alignment,
        # even when camera_trajectory.json contains only a sampled subset.
        if (
            policy == SELECTION_POLICY_VERSION
            and trajectory
            and not any(frame.image_name in trajectory for frame in result)
        ):
            raise ValueError("frame_analysis_camera_trajectory_no_selected_overlap")
        evidence = {
            "mrp_manifest_sha256": self._sha256_file(manifest_path),
            "world_alignment_sha256": world_alignment_hash,
            "camera_trajectory_sha256": trajectory_hash,
            "lfs_manifest_sha256": lfs_manifest_hash,
            "colmap_model_sha256": str(sfm["model_sha256"]),
            "registered_candidate_count": len(candidates),
            "raw_registered_candidate_count": raw_registered_candidate_count,
            "excessive_person_mask_rejected_count": raw_registered_candidate_count
            - len(candidates),
            "interval_selected_count": len(result)
            if policy == SELECTION_POLICY_VERSION
            else 0,
            "explicit_selected_count": len(result)
            if policy == SELECTED_IMAGES_POLICY_VERSION
            else 0,
            "all_registered_count": len(result)
            if policy == ALL_REGISTERED_IMAGES_POLICY_VERSION
            else 0,
            "source_frame_interval_sec": source_interval,
            "pose_coordinate_contract": self.service._pose_contract_metadata(
                pose_contract
            ),
        }
        if dataset_manifest_hash is not None:
            evidence["scene_dataset_manifest_sha256"] = dataset_manifest_hash
        if person_mask_report_hash is not None:
            evidence["person_mask_report_sha256"] = person_mask_report_hash
            evidence["analysis_mask_contract"] = "semantic-person-only-v1"
        return result, evidence

    @staticmethod
    def _dataset_mask_path(masks_root: Path, image_name: str) -> Path:
        """Resolve both COLMAP's ``image.ext.png`` and legacy same-name mask layout."""

        root = masks_root.resolve()
        candidates = (root / f"{image_name}.png", root / image_name)
        for candidate in candidates:
            path = candidate.resolve()
            try:
                path.relative_to(root)
            except ValueError as error:
                raise ValueError("frame_analysis_person_mask_path_invalid") from error
            if path.is_file() and not path.is_symlink():
                return path
        raise FileNotFoundError(f"frame_analysis_person_mask_missing:{image_name}")

    def _verify_artifact_hash(
        self,
        root: Path,
        descriptor: Any,
        uri_field: str,
        label: str,
    ) -> str:
        if not isinstance(descriptor, dict):
            raise ValueError(f"frame_analysis_mrp_{label}_descriptor_invalid")
        uri = descriptor.get(uri_field)
        expected = descriptor.get("sha256")
        if (
            not isinstance(uri, str)
            or not isinstance(expected, str)
            or SHA256_RE.fullmatch(expected) is None
        ):
            raise ValueError(f"frame_analysis_mrp_{label}_descriptor_invalid")
        path = (root / uri).resolve()
        if path.parent != root and root not in path.parents:
            raise ValueError(f"frame_analysis_mrp_{label}_path_invalid")
        if not path.is_file() or self._sha256_file(path) != expected.lower():
            raise ValueError(f"frame_analysis_mrp_{label}_hash_mismatch")
        return expected.lower()

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _trajectory_centers(self, path: Path) -> dict[str, list[float]]:
        value = self._read_json(path)
        rows = value.get("camera_centers_world")
        if not isinstance(rows, list):
            raise ValueError("frame_analysis_camera_trajectory_invalid")
        result: dict[str, list[float]] = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("image_name"), str):
                raise ValueError("frame_analysis_camera_trajectory_invalid")
            xyz = row.get("xyz_colmap")
            if not isinstance(xyz, list) or len(xyz) != 3:
                raise ValueError("frame_analysis_camera_trajectory_invalid")
            result[row["image_name"]] = [
                _finite_number(component, "frame_analysis_camera_trajectory")
                for component in xyz
            ]
        return result

    def _scene_and_package(
        self, scene_version_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        with self.service.field_memory._connection() as connection:
            row = connection.execute(
                "SELECT * FROM scene_versions WHERE scene_version_id = ?",
                (scene_version_id,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(
                    f"field_memory_scene_version_not_found:{scene_version_id}"
                )
            scene = _row(row)
        package = self.service.field_memory._verified_scene_packages().get(
            scene["scene_package_id"]
        )
        if package is None:
            raise ValueError(
                f"field_memory_scene_package_not_verified:{scene['scene_package_id']}"
            )
        return scene, package

    def _source_path(self, source_file: Path) -> Path:
        source = self._read_json(source_file)
        if source.get("status") != "verified" or not isinstance(
            source.get("source_uri"), str
        ):
            raise ValueError(f"frame_analysis_source_reference_invalid:{source_file}")
        path = SceneRepositoryLayout.from_root(self.repo_root).resolve_uri(
            source["source_uri"]
        )
        self._inside_repo(path)
        if not path.is_dir():
            raise FileNotFoundError(
                f"frame_analysis_source_reference_missing:{source['source_uri']}"
            )
        return path

    @staticmethod
    def _dataset_directory(
        package_root: Path,
        dataset_root: Path,
        uri: Any,
        label: str,
    ) -> Path:
        if (
            not isinstance(uri, str)
            or not uri
            or uri.startswith("/")
            or ".." in Path(uri).parts
        ):
            raise ValueError(f"frame_analysis_scene_dataset_{label}_uri_invalid")
        path = (dataset_root / uri).resolve()
        try:
            path.relative_to(package_root.resolve())
        except ValueError as error:
            raise ValueError(
                f"frame_analysis_scene_dataset_{label}_path_invalid"
            ) from error
        if not path.is_dir() or path.is_symlink():
            raise FileNotFoundError(f"frame_analysis_scene_dataset_{label}_missing")
        return path

    def _source_interval(
        self, reconstruction_id: str, parameters: dict[str, Any]
    ) -> float:
        layout = SceneRepositoryLayout.from_root(self.repo_root)
        for jobs_root in layout.existing_roots(layout.jobs, layout.legacy_jobs):
            for path in sorted(jobs_root.glob("*.scene.json")):
                value = self._read_json(path)
                if value.get("reconstruction_id") == reconstruction_id:
                    preprocessing = value.get("preprocessing")
                    if isinstance(preprocessing, dict):
                        return _positive_number(
                            preprocessing.get("frame_interval_sec"),
                            "source_frame_interval_sec",
                            1.0,
                        )
        raise ValueError("frame_analysis_source_frame_interval_unknown")

    def _inside_repo(self, path: Path) -> None:
        try:
            path.relative_to(self.repo_root)
        except ValueError as error:
            raise ValueError(
                f"frame_analysis_source_outside_repository:{path}"
            ) from error

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"frame_analysis_json_invalid:{path}") from error
        if not isinstance(value, dict):
            raise ValueError(f"frame_analysis_json_not_object:{path}")
        return value

    @staticmethod
    def _parse_cameras(path: Path) -> dict[str, dict[str, Any]]:
        cameras: dict[str, dict[str, Any]] = {}
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 5:
                raise ValueError("frame_analysis_colmap_camera_invalid")
            camera_id, model = parts[0], parts[1]
            width, height = int(parts[2]), int(parts[3])
            params = [float(value) for value in parts[4:]]
            if (
                model in {"SIMPLE_PINHOLE", "SIMPLE_RADIAL", "RADIAL"}
                and len(params) >= 3
            ):
                fx = fy = params[0]
                cx, cy = params[1:3]
            elif (
                model in {"PINHOLE", "OPENCV", "OPENCV_FISHEYE", "FULL_OPENCV"}
                and len(params) >= 4
            ):
                fx, fy, cx, cy = params[:4]
            else:
                raise ValueError(
                    f"frame_analysis_colmap_camera_model_unsupported:{model}"
                )
            if (
                not all(math.isfinite(value) for value in (fx, fy, cx, cy))
                or min(fx, fy, width, height) <= 0
            ):
                raise ValueError("frame_analysis_colmap_camera_intrinsics_invalid")
            cameras[camera_id] = {
                "model": model,
                "width": width,
                "height": height,
                "fx": fx,
                "fy": fy,
                "cx": cx,
                "cy": cy,
                "vertical_fov_deg": math.degrees(2.0 * math.atan(height / (2.0 * fy))),
            }
        return cameras

    @staticmethod
    def _parse_images(path: Path) -> dict[str, dict[str, Any]]:
        lines = path.read_text(encoding="utf-8").splitlines()
        result: dict[str, dict[str, Any]] = {}
        expect_image = True
        for raw in lines:
            line = raw.strip()
            if line.startswith("#"):
                continue
            if not expect_image:
                expect_image = True
                continue
            if not line:
                continue
            parts = line.split(maxsplit=9)
            if len(parts) != 10:
                raise ValueError("frame_analysis_colmap_image_invalid")
            values = [float(value) for value in parts[1:8]]
            if not all(math.isfinite(value) for value in values):
                raise ValueError("frame_analysis_colmap_image_pose_non_finite")
            result[parts[9]] = {
                "qvec": values[:4],
                "tvec": values[4:7],
                "camera_id": parts[8],
            }
            expect_image = False
        return result

    @classmethod
    def _viewer_pose(
        cls,
        qvec_wxyz: list[float],
        tvec: list[float],
        scale: float,
        alignment_rotation: list[list[float]],
        decoder_rotation: list[list[float]],
        translation: list[float],
    ) -> tuple[list[float], list[float]]:
        qw, qx, qy, qz = qvec_wxyz
        norm = math.sqrt(qw * qw + qx * qx + qy * qy + qz * qz)
        if norm == 0 or abs(norm - 1.0) > 1e-6:
            raise ValueError("frame_analysis_colmap_quaternion_not_unit:tolerance=1e-6")
        camera_from_world = cls._quaternion_matrix(qx, qy, qz, qw)
        world_from_camera = cls._transpose(camera_from_world)
        center = [-value for value in cls._matvec(world_from_camera, tvec)]
        decoded_center = cls._matvec(decoder_rotation, center)
        aligned_center = cls._matvec(alignment_rotation, decoded_center)
        position = [
            scale * aligned_center[index] + translation[index] for index in range(3)
        ]
        viewer_from_camera = cls._matmul(
            cls._matmul(alignment_rotation, decoder_rotation), world_from_camera
        )
        rotation = cls._matrix_quaternion(viewer_from_camera)
        return position, rotation

    @classmethod
    def _colmap_camera_center(
        cls, qvec_wxyz: list[float], tvec: list[float]
    ) -> list[float]:
        qw, qx, qy, qz = qvec_wxyz
        norm = math.sqrt(qw * qw + qx * qx + qy * qy + qz * qz)
        if norm == 0 or abs(norm - 1.0) > 1e-6:
            raise ValueError("frame_analysis_colmap_quaternion_not_unit:tolerance=1e-6")
        world_from_camera = cls._transpose(cls._quaternion_matrix(qx, qy, qz, qw))
        return [-value for value in cls._matvec(world_from_camera, tvec)]

    @staticmethod
    def _quaternion_matrix(x: float, y: float, z: float, w: float) -> list[list[float]]:
        return [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]

    @staticmethod
    def _matrix_quaternion(matrix: list[list[float]]) -> list[float]:
        trace = matrix[0][0] + matrix[1][1] + matrix[2][2]
        if trace > 0:
            s = math.sqrt(trace + 1.0) * 2
            w = 0.25 * s
            x = (matrix[2][1] - matrix[1][2]) / s
            y = (matrix[0][2] - matrix[2][0]) / s
            z = (matrix[1][0] - matrix[0][1]) / s
        else:
            index = max(range(3), key=lambda value: matrix[value][value])
            if index == 0:
                s = math.sqrt(1.0 + matrix[0][0] - matrix[1][1] - matrix[2][2]) * 2
                x, y, z, w = (
                    0.25 * s,
                    (matrix[0][1] + matrix[1][0]) / s,
                    (matrix[0][2] + matrix[2][0]) / s,
                    (matrix[2][1] - matrix[1][2]) / s,
                )
            elif index == 1:
                s = math.sqrt(1.0 + matrix[1][1] - matrix[0][0] - matrix[2][2]) * 2
                x, y, z, w = (
                    (matrix[0][1] + matrix[1][0]) / s,
                    0.25 * s,
                    (matrix[1][2] + matrix[2][1]) / s,
                    (matrix[0][2] - matrix[2][0]) / s,
                )
            else:
                s = math.sqrt(1.0 + matrix[2][2] - matrix[0][0] - matrix[1][1]) * 2
                x, y, z, w = (
                    (matrix[0][2] + matrix[2][0]) / s,
                    (matrix[1][2] + matrix[2][1]) / s,
                    0.25 * s,
                    (matrix[1][0] - matrix[0][1]) / s,
                )
        norm = math.sqrt(x * x + y * y + z * z + w * w)
        return [x / norm, y / norm, z / norm, w / norm]

    @staticmethod
    def _transpose(matrix: list[list[float]]) -> list[list[float]]:
        return [[matrix[column][row] for column in range(3)] for row in range(3)]

    @staticmethod
    def _matvec(matrix: list[list[float]], vector: list[float]) -> list[float]:
        return [
            sum(matrix[row][column] * vector[column] for column in range(3))
            for row in range(3)
        ]

    @staticmethod
    def _matmul(left: list[list[float]], right: list[list[float]]) -> list[list[float]]:
        return [
            [
                sum(left[row][k] * right[k][column] for k in range(3))
                for column in range(3)
            ]
            for row in range(3)
        ]

    @staticmethod
    def _composite(
        source: Image.Image,
        background: tuple[int, int, int],
        mask: Image.Image | None = None,
        *,
        mask_convention: str = "white_is_retained",
    ) -> Image.Image:
        rgba = source.convert("RGBA")
        if mask is not None:
            if mask.size != rgba.size:
                raise ValueError("frame_analysis_mask_dimensions_mismatch")
            if mask_convention == "white_is_person":
                rgba.putalpha(ImageOps.invert(mask.convert("L")))
            elif mask_convention == "white_is_retained":
                rgba.putalpha(mask)
            else:
                raise ValueError("frame_analysis_mask_convention_invalid")
        canvas = Image.new("RGBA", rgba.size, (*background, 255))
        canvas.alpha_composite(rgba)
        return canvas.convert("RGB")

    @staticmethod
    def _difference_hash(image: Image.Image) -> int:
        reduced = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        pixels = list(reduced.getdata())
        value = 0
        for row in range(8):
            for column in range(8):
                value = (value << 1) | int(
                    pixels[row * 9 + column] > pixels[row * 9 + column + 1]
                )
        return value

    @staticmethod
    def _hash_similarity(left: int, right: int) -> float:
        return 1.0 - ((left ^ right).bit_count() / 64.0)

    @staticmethod
    def _encode_jpeg(image: Image.Image, maximum: int | None) -> bytes:
        value = image.copy()
        if maximum is not None:
            value.thumbnail((maximum, maximum), Image.Resampling.LANCZOS)
        output = BytesIO()
        value.convert("RGB").save(output, format="JPEG", quality=90, optimize=True)
        return output.getvalue()


def dispatch_frame_analysis_api(
    service: FrameAnalysisService,
    method: str,
    path: str,
    payload: dict[str, Any] | None,
    *,
    enabled_features: frozenset[str],
    actor_id: str = "local-operator",
    actor_role: str = "admin",
    request_id: str | None = None,
) -> Any:
    body = payload or {}
    review_match = re.fullmatch(r"/api/frame-analysis/frames/([^/]+)/reviews", path)
    if review_match and method == "POST":
        require_feature(enabled_features, "frame-analysis")
        return service.review_frame(review_match.group(1), body, actor_id=actor_id)
    frame_match = re.fullmatch(r"/api/frame-analysis/frames/([^/]+)", path)
    if frame_match and method == "GET":
        # Spatial-search results resolve an opaque frame_analysis_id into the
        # already validated Viewer-world camera pose.  This is deliberately a
        # read-only exception: creating runs and switching the active run stay
        # behind the frame-analysis feature boundary.
        require_feature(enabled_features, "frame-analysis", "spatial-search")
        return service.frame(frame_match.group(1))
    require_feature(enabled_features, "frame-analysis")
    if path == "/api/frame-analysis/policies" and method == "GET":
        return analysis_policy_catalog()
    if path == "/api/frame-analysis/models" and method == "GET":
        return service.model_manager.catalog()
    if path == "/api/frame-analysis/models/install" and method == "POST":
        return service.model_manager.install(
            _text(body.get("model_id"), "analysis_model_id", maximum=512)
        )
    if path == "/api/frame-analysis/scene-versions" and method == "GET":
        return service.scene_versions()
    reset_preflight_match = re.fullmatch(
        r"/api/frame-analysis/scene-versions/([^/]+)/reset-preflight", path
    )
    if reset_preflight_match and method == "GET":
        if actor_role != "admin":
            raise PermissionError("spatial_admin_role_required")
        return service.reset_preflight(reset_preflight_match.group(1))
    reset_match = re.fullmatch(
        r"/api/frame-analysis/scene-versions/([^/]+)/reset", path
    )
    if reset_match and method == "POST":
        return service.reset_scene_analysis(
            reset_match.group(1),
            body,
            actor_id=actor_id,
            actor_role=actor_role,
            request_id=request_id,
        )
    candidates_match = re.fullmatch(
        r"/api/frame-analysis/scene-versions/([^/]+)/candidates/query", path
    )
    if candidates_match and method == "POST":
        return RegisteredFrameAnalysisEngine(
            service.field_memory.repo_root, service, None
        ).candidates(
            candidates_match.group(1),
            query=body.get("query", ""),
            offset=body.get("offset", 0),
            limit=body.get("limit", 24),
        )
    selected_images_match = re.fullmatch(
        r"/api/frame-analysis/scene-versions/([^/]+)/selected-images", path
    )
    if selected_images_match and method == "POST":
        request = dict(body)
        request["scene_version_id"] = selected_images_match.group(1)
        request["selected_image_names"] = body.get("image_names")
        return service.create_run(request)
    all_registered_match = re.fullmatch(
        r"/api/frame-analysis/scene-versions/([^/]+)/all-registered-images", path
    )
    if all_registered_match and method == "POST":
        return service.create_all_registered_run(all_registered_match.group(1), body)
    if path == "/api/frame-analysis/runs":
        return (
            service.runs(body.get("scene_version_id"))
            if method == "GET"
            else service.create_run(body)
        )
    recent_frames_match = re.fullmatch(
        r"/api/frame-analysis/runs/([^/]+)/recent-frames", path
    )
    if recent_frames_match and method == "GET":
        return service.recent_frames(recent_frames_match.group(1))
    quality_summary_match = re.fullmatch(
        r"/api/frame-analysis/runs/([^/]+)/quality-summary", path
    )
    if quality_summary_match and method == "GET":
        return service.quality_summary(quality_summary_match.group(1))
    run_match = re.fullmatch(r"/api/frame-analysis/runs/([^/]+)", path)
    if run_match and method == "GET":
        return service.run(run_match.group(1))
    cancel_match = re.fullmatch(r"/api/frame-analysis/runs/([^/]+)/cancel", path)
    if cancel_match and method == "POST":
        return service.cancel_run(cancel_match.group(1))
    activate_match = re.fullmatch(
        r"/api/frame-analysis/scene-versions/([^/]+)/active-run", path
    )
    if activate_match and method == "POST":
        return service.activate(
            activate_match.group(1),
            _text(body.get("analysis_run_id"), "analysis_run_id"),
        )
    raise FileNotFoundError(f"api_endpoint_not_found:{method}:{path}")
