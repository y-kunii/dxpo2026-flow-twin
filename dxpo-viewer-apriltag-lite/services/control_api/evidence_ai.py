"""Evidence-only AI inquiry queue.

The immutable evidence snapshot is created by the existing site-scoped search
service.  Workers can cite only records present in that snapshot and must
return unknown when the snapshot is insufficient.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from services.vlm_model_catalog import DEFAULT_VLM, reviewed_vlm

from .features import require_feature
from .frame_analysis_policies import reviewed_analysis_policy
from .field_memory import FieldMemoryService, _identifier, _now, _row, _text
from .spatial_search import SpatialSearchService


class EvidenceAiService:
    def __init__(
        self, field_memory: FieldMemoryService, search: SpatialSearchService
    ) -> None:
        self.field_memory = field_memory
        self.search = search

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        question = _text(payload.get("question"), "evidence_question", maximum=2000)
        search_request = payload.get("search")
        if not isinstance(search_request, dict):
            raise ValueError("evidence_ai_search_required")
        # Search performs site validation and applies site scope before this
        # snapshot is persisted.  The worker is never allowed to search again.
        evidence = self.search.search(search_request)
        inquiry_id = _identifier(
            payload.get("inquiry_id"), "inquiry_id", prefix="inquiry"
        )
        model = reviewed_vlm(str(payload.get("model_id", DEFAULT_VLM.model_id)))
        created_at = _now()
        # Model-generated frame observations are useful discovery candidates,
        # but they must not become answer evidence until a human accepts or
        # corrects them. Other registry records retain their existing policy.
        records = [
            record for record in evidence["results"] if self._eligible_evidence(record)
        ]
        snapshot = {
            "site_id": evidence["site_id"],
            "query": evidence["query"],
            "records": records,
            "evidence_policy": {
                "frame_analysis": "purpose-compatible-human-reviewed-v2",
                "allowed_review_statuses": ["accepted", "corrected"],
                "requires_grounded_ai_downstream_use": True,
                "requires_usable_image": True,
            },
        }
        with self.field_memory._connection(write=True) as connection:
            connection.execute(
                "INSERT INTO evidence_inquiries(inquiry_id, site_id, question, evidence_snapshot_json, "
                "model_id, model_revision, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)",
                (
                    inquiry_id,
                    evidence["site_id"],
                    question,
                    json.dumps(
                        snapshot, ensure_ascii=False, allow_nan=False, sort_keys=True
                    ),
                    model.model_id,
                    model.revision,
                    created_at,
                ),
            )
        return self.inquiry(inquiry_id)

    @staticmethod
    def _eligible_evidence(record: Any) -> bool:
        if (
            not isinstance(record, dict)
            or record.get("record_type") != "frame_analysis"
        ):
            return True
        extra = record.get("extra")
        quality = extra.get("image_quality") if isinstance(extra, dict) else None
        try:
            policy = reviewed_analysis_policy(record.get("analysis_policy_id"))
        except ValueError:
            return False
        return (
            record.get("review_status") in {"accepted", "corrected"}
            and isinstance(extra, dict)
            and extra.get("schema_version") == policy.output_schema_version
            and "grounded-ai" in policy.downstream_uses
            and isinstance(quality, dict)
            and quality.get("usable") is True
        )

    def inquiries(self, site_id: str) -> list[dict[str, Any]]:
        self.field_memory.site(site_id)
        with self.field_memory._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM evidence_inquiries WHERE site_id = ? ORDER BY created_at DESC",
                (site_id,),
            )
            return [self._inquiry(connection, row) for row in rows]

    def inquiry(self, inquiry_id: str) -> dict[str, Any]:
        _identifier(inquiry_id, "inquiry_id", prefix="inquiry")
        with self.field_memory._connection() as connection:
            row = connection.execute(
                "SELECT * FROM evidence_inquiries WHERE inquiry_id = ?", (inquiry_id,)
            ).fetchone()
            if row is None:
                raise FileNotFoundError(f"evidence_ai_inquiry_not_found:{inquiry_id}")
            return self._inquiry(connection, row)

    def claim_next(self) -> dict[str, Any] | None:
        with self.field_memory._connection(write=True) as connection:
            row = connection.execute(
                "SELECT inquiry_id FROM evidence_inquiries WHERE status = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            started_at = _now()
            changed = connection.execute(
                "UPDATE evidence_inquiries SET status = 'running', started_at = ? "
                "WHERE inquiry_id = ? AND status = 'queued'",
                (started_at, row["inquiry_id"]),
            ).rowcount
            if changed != 1:
                return None
        return self.inquiry(row["inquiry_id"])

    def complete(self, inquiry_id: str, output: dict[str, Any]) -> dict[str, Any]:
        answer = _text(
            output.get("answer", ""), "evidence_answer", required=False, maximum=16000
        )
        is_unknown = output.get("is_unknown")
        if not isinstance(is_unknown, bool):
            raise ValueError("evidence_ai_is_unknown_required")
        raw_citations = output.get("citations", [])
        if not isinstance(raw_citations, list):
            raise ValueError("evidence_ai_citations_invalid")
        completed_at = _now()
        with self.field_memory._connection(write=True) as connection:
            inquiry = connection.execute(
                "SELECT * FROM evidence_inquiries WHERE inquiry_id = ?", (inquiry_id,)
            ).fetchone()
            if inquiry is None:
                raise FileNotFoundError(f"evidence_ai_inquiry_not_found:{inquiry_id}")
            if inquiry["status"] != "running":
                raise ValueError("evidence_ai_inquiry_not_running")
            snapshot = json.loads(inquiry["evidence_snapshot_json"])
            allowed = {
                (record["record_type"], record["record_id"])
                for record in snapshot.get("records", [])
                if isinstance(record, dict)
                and "record_type" in record
                and "record_id" in record
            }
            if not snapshot.get("records"):
                is_unknown = True
            citations: list[tuple[str, str, str]] = []
            for citation in raw_citations:
                if not isinstance(citation, dict):
                    raise ValueError("evidence_ai_citation_invalid")
                record_type = _text(
                    citation.get("record_type"), "citation_record_type", maximum=64
                )
                record_id = _text(
                    citation.get("record_id"), "citation_record_id", maximum=96
                )
                if (record_type, record_id) not in allowed:
                    raise ValueError("evidence_ai_citation_outside_snapshot")
                excerpt = _text(
                    citation.get("excerpt", ""),
                    "citation_excerpt",
                    required=False,
                    maximum=1000,
                )
                citations.append((record_type, record_id, excerpt))
            if not is_unknown and not citations:
                raise ValueError("evidence_ai_grounded_answer_requires_citation")
            if is_unknown:
                citations = []
                if not answer:
                    answer = "根拠が不足しているため回答できません。"
            connection.execute(
                "UPDATE evidence_inquiries SET status = 'completed', answer = ?, is_unknown = ?, "
                "completed_at = ? WHERE inquiry_id = ?",
                (answer, int(is_unknown), completed_at, inquiry_id),
            )
            for ordinal, (record_type, record_id, excerpt) in enumerate(citations):
                connection.execute(
                    "INSERT INTO evidence_citations VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        f"citation_{uuid.uuid4().hex}",
                        inquiry_id,
                        record_type,
                        record_id,
                        ordinal,
                        excerpt,
                    ),
                )
        return self.inquiry(inquiry_id)

    def fail(self, inquiry_id: str, reason: str) -> dict[str, Any]:
        failure = _text(reason, "evidence_failure_reason", maximum=2000)
        with self.field_memory._connection(write=True) as connection:
            connection.execute(
                "UPDATE evidence_inquiries SET status = 'failed', failure_reason = ?, completed_at = ? "
                "WHERE inquiry_id = ? AND status = 'running'",
                (failure, _now(), inquiry_id),
            )
        return self.inquiry(inquiry_id)

    @staticmethod
    def _inquiry(connection: Any, row: Any) -> dict[str, Any]:
        value = _row(row)
        value["evidence_snapshot"] = json.loads(value.pop("evidence_snapshot_json"))
        value["is_unknown"] = bool(value["is_unknown"])
        value["citations"] = [
            _row(citation)
            for citation in connection.execute(
                "SELECT * FROM evidence_citations WHERE inquiry_id = ? ORDER BY ordinal",
                (value["inquiry_id"],),
            )
        ]
        return value


def dispatch_evidence_ai_api(
    service: EvidenceAiService,
    method: str,
    path: str,
    payload: dict[str, Any] | None,
    *,
    enabled_features: frozenset[str],
) -> Any:
    require_feature(enabled_features, "evidence-ai")
    body = payload or {}
    if path == "/api/evidence-ai/inquiries" and method == "POST":
        return service.create(body)
    if path == "/api/evidence-ai/inquiries/query" and method == "POST":
        return service.inquiries(str(body.get("site_id", "")))
    if path.startswith("/api/evidence-ai/inquiries/") and method == "GET":
        return service.inquiry(path.rsplit("/", 1)[1])
    raise FileNotFoundError(f"api_endpoint_not_found:{method}:{path}")
