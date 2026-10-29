#!/usr/bin/env python3
"""Evaluate Change Audit detection and human-review publication from a fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.control_api.change_review import ChangeReviewService
from services.control_api.field_memory import FieldMemoryService


def _pose(position: list[float]) -> dict[str, Any]:
    return {
        "position": {"x": position[0], "y": position[1], "z": position[2]},
        "rotation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
    }


def evaluate(fixture_path: Path) -> dict[str, Any]:
    raw = fixture_path.read_bytes()
    fixture = json.loads(raw)
    required = {"schema_version", "fixture_id", "site_id", "scene_versions", "assets", "movement_threshold_m"}
    if not isinstance(fixture, dict) or not required.issubset(fixture):
        raise ValueError("change_audit_acceptance_fixture_invalid")
    before = fixture["scene_versions"]["before"]
    after = fixture["scene_versions"]["after"]
    packages = [before["scene_package_id"], after["scene_package_id"]]
    results = []
    with tempfile.TemporaryDirectory(prefix="change-audit-acceptance-") as temporary:
        registry = FieldMemoryService(
            Path(temporary),
            scene_packages_provider=lambda: [
                {"scene_package_id": item, "scene_id": fixture["fixture_id"], "status": "verified"}
                for item in packages
            ],
        )
        registry.create_site({"site_id": fixture["site_id"], "name": "Change Audit Fixture"})
        for label, scene in (("before", before), ("after", after)):
            registry.create_scene_version(fixture["site_id"], {
                "scene_version_id": scene["scene_version_id"],
                "scene_package_id": scene["scene_package_id"],
                "label": label,
            })
        for asset in fixture["assets"]:
            registry.create_asset(fixture["site_id"], {
                "asset_id": asset["asset_id"], "name": asset.get("name", asset["asset_id"]),
            })
            for label, scene in (("before", before), ("after", after)):
                position = asset.get(label)
                if position is not None:
                    registry.create_observation(asset["asset_id"], {
                        "observation_id": f"observation-{asset['asset_id']}-{label}",
                        "scene_version_id": scene["scene_version_id"],
                        "pose": _pose(position),
                    })
        service = ChangeReviewService(registry)
        detected = service.detect({
            "site_id": fixture["site_id"],
            "from_scene_version_id": before["scene_version_id"],
            "to_scene_version_id": after["scene_version_id"],
            "movement_threshold_m": fixture["movement_threshold_m"],
        })
        by_asset = {item["asset_id"]: item for item in detected["candidates"]}
        for expected in fixture["assets"]:
            candidate = by_asset.get(expected["asset_id"])
            detection_passed = candidate is not None and candidate["change_type"] == expected["expected_change_type"]
            distance_limit = expected.get("minimum_distance_m")
            if distance_limit is not None:
                detection_passed = detection_passed and float(candidate.get("distance_m") or 0.0) >= float(distance_limit)
            publication_passed = False
            if candidate is not None:
                reviewed = service.review(candidate["change_candidate_id"], {
                    "decision": expected["decision"],
                    "reason": "acceptance fixture decision",
                }, actor_id="acceptance-reviewer")
                publication_passed = reviewed["status"] == expected["decision"]
            results.append({
                "asset_id": expected["asset_id"],
                "detection": "passed" if detection_passed else "failed",
                "review": "passed" if publication_passed else "failed",
                "expected_change_type": expected["expected_change_type"],
                "actual_change_type": candidate.get("change_type") if candidate else None,
            })
        expected_confirmed = sum(item["decision"] != "rejected" for item in fixture["assets"])
        confirmed = registry.summary()["counts"]["confirmed_changes"]
        confirmed_passed = confirmed == expected_confirmed
    status = "passed" if confirmed_passed and all(
        item["detection"] == "passed" and item["review"] == "passed" for item in results
    ) else "failed"
    return {
        "schema_version": "1.0.0",
        "kind": "change_audit_acceptance_report",
        "status": status,
        "fixture_id": fixture["fixture_id"],
        "fixture_sha256": hashlib.sha256(raw).hexdigest(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scene_versions": fixture["scene_versions"],
        "cases": results,
        "confirmed_changes": {"expected": expected_confirmed, "actual": confirmed},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture", type=Path,
        default=ROOT / "tests/fixtures/change_audit/acceptance-v1.json",
    )
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
