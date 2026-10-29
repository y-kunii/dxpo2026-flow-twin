#!/usr/bin/env python3
"""Create a non-destructive inventory of canonical and derived artifacts.

The report is deliberately advisory: it never removes, moves, or rewrites an
artifact.  In particular, an MRP that is not referenced by a currently visible
Scene Package is an archive candidate, not a deletion candidate.  That keeps
rebuild, audit, and provenance paths intact until an operator makes a separate
retention decision.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from packages.scene_repository import SceneRepositoryLayout


LEGACY_SCENE_NAMES = ("room_office_f26_apriltag",)
ASSET_SUFFIXES = {".spz", ".ply", ".rad", ".splat", ".glb"}


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def canonical_scene_packages(root: Path) -> list[dict[str, Any]]:
    packages_root = SceneRepositoryLayout.from_root(root).scene_packages
    packages: list[dict[str, Any]] = []
    if not packages_root.is_dir():
        return packages
    for manifest_path in sorted(packages_root.glob("*/scene_manifest.json")):
        manifest = read_json(manifest_path)
        if manifest is None:
            continue
        package_id = manifest.get("scene_package_id")
        scene_id = manifest.get("scene_id")
        reconstruction_id = manifest.get("reconstruction_id")
        if not all(isinstance(value, str) and value for value in (package_id, scene_id, reconstruction_id)):
            continue
        packages.append({
            "path": relative(root, manifest_path.parent),
            "schema_version": manifest.get("schema_version"),
            "project_id": manifest.get("project_id"),
            "scene_id": scene_id,
            "scene_package_id": package_id,
            "reconstruction_id": reconstruction_id,
            "status": "canonical",
        })
    return packages


def metric_reconstruction_packages(root: Path, references: set[tuple[str, str]]) -> list[dict[str, Any]]:
    mrp_root = SceneRepositoryLayout.from_root(root).metric_reconstructions
    records: list[dict[str, Any]] = []
    if not mrp_root.is_dir():
        return records
    for manifest_path in sorted(mrp_root.glob("*/*/manifest.json")):
        manifest = read_json(manifest_path)
        if manifest is None:
            continue
        scene_id = manifest.get("scene_id", manifest_path.parent.parent.name)
        reconstruction_id = manifest.get("reconstruction_id", manifest_path.parent.name)
        if not isinstance(scene_id, str) or not isinstance(reconstruction_id, str):
            continue
        active = (scene_id, reconstruction_id) in references
        records.append({
            "path": relative(root, manifest_path.parent),
            "scene_id": scene_id,
            "reconstruction_id": reconstruction_id,
            "lifecycle": "active" if active else "archive",
            "reason": (
                "referenced_by_canonical_scene_package"
                if active
                else "not_referenced_by_a_current_canonical_scene_package; retain for rebuild, audit, and provenance review"
            ),
        })
    return records


def runtime_candidates(root: Path, package_ids: set[str]) -> list[dict[str, str]]:
    runtime_root = root / "web_viewer" / "app" / "public" / "scene_package" / "runtime"
    candidates: list[dict[str, str]] = []
    if not runtime_root.is_dir():
        return candidates
    for child in sorted(runtime_root.iterdir()):
        if not child.is_dir():
            continue
        candidates.append({
            "path": relative(root, child),
            "classification": "derived_runtime_copy" if child.name in package_ids else "orphaned_derived_runtime_copy",
            "action": "do_not_delete_automatically; regenerate from the canonical Scene Package after an operator stops affected Viewer sessions",
        })
    return candidates


def legacy_static_candidates(root: Path) -> list[dict[str, str]]:
    package_root = root / "web_viewer" / "app" / "public" / "scene_package"
    candidates: list[dict[str, str]] = []
    if not package_root.is_dir():
        return candidates
    for child in sorted(package_root.iterdir()):
        if child.name in {"runtime", "distribution_index.json"}:
            continue
        candidates.append({
            "path": relative(root, child),
            "classification": "legacy_static_viewer_asset",
            "action": "review before deletion; current Viewer loading accepts only an explicit runtime Scene Package",
        })
    return candidates


def unreferenced_public_assets(root: Path) -> list[dict[str, str]]:
    assets_root = root / "web_viewer" / "app" / "public" / "assets"
    if not assets_root.is_dir():
        return []
    def uncommented_source(path: Path) -> str:
        source = path.read_text(encoding="utf-8", errors="ignore")
        source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
        return "\n".join(line.split("//", 1)[0] for line in source.splitlines())

    source_text = "\n".join(
        uncommented_source(path)
        for folder in (root / "web_viewer" / "app" / "src", root / "web_viewer" / "app" / "scripts")
        if folder.is_dir()
        for path in folder.rglob("*")
        if path.is_file() and path.suffix in {".ts", ".tsx", ".js", ".mjs"}
    )
    candidates: list[dict[str, str]] = []
    for asset in sorted(path for path in assets_root.rglob("*") if path.is_file() and path.suffix.lower() in ASSET_SUFFIXES):
        if asset.name not in source_text:
            candidates.append({
                "path": relative(root, asset),
                "classification": "unreferenced_public_gaussian_asset",
                "action": "review before deletion; no reference was found in Viewer source or build scripts",
            })
    return candidates


def legacy_path_references(root: Path) -> list[str]:
    results: set[str] = set()
    excluded = {".git", "node_modules", "dist", "data", "public"}
    for path in root.rglob("*"):
        if not path.is_file() or any(part in excluded for part in path.parts):
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        if path.suffix not in {".py", ".ts", ".md", ".json", ".toml", ".yml", ".yaml", ".mjs", ".sh"}:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(name in content for name in LEGACY_SCENE_NAMES):
            results.add(relative(root, path))
    return sorted(results)


def build_report(root: Path) -> dict[str, Any]:
    packages = canonical_scene_packages(root)
    references = {(record["scene_id"], record["reconstruction_id"]) for record in packages}
    mrps = metric_reconstruction_packages(root, references)
    package_ids = {str(record["scene_package_id"]) for record in packages}
    return {
        "kind": "spatial_3dgs_artifact_lifecycle_report",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "non_destructive": True,
        "policy": {
            "canonical_viewer_input": "Scene Package",
            "derived_artifacts": ["web_viewer/app/public/scene_package/runtime", "viewer_distribution ZIP"],
            "mrp_lifecycle": "active/archive classification only; no MRP is a deletion candidate without a separate retention decision",
        },
        "canonical_scene_packages": packages,
        "metric_reconstruction_packages": mrps,
        "derived_runtime_candidates": runtime_candidates(root, package_ids),
        "legacy_static_candidates": legacy_static_candidates(root),
        "unreferenced_public_asset_candidates": unreferenced_public_assets(root),
        "legacy_path_references": legacy_path_references(root),
        "summary": {
            "canonical_scene_packages": len(packages),
            "active_mrps": sum(record["lifecycle"] == "active" for record in mrps),
            "archive_mrps": sum(record["lifecycle"] == "archive" for record in mrps),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Report artifact lifecycle candidates without changing data")
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    root = args.root.resolve()
    output = (args.output or root / "data" / "output" / "artifact_lifecycle_report.json").resolve()
    report = build_report(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Artifact lifecycle report: {output}")
    print(json.dumps(report["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
