#!/usr/bin/env python3
"""Move flat data directories into the semantic Scene Repository v2 layout.

The migration is same-filesystem, restart-safe at directory boundaries, and
fail-closed on collisions or active pipeline jobs. It removes obsolete flat
directories only after their payloads have been moved or hash-deduplicated.
Historical manifest URIs remain readable through ``SceneRepositoryLayout``.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packages.scene_repository import (  # noqa: E402
    ACTIVE_JOB_STATUSES,
    SceneRepositoryLayout,
    catalog_status,
    rebuild_catalog,
)


MIGRATION_ID = "scene_repository_v2"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _relative(layout: SceneRepositoryLayout, path: Path) -> str:
    return Path(os.path.abspath(path)).relative_to(layout.root).as_posix()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pid_is_alive(value: Any) -> bool:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _active_jobs(layout: SceneRepositoryLayout) -> list[str]:
    active: list[str] = []
    for root in layout.existing_roots(layout.jobs, layout.legacy_jobs):
        for path in root.glob("*.json"):
            if path.name.endswith(".scene.json"):
                continue
            job = _read_json(path)
            if job.get("status") in ACTIVE_JOB_STATUSES and _pid_is_alive(job.get("pid")):
                active.append(str(job.get("job_id") or path.stem))
    return sorted(set(active))


def _flat_moves(layout: SceneRepositoryLayout) -> list[tuple[Path, Path]]:
    return [
        (layout.legacy_scene_packages, layout.scene_packages),
        (layout.legacy_metric_reconstructions, layout.metric_reconstructions),
        (layout.legacy_source_files, layout.source_files),
        (layout.legacy_catalog, layout.catalog),
    ]


def _data_work_payloads(layout: SceneRepositoryLayout) -> list[str]:
    payloads: list[str] = []
    if not layout.legacy_work.is_dir():
        return payloads
    for current, directories, files in os.walk(layout.legacy_work, followlinks=False):
        current_path = Path(current)
        directories[:] = [name for name in directories if not (current_path / name).is_symlink()]
        for name in files:
            path = current_path / name
            if not path.is_symlink() and path != layout.legacy_work / ".gitkeep":
                payloads.append(_relative(layout, path))
    return sorted(payloads)


def _job_uri_compatibility(layout: SceneRepositoryLayout) -> dict[str, Any]:
    """Verify that moved job metadata still resolves without rewriting history."""
    failures: list[str] = []
    legacy_uri_count = 0
    recoverable_input_count = 0
    record_count = 0
    for path in sorted(layout.jobs.glob("*.json")):
        if path.name.endswith(".scene.json"):
            continue
        record = _read_json(path)
        if not record:
            continue
        record_count += 1
        for key in ("config_uri", "report_uri"):
            uri = record.get(key)
            if not isinstance(uri, str) or not uri:
                if key == "config_uri" or record.get("status") == "verified":
                    failures.append(f"job_{key}_missing:{path.stem}")
                continue
            rewritten = layout.rewrite_legacy_uri(uri)
            legacy_uri_count += int(rewritten != uri.replace("\\", "/").lstrip("./"))
            resolved = layout.resolve_uri(uri)
            if not resolved.is_file() and (key == "config_uri" or record.get("status") == "verified"):
                failures.append(f"job_{key}_unresolved:{path.stem}:{uri}")
        config_uri = record.get("config_uri")
        config_path = layout.resolve_uri(config_uri) if isinstance(config_uri, str) else None
        config = _read_json(config_path) if config_path is not None and config_path.is_file() else {}
        if record.get("status") == "verified":
            for key in ("workspace", "input_video"):
                uri = config.get(key)
                resolved = layout.resolve_uri(uri) if isinstance(uri, str) else None
                if key == "input_video" and (resolved is None or not resolved.is_file()):
                    workspace_uri = config.get("workspace")
                    workspace = layout.resolve_uri(workspace_uri) if isinstance(workspace_uri, str) else None
                    manifest = _read_json(workspace / "input_manifest.json") if workspace is not None else {}
                    digest = manifest.get("sha256")
                    candidates = (
                        [
                            candidate
                            for candidate in (layout.sources / digest).glob("asset.*")
                            if candidate.is_file()
                        ]
                        if isinstance(digest, str) and len(digest) == 64
                        else []
                    )
                    expected_size = manifest.get("size")
                    if len(candidates) == 1 and (
                        not isinstance(expected_size, int)
                        or candidates[0].stat().st_size == expected_size
                    ):
                        recoverable_input_count += 1
                        resolved = candidates[0]
                if resolved is None or not resolved.exists():
                    failures.append(f"job_config_{key}_unresolved:{path.stem}:{uri}")
                elif isinstance(uri, str) and layout.rewrite_legacy_uri(uri) != uri.replace("\\", "/").lstrip("./"):
                    legacy_uri_count += 1
    return {
        "status": "failed" if failures else "verified",
        "record_count": record_count,
        "legacy_uri_count": legacy_uri_count,
        "recoverable_input_count": recoverable_input_count,
        "failure_reasons": sorted(set(failures)),
    }


def _input_files(layout: SceneRepositoryLayout) -> list[dict[str, Any]]:
    input_root = layout.root / "data/input"
    result: list[dict[str, Any]] = []
    if not input_root.is_dir():
        return result
    for source in sorted(path for path in input_root.rglob("*") if path.is_file() and not path.is_symlink()):
        if source.name == ".gitkeep":
            continue
        digest = _sha256_file(source)
        suffix = source.suffix.lower() or ".bin"
        result.append({
            "source": source,
            "legacy_uri": _relative(layout, source),
            "sha256": digest,
            "size_bytes": source.stat().st_size,
            "modified_at": source.stat().st_mtime,
            "target": layout.sources / digest / f"asset{suffix}",
        })
    return result


def audit(root: Path) -> dict[str, Any]:
    layout = SceneRepositoryLayout.from_root(root)
    active_jobs = _active_jobs(layout)
    collisions = [
        {"source": _relative(layout, source), "target": _relative(layout, target)}
        for source, target in _flat_moves(layout)
        if source.exists() and target.exists()
    ]
    input_files = _input_files(layout)
    input_failures: list[str] = []
    for item in input_files:
        target = item["target"]
        legacy_target = layout.legacy_source_files / "sha256" / item["sha256"] / target.name
        existing = target if target.is_file() else legacy_target
        if existing.is_file() and (
            existing.stat().st_size != item["size_bytes"]
            or _sha256_file(existing) != item["sha256"]
        ):
            input_failures.append(f"source_hash_collision:{item['legacy_uri']}")
    work_payloads = _data_work_payloads(layout)
    status = "blocked" if active_jobs or collisions or input_failures or work_payloads else "ready"
    return {
        "status": status,
        "migration_id": MIGRATION_ID,
        "target_root": "data/scene_repository",
        "active_jobs": active_jobs,
        "collisions": collisions,
        "legacy_work_payloads": work_payloads,
        "input_asset_count": len(input_files),
        "input_asset_bytes": sum(int(item["size_bytes"]) for item in input_files),
        "input_failures": input_failures,
        "flat_moves": [
            {"source": _relative(layout, source), "target": _relative(layout, target)}
            for source, target in _flat_moves(layout) if source.exists()
        ],
    }


def _move(source: Path, target: Path, layout: SceneRepositoryLayout, completed: list[dict[str, str]]) -> None:
    if not source.exists():
        return
    if target.exists():
        raise FileExistsError(f"repository_layout_target_exists:{target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, target)
    completed.append({"source": _relative(layout, source), "target": _relative(layout, target)})


def _move_site_records(layout: SceneRepositoryLayout, completed: list[dict[str, str]]) -> None:
    source = layout.legacy_site_records
    if not source.is_dir():
        return
    layout.site_records.mkdir(parents=True, exist_ok=True)
    mapping = {
        "field_memory.sqlite3": layout.site_registry,
        "registry.sqlite3": layout.site_registry,
        "content": layout.site_records / "attachments",
        "backups": layout.site_records / "backups",
        "acceptance": layout.site_records / "acceptance",
        "derived": layout.site_record_indexes,
        "staging": layout.site_record_staging,
    }
    known = set(mapping) | {".gitkeep"}
    unknown = [path.name for path in source.iterdir() if path.name not in known]
    if unknown:
        raise ValueError("unknown_legacy_site_record_entries:" + ",".join(sorted(unknown)))
    for name, target in mapping.items():
        _move(source / name, target, layout, completed)
    (source / ".gitkeep").unlink(missing_ok=True)
    source.rmdir()


def _move_deliverables(layout: SceneRepositoryLayout, completed: list[dict[str, str]]) -> None:
    source = layout.legacy_deliverables
    if not source.is_dir():
        return
    _move(source / "app_distribution", layout.application_deliverables, layout, completed)
    _move(source / "viewer_distribution", layout.viewer_deliverables, layout, completed)
    layout.reports.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.iterdir()):
        if path.name == ".gitkeep":
            path.unlink()
            continue
        _move(path, layout.reports / path.name, layout, completed)
    source.rmdir()


def _store_input_assets(layout: SceneRepositoryLayout) -> list[dict[str, Any]]:
    stored: list[dict[str, Any]] = []
    for item in _input_files(layout):
        source: Path = item["source"]
        target: Path = item["target"]
        target.parent.mkdir(parents=True, exist_ok=True)
        reused = target.is_file()
        if reused:
            if _sha256_file(target) != item["sha256"]:
                raise FileExistsError(f"source_hash_collision:{target}")
            source.unlink()
        else:
            assets = [path for path in target.parent.glob("asset.*") if path.is_file()]
            if assets:
                if len(assets) != 1 or _sha256_file(assets[0]) != item["sha256"]:
                    raise FileExistsError(f"source_hash_collision:{target.parent}")
                target = assets[0]
                source.unlink()
                reused = True
            else:
                os.replace(source, target)
        metadata_path = target.parent / "upload.json"
        metadata = _read_json(metadata_path)
        legacy_paths = metadata.get("legacy_paths")
        legacy_paths = list(legacy_paths) if isinstance(legacy_paths, list) else []
        if item["legacy_uri"] not in legacy_paths:
            legacy_paths.append(item["legacy_uri"])
        metadata.update({
            "schema_version": "1.0.0",
            "sha256": item["sha256"],
            "size_bytes": item["size_bytes"],
            "original_filename": metadata.get("original_filename") or source.name,
            "stored_at": metadata.get("stored_at") or datetime.fromtimestamp(
                item["modified_at"], timezone.utc
            ).isoformat(),
            "legacy_paths": sorted(legacy_paths),
        })
        _write_json(metadata_path, metadata)
        stored.append({
            "source": item["legacy_uri"],
            "target": _relative(layout, target),
            "sha256": item["sha256"],
            "size_bytes": item["size_bytes"],
            "reused": reused,
        })
    input_root = layout.root / "data/input"
    if input_root.is_dir():
        for path in sorted(input_root.rglob("*"), key=lambda value: len(value.parts), reverse=True):
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        input_root.rmdir()
    return stored


def _retire_legacy_work(layout: SceneRepositoryLayout) -> None:
    if not layout.legacy_work.is_dir():
        return
    if _data_work_payloads(layout):
        raise ValueError("legacy_work_contains_payload")
    for path in sorted(layout.legacy_work.rglob("*"), key=lambda value: len(value.parts), reverse=True):
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            path.rmdir()
    layout.legacy_work.rmdir()


def apply_migration(root: Path) -> dict[str, Any]:
    layout = SceneRepositoryLayout.from_root(root)
    report = audit(layout.root)
    if report["status"] != "ready":
        raise RuntimeError("scene_repository_v2_preflight_blocked")
    completed: list[dict[str, str]] = []

    # Move source files before processing legacy data/input so duplicate input
    # files can be verified against the final content-addressed store.
    for source, target in _flat_moves(layout):
        _move(source, target, layout, completed)
    _move_site_records(layout, completed)
    _move_deliverables(layout, completed)
    stored_inputs = _store_input_assets(layout)
    _retire_legacy_work(layout)
    layout.ensure_runtime_roots()

    catalog = rebuild_catalog(layout.root)
    if layout.site_registry.is_file():
        with sqlite3.connect(f"file:{layout.site_registry}?mode=ro", uri=True) as connection:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("site_records_registry_integrity_failed")
    journal = {
        "schema_version": "2.0.0",
        "migration_id": MIGRATION_ID,
        "status": "verified",
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "repository_root": "data/scene_repository",
        "moves": completed,
        "content_addressed_inputs": stored_inputs,
        "legacy_work_retired": True,
        "flat_data_paths_retired": True,
        "catalog": catalog,
    }
    journal_path = layout.migrations / f"{MIGRATION_ID}.json"
    _write_json(journal_path, journal)
    return {
        "status": "verified",
        "migration_id": MIGRATION_ID,
        "journal_uri": _relative(layout, journal_path),
        "moved_count": len(completed),
        "input_asset_count": len(stored_inputs),
        "catalog": catalog,
    }


def verify(root: Path) -> dict[str, Any]:
    layout = SceneRepositoryLayout.from_root(root)
    failures: list[str] = []
    legacy_paths = (
        layout.legacy_catalog,
        layout.legacy_scene_packages,
        layout.legacy_metric_reconstructions,
        layout.legacy_source_files,
        layout.legacy_site_records,
        layout.legacy_deliverables,
        layout.root / "data/input",
        layout.legacy_work,
    )
    for path in legacy_paths:
        if path.exists() or path.is_symlink():
            failures.append(f"legacy_flat_path_remains:{_relative(layout, path)}")
    required = (
        layout.scene_packages,
        layout.metric_reconstructions,
        layout.sources,
        layout.site_records,
        layout.deliverables,
        layout.catalog,
    )
    for path in required:
        if not path.exists():
            failures.append(f"repository_path_missing:{_relative(layout, path)}")
    catalog = catalog_status(layout.root)
    if catalog.get("status") != "verified":
        failures.append("repository_catalog_not_verified")
    data_entries = sorted(path.name for path in (layout.root / "data").iterdir())
    if data_entries != ["scene_repository"]:
        failures.append("data_root_not_consolidated:" + ",".join(data_entries))
    journal = _read_json(layout.migrations / f"{MIGRATION_ID}.json")
    if journal.get("status") != "verified":
        failures.append("scene_repository_v2_journal_missing")
    job_uris = _job_uri_compatibility(layout)
    failures.extend(job_uris["failure_reasons"])
    return {
        "status": "failed" if failures else "verified",
        "migration_id": MIGRATION_ID,
        "repository_root": "data/scene_repository",
        "catalog": catalog,
        "data_root_entries": data_entries,
        "job_uri_compatibility": job_uris,
        "failure_reasons": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("audit", "apply", "catalog", "verify"))
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        if args.command == "audit":
            result = audit(args.repo_root)
        elif args.command == "apply":
            result = apply_migration(args.repo_root)
        elif args.command == "catalog":
            result = rebuild_catalog(args.repo_root)
        else:
            result = verify(args.repo_root)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        result = {"status": "failed", "failure_reasons": [str(error)]}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"ready", "verified"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
