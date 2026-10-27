"""Human-reviewed SceneVersion change candidates."""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from typing import Any

from .features import require_feature
from .field_memory import FieldMemoryService, _finite_number, _identifier, _json_data, _now, _row, _text


class ChangeReviewService:
    def __init__(self, field_memory: FieldMemoryService) -> None:
        self.field_memory = field_memory

    def detect(self, payload: dict[str, Any]) -> dict[str, Any]:
        site_id = _identifier(payload.get("site_id"), "site_id", prefix="site")
        before_id = _identifier(payload.get("from_scene_version_id"), "scene_version_id", prefix="scene")
        after_id = _identifier(payload.get("to_scene_version_id"), "scene_version_id", prefix="scene")
        threshold = _finite_number(payload.get("movement_threshold_m", 0.10), "movement_threshold_m")
        if threshold < 0:
            raise ValueError("change_review_movement_threshold_invalid")
        if before_id == after_id:
            raise ValueError("change_review_scene_versions_must_differ")
        created_at = _now()
        created: list[str] = []
        with self.field_memory._connection(write=True) as connection:
            versions = connection.execute(
                "SELECT scene_version_id, site_id FROM scene_versions WHERE scene_version_id IN (?, ?)",
                (before_id, after_id),
            ).fetchall()
            if len(versions) != 2 or any(row["site_id"] != site_id for row in versions):
                raise ValueError("change_review_scene_version_site_mismatch")
            observations = connection.execute(
                "SELECT * FROM asset_observations WHERE scene_version_id IN (?, ?)", (before_id, after_id)
            ).fetchall()
            by_asset: dict[str, dict[str, Any]] = {}
            for row in observations:
                by_asset.setdefault(row["asset_id"], {})[row["scene_version_id"]] = dict(row)
            for asset_id, pair in sorted(by_asset.items()):
                before = pair.get(before_id)
                after = pair.get(after_id)
                distance = None
                if before is None:
                    change_type = "appeared"
                elif after is None:
                    change_type = "disappeared"
                else:
                    distance = math.sqrt(sum(
                        (float(after[f"position_{axis}"]) - float(before[f"position_{axis}"])) ** 2
                        for axis in "xyz"
                    ))
                    change_type = "moved" if distance > threshold else "unchanged"
                candidate_id = f"change_{uuid.uuid4().hex}"
                try:
                    connection.execute(
                        "INSERT INTO change_candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                        (
                            candidate_id, site_id, before_id, after_id, asset_id, change_type,
                            json.dumps(before, allow_nan=False, sort_keys=True) if before else None,
                            json.dumps(after, allow_nan=False, sort_keys=True) if after else None,
                            distance, f"position-distance-v1:threshold={threshold:.12g}", created_at,
                        ),
                    )
                    created.append(candidate_id)
                except sqlite3.IntegrityError:
                    existing = connection.execute(
                        "SELECT change_candidate_id FROM change_candidates WHERE from_scene_version_id = ? "
                        "AND to_scene_version_id = ? AND asset_id = ?", (before_id, after_id, asset_id),
                    ).fetchone()
                    if existing is None:
                        raise
                    created.append(existing["change_candidate_id"])
        return {"site_id": site_id, "count": len(created), "candidates": self.candidates(site_id, before_id, after_id)}

    def candidates(
        self, site_id: str, from_scene_version_id: str | None = None,
        to_scene_version_id: str | None = None,
    ) -> list[dict[str, Any]]:
        _identifier(site_id, "site_id", prefix="site")
        clauses = ["site_id = ?"]
        params: list[Any] = [site_id]
        if from_scene_version_id:
            clauses.append("from_scene_version_id = ?")
            params.append(from_scene_version_id)
        if to_scene_version_id:
            clauses.append("to_scene_version_id = ?")
            params.append(to_scene_version_id)
        with self.field_memory._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM change_candidates WHERE " + " AND ".join(clauses) + " ORDER BY created_at",
                tuple(params),
            )
            return [self._candidate(row) for row in rows]

    def review(self, candidate_id: str, payload: dict[str, Any], actor_id: str) -> dict[str, Any]:
        _identifier(candidate_id, "change_candidate_id", prefix="change")
        decision = _text(payload.get("decision"), "change_decision", maximum=16)
        if decision not in {"approved", "rejected", "modified"}:
            raise ValueError("change_review_decision_invalid")
        reason = _text(payload.get("reason"), "change_reason", maximum=2000)
        modified_json = None
        if decision == "modified":
            modified_json = _json_data(payload.get("change"), "modified_change")
        elif payload.get("change") is not None:
            raise ValueError("change_review_change_only_for_modified")
        created_at = _now()
        with self.field_memory._connection(write=True) as connection:
            candidate = connection.execute(
                "SELECT * FROM change_candidates WHERE change_candidate_id = ?", (candidate_id,)
            ).fetchone()
            if candidate is None:
                raise FileNotFoundError(f"change_candidate_not_found:{candidate_id}")
            if candidate["status"] != "pending":
                raise ValueError("change_candidate_already_reviewed")
            connection.execute(
                "INSERT INTO change_review_events VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"review_{uuid.uuid4().hex}", candidate_id, decision, reason, modified_json, actor_id, created_at),
            )
            connection.execute(
                "UPDATE change_candidates SET status = ? WHERE change_candidate_id = ?", (decision, candidate_id)
            )
            if decision != "rejected":
                change = json.loads(modified_json) if modified_json else {
                    "change_type": candidate["change_type"],
                    "before": json.loads(candidate["before_json"]) if candidate["before_json"] else None,
                    "after": json.loads(candidate["after_json"]) if candidate["after_json"] else None,
                    "distance_m": candidate["distance_m"],
                }
                connection.execute(
                    "INSERT INTO confirmed_change_events VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"change_event_{uuid.uuid4().hex}", candidate_id, candidate["site_id"],
                        candidate["asset_id"], json.dumps(change, ensure_ascii=False, allow_nan=False, sort_keys=True),
                        actor_id, created_at,
                    ),
                )
        return self.candidate(candidate_id)

    def candidate(self, candidate_id: str) -> dict[str, Any]:
        with self.field_memory._connection() as connection:
            row = connection.execute(
                "SELECT * FROM change_candidates WHERE change_candidate_id = ?", (candidate_id,)
            ).fetchone()
            if row is None:
                raise FileNotFoundError(f"change_candidate_not_found:{candidate_id}")
            result = self._candidate(row)
            result["reviews"] = [_row(item) for item in connection.execute(
                "SELECT * FROM change_review_events WHERE change_candidate_id = ? ORDER BY created_at",
                (candidate_id,),
            )]
            return result

    @staticmethod
    def _candidate(row: Any) -> dict[str, Any]:
        value = _row(row)
        value["before"] = json.loads(value.pop("before_json")) if value["before_json"] else None
        value["after"] = json.loads(value.pop("after_json")) if value["after_json"] else None
        return value


def dispatch_change_review_api(
    service: ChangeReviewService, method: str, path: str, payload: dict[str, Any] | None,
    *, enabled_features: frozenset[str], actor_id: str,
) -> Any:
    require_feature(enabled_features, "change-review")
    body = payload or {}
    if method == "POST" and path == "/api/change-review/detect":
        return service.detect(body)
    if method == "POST" and path.endswith("/review"):
        candidate_id = path.split("/")[-2]
        return service.review(candidate_id, body, actor_id)
    if method == "GET" and path.startswith("/api/change-review/candidates/"):
        return service.candidate(path.rsplit("/", 1)[1])
    if method == "POST" and path == "/api/change-review/candidates/query":
        return service.candidates(
            str(body.get("site_id", "")), body.get("from_scene_version_id"), body.get("to_scene_version_id")
        )
    raise FileNotFoundError(f"api_endpoint_not_found:{method}:{path}")
