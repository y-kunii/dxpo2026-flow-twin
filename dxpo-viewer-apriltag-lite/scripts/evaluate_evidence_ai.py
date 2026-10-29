#!/usr/bin/env python3
"""Evaluate Evidence AI retrieval and abstention against a versioned fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.control_api.evidence_ai import EvidenceAiService
from services.control_api.field_memory import FieldMemoryService
from services.control_api.spatial_search import SpatialSearchService


def evaluate(fixture_path: Path) -> dict[str, object]:
    raw = fixture_path.read_bytes()
    fixture = json.loads(raw)
    required = {"schema_version", "fixture_id", "site", "assets", "cases"}
    if not isinstance(fixture, dict) or not required.issubset(fixture):
        raise ValueError("evidence_acceptance_fixture_invalid")
    results = []
    with tempfile.TemporaryDirectory(prefix="evidence-ai-acceptance-") as temporary:
        registry = FieldMemoryService(Path(temporary))
        site = registry.create_site(fixture["site"])
        for asset in fixture["assets"]:
            registry.create_asset(site["site_id"], asset)
        evidence = EvidenceAiService(registry, SpatialSearchService(registry))
        for case in fixture["cases"]:
            inquiry = evidence.create({
                "inquiry_id": f"inquiry-{case['case_id']}",
                "question": case["question"],
                "search": {"site_id": site["site_id"], "query": case["search_query"]},
            })
            actual = {
                (item["record_type"], item["record_id"])
                for item in inquiry["evidence_snapshot"]["records"]
            }
            expected = {
                (item["record_type"], item["record_id"])
                for item in case["expected_evidence"]
            }
            retrieval_passed = expected.issubset(actual)
            evidence.claim_next()
            if case["expected_answerability"]:
                first = case["expected_evidence"][0]
                completed = evidence.complete(inquiry["inquiry_id"], {
                    "answer": "fixture-grounded-answer", "is_unknown": False,
                    "citations": [{**first, "excerpt": "fixture evidence"}],
                })
                answerability_passed = not completed["is_unknown"] and bool(completed["citations"])
            else:
                completed = evidence.complete(inquiry["inquiry_id"], {
                    "answer": "", "is_unknown": False, "citations": [],
                })
                answerability_passed = completed["is_unknown"] and not completed["citations"]
            results.append({
                "case_id": case["case_id"], "retrieval": "passed" if retrieval_passed else "failed",
                "answerability": "passed" if answerability_passed else "failed",
                "expected_evidence": sorted(expected), "actual_evidence": sorted(actual),
            })
    status = "passed" if all(
        item["retrieval"] == "passed" and item["answerability"] == "passed" for item in results
    ) else "failed"
    return {
        "schema_version": "1.0.0", "kind": "evidence_ai_acceptance_report", "status": status,
        "fixture_id": fixture["fixture_id"], "fixture_sha256": hashlib.sha256(raw).hexdigest(),
        "generated_at": datetime.now(timezone.utc).isoformat(), "cases": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=ROOT / "tests/fixtures/evidence_ai/acceptance-v1.json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate(args.fixture)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
