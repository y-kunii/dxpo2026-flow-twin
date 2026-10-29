#!/usr/bin/env python3
"""Replace a verified runtime copy with a verified hardlink publication."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "generation_pipeline" / "tools"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TOOLS))

from scene_dataset import publish_scene_package_tree, read_json  # noqa: E402
from validate_scene_package import validate_scene_package  # noqa: E402
from packages.scene_repository import SceneRepositoryLayout  # noqa: E402


def deduplicate(root: Path, scene_package_id: str) -> dict[str, object]:
    root = root.resolve()
    canonical = SceneRepositoryLayout.from_root(root).scene_packages / scene_package_id
    runtime = (
        root / "web_viewer" / "app" / "public" / "scene_package" / "runtime" /
        scene_package_id
    )
    if not canonical.is_dir() or canonical.is_symlink():
        raise FileNotFoundError(f"canonical_scene_package_missing:{scene_package_id}")
    if not runtime.is_dir() or runtime.is_symlink():
        raise FileNotFoundError(f"runtime_scene_package_missing:{scene_package_id}")
    manifest = read_json(canonical / "scene_manifest.json")
    scene_id = manifest.get("scene_id")
    if manifest.get("scene_package_id") != scene_package_id or not isinstance(scene_id, str):
        raise ValueError("canonical_scene_package_identity_invalid")
    for label, package in (("canonical", canonical), ("runtime", runtime)):
        validation = validate_scene_package(package, scene_id)
        if validation.get("status") != "verified":
            raise ValueError(f"{label}_scene_package_not_verified")
        other_manifest = read_json(package / "scene_manifest.json")
        if other_manifest != manifest:
            raise ValueError("canonical_runtime_manifest_mismatch")

    token = uuid.uuid4().hex
    staging = runtime.with_name(f".{scene_package_id}.hardlink.{token}")
    previous = runtime.with_name(f".{scene_package_id}.previous.{token}")
    publication = publish_scene_package_tree(canonical, staging)
    try:
        validation = validate_scene_package(staging, scene_id)
        if validation.get("status") != "verified":
            raise ValueError("hardlink_runtime_validation_failed")
        os.replace(runtime, previous)
        try:
            os.replace(staging, runtime)
        except BaseException:
            os.replace(previous, runtime)
            raise
        shutil.rmtree(previous)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    canonical_files = {
        path.relative_to(canonical): path
        for path in canonical.rglob("*")
        if path.is_file()
    }
    shared_bytes = 0
    for relative, source in canonical_files.items():
        published = runtime / relative
        if published.is_file() and source.stat().st_ino == published.stat().st_ino:
            shared_bytes += source.stat().st_size
    return {
        "status": "verified",
        "scene_package_id": scene_package_id,
        "publication_storage": publication,
        "shared_payload_bytes": shared_bytes,
        "file_count": len(canonical_files),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--scene-package", required=True)
    args = parser.parse_args()
    try:
        result = deduplicate(args.repo_root, args.scene_package)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "failed", "failure_reasons": [str(error)]}, indent=2))
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
