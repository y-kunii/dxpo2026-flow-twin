"""Release registry, acceptance gates, and non-destructive retention planning."""

from __future__ import annotations

import json
import hashlib
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from packages.scene_repository import SceneRepositoryLayout

from .products import load_product_profile


RELEASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,191}$")
TRANSITIONS = {
    "draft": frozenset({"candidate", "quarantined"}),
    "candidate": frozenset({"qualified", "quarantined"}),
    "qualified": frozenset({"released", "superseded", "quarantined"}),
    "released": frozenset({"superseded", "quarantined"}),
    "superseded": frozenset({"quarantined"}),
    "quarantined": frozenset({"revoked", "draft"}),
    "revoked": frozenset(),
}
RETENTION_DAYS = {"draft": 30, "candidate": 90, "superseded": 180}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.partial")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def release_records(root: Path) -> list[dict[str, Any]]:
    layout = SceneRepositoryLayout.from_root(root)
    records: list[dict[str, Any]] = []
    if not layout.product_releases.is_dir():
        return records
    for path in layout.product_releases.glob("*/*.json"):
        if path.is_symlink() or not path.is_file():
            continue
        value = _read_json(path)
        if value.get("kind") != "spatial_product_release":
            continue
        value["record_path"] = path.relative_to(root).as_posix()
        records.append(value)
    return sorted(records, key=lambda item: str(item.get("created_at", "")), reverse=True)


def acceptance_records(root: Path) -> list[dict[str, Any]]:
    layout = SceneRepositoryLayout.from_root(root)
    records = []
    for path in layout.acceptance_records.glob("*.json") if layout.acceptance_records.is_dir() else []:
        if path.is_symlink() or not path.is_file():
            continue
        value = _read_json(path)
        if value.get("kind") != "spatial_product_acceptance_record":
            continue
        value["record_path"] = path.relative_to(root).as_posix()
        records.append(value)
    return sorted(records, key=lambda item: str(item.get("created_at", "")), reverse=True)


def _release_path(root: Path, release_id: str) -> Path:
    if RELEASE_ID_RE.fullmatch(release_id) is None:
        raise ValueError("product_release_id_invalid")
    matches = [
        path for path in SceneRepositoryLayout.from_root(root).product_releases.glob(f"*/{release_id}.json")
        if path.is_file() and not path.is_symlink()
    ]
    if len(matches) != 1:
        raise FileNotFoundError("product_release_not_found")
    return matches[0]


def transition_release(
    root: Path,
    release_id: str,
    target: str,
    *,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    if target not in TRANSITIONS:
        raise ValueError("product_release_lifecycle_invalid")
    if not actor.strip() or not reason.strip():
        raise ValueError("product_release_transition_audit_required")
    path = _release_path(root, release_id)
    record = _read_json(path)
    current = str(record.get("lifecycle"))
    if target not in TRANSITIONS.get(current, frozenset()):
        raise ValueError(f"product_release_transition_invalid:{current}:{target}")
    if target == "qualified":
        qualified = [
            item for item in acceptance_records(root)
            if item.get("release_id") == release_id and item.get("result") == "qualified"
        ]
        if not qualified:
            raise ValueError("product_release_qualification_record_required")
    now = datetime.now(timezone.utc).isoformat()
    record["lifecycle"] = target
    record.setdefault("lifecycle_events", []).append({
        "from": current,
        "to": target,
        "actor": actor.strip(),
        "reason": reason.strip(),
        "created_at": now,
    })
    record["updated_at"] = now
    _atomic_json(path, record)
    return record


def record_acceptance(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    release_id = payload.get("release_id")
    if not isinstance(release_id, str):
        raise ValueError("acceptance_release_id_required")
    release = _read_json(_release_path(root, release_id))
    profile_id = release.get("product", {}).get("profile_id")
    if not isinstance(profile_id, str):
        raise ValueError("acceptance_standard_profile_required")
    profile = load_product_profile(profile_id)
    required = profile["acceptance"]["required"]
    tests = payload.get("tests")
    target = payload.get("target")
    dataset = payload.get("dataset")
    if not isinstance(tests, dict) or not isinstance(target, dict) or not isinstance(dataset, dict):
        raise ValueError("acceptance_target_dataset_tests_required")
    if not isinstance(target.get("os"), str) or not isinstance(target.get("architecture"), str):
        raise ValueError("acceptance_target_invalid")
    qualification_target = target.get("qualification_target")
    if qualification_target not in profile["platforms"]["qualification_targets"]:
        raise ValueError("acceptance_target_not_in_product_profile")
    fixture_id = dataset.get("fixture_id")
    fixture_digest = dataset.get("sha256")
    if not isinstance(fixture_id, str) or not fixture_id.strip():
        raise ValueError("acceptance_dataset_fixture_id_required")
    if not isinstance(fixture_digest, str) or re.fullmatch(r"[a-f0-9]{64}", fixture_digest) is None:
        raise ValueError("acceptance_dataset_sha256_required")
    missing = [name for name in required if tests.get(name) != "passed"]
    result = "failed" if any(tests.get(name) == "failed" for name in required) else (
        "qualified" if not missing else "incomplete"
    )
    created_at = datetime.now(timezone.utc)
    acceptance_id = (
        f"acceptance-{profile_id}-{created_at.strftime('%Y%m%dT%H%M%S%fZ')}"
    )
    record = {
        "schema_version": "1.0.0",
        "kind": "spatial_product_acceptance_record",
        "acceptance_id": acceptance_id,
        "release_id": release_id,
        "suite": profile["acceptance"]["suite"],
        "suite_profile_version": profile["metadata"]["version"],
        "target": target,
        "dataset": dataset,
        "tests": tests,
        "required_tests": required,
        "missing_or_failed_tests": missing,
        "result": result,
        "created_at": created_at.isoformat(),
        "evidence": payload.get("evidence", []),
        "notes": payload.get("notes"),
    }
    path = SceneRepositoryLayout.from_root(root).acceptance_records / f"{acceptance_id}.json"
    _atomic_json(path, record)
    return record


def artifact_inventory(root: Path) -> dict[str, Any]:
    layout = SceneRepositoryLayout.from_root(root)
    releases = release_records(root)
    acceptances = acceptance_records(root)
    references = Counter(
        str(artifact.get("sha256"))
        for release in releases
        if release.get("lifecycle") != "revoked"
        for artifact in release.get("artifacts", [])
        if isinstance(artifact, dict) and artifact.get("kind") == "scene_content_pack"
    )
    content = []
    for path in layout.content_packs.glob("*.zip") if layout.content_packs.is_dir() else []:
        if path.is_symlink() or not path.is_file():
            continue
        digest = path.stem
        content.append({
            "sha256": digest,
            "size_bytes": path.stat().st_size,
            "reference_count": references[digest],
            "path": path.relative_to(root).as_posix(),
        })
    release_runtime_paths = {
        str(artifact.get("path"))
        for release in releases
        for artifact in release.get("artifacts", [])
        if isinstance(artifact, dict) and artifact.get("kind") == "runtime_bundle"
    }
    runtimes = []
    for path in layout.application_deliverables.glob("*.zip") if layout.application_deliverables.is_dir() else []:
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        runtimes.append({
            "filename": path.name,
            "path": relative,
            "size_bytes": path.stat().st_size,
            "registered_release": relative in release_runtime_paths,
            "classification": "runtime_bundle" if relative in release_runtime_paths else "legacy_monolithic_distribution",
        })
    partials = []
    for path in layout.application_deliverables.glob("*.partial") if layout.application_deliverables.is_dir() else []:
        if path.is_symlink() or not path.is_file():
            continue
        partials.append({
            "path": path.relative_to(root).as_posix(),
            "size_bytes": path.stat().st_size,
            "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        })
    return {
        "schema_version": "1.0.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "releases": releases,
        "acceptance_records": acceptances,
        "runtime_bundles": runtimes,
        "content_packs": sorted(content, key=lambda item: item["sha256"]),
        "partials": partials,
        "summary": {
            "release_count": len(releases),
            "acceptance_count": len(acceptances),
            "runtime_count": len(runtimes),
            "legacy_runtime_count": sum(not item["registered_release"] for item in runtimes),
            "content_pack_count": len(content),
            "content_pack_bytes": sum(item["size_bytes"] for item in content),
            "partial_count": len(partials),
        },
    }


def retention_preview(root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    inventory = artifact_inventory(root)
    actions: list[dict[str, Any]] = []
    releases = inventory["releases"]
    by_profile: dict[str, list[dict[str, Any]]] = {}
    for release in releases:
        profile = str(release.get("product", {}).get("profile_id"))
        by_profile.setdefault(profile, []).append(release)
    protected_superseded = {
        item["release_id"]
        for rows in by_profile.values()
        for item in [row for row in rows if row.get("lifecycle") == "superseded"][:2]
    }
    for release in releases:
        lifecycle = str(release.get("lifecycle"))
        if lifecycle in {"released", "qualified", "quarantined", "revoked"}:
            continue
        created = datetime.fromisoformat(str(release["created_at"]).replace("Z", "+00:00"))
        age_days = (now - created).days
        threshold = RETENTION_DAYS.get(lifecycle)
        if threshold is not None and age_days >= threshold:
            if lifecycle == "superseded" and release.get("release_id") in protected_superseded:
                continue
            actions.append({
                "action": "delete_release_artifacts_candidate",
                "release_id": release.get("release_id"),
                "lifecycle": lifecycle,
                "age_days": age_days,
                "threshold_days": threshold,
                "automatic": False,
            })
    for item in inventory["content_packs"]:
        if item["reference_count"] == 0:
            actions.append({
                "action": "delete_unreferenced_content_pack_candidate",
                "sha256": item["sha256"],
                "size_bytes": item["size_bytes"],
                "automatic": False,
            })
    for item in inventory["partials"]:
        modified = datetime.fromisoformat(item["modified_at"])
        if now - modified >= timedelta(hours=24):
            actions.append({
                "action": "quarantine_partial_candidate",
                **item,
                "automatic": False,
            })
    for item in inventory["runtime_bundles"]:
        if not item["registered_release"]:
            actions.append({
                "action": "migrate_or_archive_legacy_distribution",
                "path": item["path"],
                "size_bytes": item["size_bytes"],
                "automatic": False,
            })
    for action in actions:
        action["action_id"] = "retention-" + hashlib.sha256(
            json.dumps(action, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
    result = {
        "schema_version": "1.0.0",
        "kind": "spatial_product_retention_preview",
        "generated_at": now.isoformat(),
        "destructive_changes_applied": False,
        "policy": {
            "draft_days": 30,
            "candidate_days": 90,
            "superseded_days": 180,
            "superseded_generations_kept": 2,
            "quarantined_auto_delete": False,
            "legacy_auto_delete": False,
        },
        "actions": actions,
        "reclaimable_bytes": sum(
            int(item.get("size_bytes", 0)) for item in actions
            if item["action"] in {
                "delete_unreferenced_content_pack_candidate",
                "quarantine_partial_candidate",
                "migrate_or_archive_legacy_distribution",
            }
        ),
    }
    result["confirmation_sha256"] = hashlib.sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return result


def apply_retention_plan(
    root: Path,
    plan: dict[str, Any],
    *,
    confirmation_sha256: str,
    actor: str,
) -> dict[str, Any]:
    """Move currently eligible artifacts to recoverable quarantine.

    Targets are re-resolved from a fresh preview; paths supplied by callers
    are never trusted. Permanent deletion is intentionally a separate storage
    administrator responsibility.
    """

    expected = plan.get("confirmation_sha256")
    if not isinstance(expected, str) or expected != confirmation_sha256:
        raise ValueError("product_retention_confirmation_mismatch")
    unsigned = {key: value for key, value in plan.items() if key != "confirmation_sha256"}
    actual = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if actual != expected:
        raise ValueError("product_retention_plan_tampered")
    if not actor.strip() or plan.get("destructive_changes_applied") is not False:
        raise ValueError("product_retention_actor_and_preview_required")
    requested = {
        str(item.get("action_id")): item
        for item in plan.get("actions", [])
        if isinstance(item, dict)
    }
    current = retention_preview(root)
    eligible = {
        str(item.get("action_id")): item
        for item in current["actions"]
        if isinstance(item, dict)
    }
    stale = sorted(set(requested) - set(eligible))
    if stale:
        raise ValueError("product_retention_plan_stale:" + ",".join(stale))
    layout = SceneRepositoryLayout.from_root(root)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    quarantine = layout.runtime / "trash/product-artifacts" / timestamp
    moved: list[dict[str, Any]] = []

    def move(path: Path, category: str) -> None:
        resolved = path.resolve()
        if not resolved.is_relative_to(root.resolve()) or not path.exists() or path.is_symlink():
            raise ValueError("product_retention_target_invalid")
        destination = quarantine / category / path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, destination)
        moved.append({
            "source": resolved.relative_to(root.resolve()).as_posix(),
            "quarantine": destination.relative_to(root.resolve()).as_posix(),
            "size_bytes": destination.stat().st_size,
        })

    inventory = artifact_inventory(root)
    runtime_by_path = {item["path"]: item for item in inventory["runtime_bundles"]}
    content_by_digest = {item["sha256"]: item for item in inventory["content_packs"]}
    release_by_id = {item["release_id"]: item for item in inventory["releases"]}
    for action_id, _action in requested.items():
        current_action = eligible[action_id]
        kind = current_action["action"]
        if kind == "delete_unreferenced_content_pack_candidate":
            item = content_by_digest[current_action["sha256"]]
            if item["reference_count"] != 0:
                raise ValueError("product_retention_content_pack_referenced")
            move(root / item["path"], "content-packs")
        elif kind in {"quarantine_partial_candidate", "migrate_or_archive_legacy_distribution"}:
            move(root / current_action["path"], "runtime-artifacts")
        elif kind == "delete_release_artifacts_candidate":
            release = release_by_id[current_action["release_id"]]
            for artifact in release.get("artifacts", []):
                if not isinstance(artifact, dict) or artifact.get("kind") != "runtime_bundle":
                    continue
                runtime_path = str(artifact.get("path"))
                referenced_elsewhere = any(
                    other.get("release_id") != release["release_id"]
                    and any(
                        isinstance(candidate, dict) and candidate.get("kind") == "runtime_bundle"
                        and candidate.get("path") == runtime_path
                        for candidate in other.get("artifacts", [])
                    )
                    for other in inventory["releases"]
                )
                if not referenced_elsewhere and runtime_path in runtime_by_path:
                    move(root / runtime_path, "runtime-bundles")
            move(_release_path(root, release["release_id"]), "release-records")
        else:
            raise ValueError(f"product_retention_action_unsupported:{kind}")
    manifest = {
        "schema_version": "1.0.0",
        "kind": "spatial_product_retention_quarantine",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "actor": actor.strip(),
        "plan_confirmation_sha256": expected,
        "permanent_deletion_applied": False,
        "moved": moved,
    }
    _atomic_json(quarantine / "quarantine-manifest.json", manifest)
    return manifest
