#!/usr/bin/env python3
"""Build and verify a real frame-analysis distribution in a clean directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from create_app_distribution import create_custom_app_distribution  # noqa: E402
from packages.scene_repository import SceneRepositoryLayout  # noqa: E402


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    root = destination.resolve()
    for member in archive.infolist():
        target = (destination / member.filename).resolve()
        if target != root and root not in target.parents:
            raise RuntimeError("distribution_archive_member_outside_root")
    archive.extractall(destination)


def verify_checksums(bundle: Path) -> int:
    lines = (bundle / "checksums.sha256").read_text(encoding="utf-8").splitlines()
    for line in lines:
        expected, relative = line.split("  ", 1)
        path = bundle / relative
        if not path.is_file() or sha256(path) != expected:
            raise RuntimeError(f"distribution_checksum_mismatch:{relative}")
    return len(lines)


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def docker_smoke(bundle: Path) -> dict[str, Any]:
    if shutil.which("docker") is None:
        raise RuntimeError("docker_not_available")
    port = available_port()
    project = f"spatial-acceptance-{uuid.uuid4().hex[:10]}"
    environment = {**os.environ, "APP_PORT": str(port), "COMPOSE_PROJECT_NAME": project}
    try:
        subprocess.run(
            ["docker", "compose", "config", "--quiet"],
            cwd=bundle,
            env=environment,
            check=True,
            timeout=60,
        )
        subprocess.run(
            ["docker", "compose", "up", "-d", "--build", "application-api", "app"],
            cwd=bundle,
            env=environment,
            check=True,
            timeout=1800,
        )
        deadline = time.monotonic() + 120
        last_error = ""
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/", timeout=3
                ) as response:
                    if response.status == 200:
                        return {
                            "status": "verified",
                            "port": port,
                            "compose_project": project,
                        }
            except Exception as error:  # noqa: BLE001 - retain last startup diagnostic
                last_error = str(error)
            time.sleep(2)
        raise RuntimeError(f"distribution_docker_start_timeout:{last_error}")
    finally:
        subprocess.run(
            ["docker", "compose", "down", "--volumes", "--remove-orphans"],
            cwd=bundle,
            env=environment,
            check=False,
            timeout=180,
        )


def accept(
    root: Path, package_id: str, output: Path, *, run_docker: bool
) -> dict[str, Any]:
    layout = SceneRepositoryLayout.from_root(root)
    source_package = layout.scene_packages / package_id
    source_manifest = source_package / "scene_manifest.json"
    if not source_manifest.is_file():
        raise FileNotFoundError(f"scene_package_not_found:{package_id}")
    source_hash_before = sha256(source_manifest)
    recipe = {
        "package_id": "frame-analysis-acceptance",
        "label": "登録画像AI解析 受入試験",
        "ui_modules": ["frame-analysis"],
        "backend_components": ["frame-analysis-api", "search-api", "frame-vlm-worker"],
        "scene_package_ids": [package_id],
        "viewer_variant": "full",
        "include_active_analysis": True,
    }

    def progress(stage: str, current: int, total: int, message: str) -> None:
        print(f"[{stage}] {current}/{total} {message}", flush=True)

    build = create_custom_app_distribution(
        root, recipe, output, progress_callback=progress
    )
    with tempfile.TemporaryDirectory(
        prefix="spatial-distribution-clean-room-"
    ) as extracted_value:
        extracted = Path(extracted_value)
        with zipfile.ZipFile(output) as archive:
            safe_extract(archive, extracted)
        roots = [path for path in extracted.iterdir() if path.is_dir()]
        if len(roots) != 1:
            raise RuntimeError("distribution_archive_root_invalid")
        bundle = roots[0]
        checksum_count = verify_checksums(bundle)
        manifest = json.loads(
            (bundle / "app_package_manifest.json").read_text(encoding="utf-8")
        )
        overlay = manifest.get("analysis_overlay")
        if not isinstance(overlay, dict) or overlay.get("included") is not True:
            raise RuntimeError("distribution_active_analysis_overlay_missing")
        state = bundle / "state" / "site-records"
        database = state / "registry.sqlite3"
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("distribution_overlay_database_invalid")
            run_count = int(
                connection.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0]
            )
            frame_count = int(
                connection.execute("SELECT COUNT(*) FROM frame_analyses").fetchone()[0]
            )
            review_event_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM frame_analysis_review_events"
                ).fetchone()[0]
            )
            run_contract_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM analysis_run_contracts"
                ).fetchone()[0]
            )
            observation_set_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM frame_observation_sets"
                ).fetchone()[0]
            )
            observation_item_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM frame_observation_items"
                ).fetchone()[0]
            )
            attachment_hashes = {
                str(value).lower()
                for row in connection.execute(
                    "SELECT image_content_sha256, vlm_input_content_sha256, thumbnail_content_sha256 FROM frame_analyses"
                )
                for value in row
            }
        finally:
            connection.close()
        declared_frames = sum(
            int(item["frame_count"]) for item in overlay.get("runs", [])
        )
        declared_reviews = sum(
            int(item.get("review_event_count", 0)) for item in overlay.get("runs", [])
        )
        declared_contracts = sum(
            int(item.get("run_contract_count", 0)) for item in overlay.get("runs", [])
        )
        declared_sets = sum(
            int(item.get("observation_set_count", 0))
            for item in overlay.get("runs", [])
        )
        declared_items = sum(
            int(item.get("observation_item_count", 0))
            for item in overlay.get("runs", [])
        )
        if frame_count != declared_frames:
            raise RuntimeError(
                f"distribution_overlay_frame_count_mismatch:{frame_count}:{declared_frames}"
            )
        if review_event_count != declared_reviews:
            raise RuntimeError(
                "distribution_overlay_review_count_mismatch:"
                f"{review_event_count}:{declared_reviews}"
            )
        if run_contract_count != declared_contracts:
            raise RuntimeError(
                "distribution_overlay_contract_count_mismatch:"
                f"{run_contract_count}:{declared_contracts}"
            )
        if observation_set_count != declared_sets:
            raise RuntimeError(
                "distribution_overlay_observation_set_count_mismatch:"
                f"{observation_set_count}:{declared_sets}"
            )
        if observation_item_count != declared_items:
            raise RuntimeError(
                "distribution_overlay_observation_item_count_mismatch:"
                f"{observation_item_count}:{declared_items}"
            )
        for content_sha256 in attachment_hashes:
            attachment = (
                state / "attachments" / "sha256" / content_sha256[:2] / content_sha256
            )
            if not attachment.is_file() or sha256(attachment) != content_sha256:
                raise RuntimeError(
                    f"distribution_overlay_attachment_invalid:{content_sha256}"
                )
        bundled_manifest = (
            bundle
            / "web_viewer"
            / "app"
            / "public"
            / "scene_package"
            / "runtime"
            / package_id
            / "scene_manifest.json"
        )
        if (
            sha256(bundled_manifest) != source_hash_before
            or sha256(source_manifest) != source_hash_before
        ):
            raise RuntimeError("scene_package_immutability_check_failed")
        docker = docker_smoke(bundle) if run_docker else {"status": "not_requested"}
        report = {
            "schema_version": "1.0.0",
            "status": "verified",
            "scene_package_id": package_id,
            "source_scene_manifest_sha256": source_hash_before,
            "zip": {
                "path": str(output),
                "size_bytes": output.stat().st_size,
                "sha256": sha256(output),
                "verified_file_count": checksum_count,
            },
            "analysis_overlay": {
                "run_count": run_count,
                "frame_count": frame_count,
                "review_event_count": review_event_count,
                "attachment_count": len(attachment_hashes),
                "attachment_bytes": int(overlay.get("attachment_bytes", 0)),
                "state_uri": "state/site-records",
            },
            "docker_smoke": docker,
            "build": build,
        }
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--scene-package", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--docker-smoke", action="store_true")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    if args.output is not None:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        report = accept(
            args.repo_root.resolve(),
            args.scene_package,
            output,
            run_docker=args.docker_smoke,
        )
    else:
        with tempfile.TemporaryDirectory(
            prefix="spatial-distribution-acceptance-"
        ) as temporary:
            output = Path(temporary) / "frame-analysis-acceptance.zip"
            report = accept(
                args.repo_root.resolve(),
                args.scene_package,
                output,
                run_docker=args.docker_smoke,
            )
            if args.keep:
                raise ValueError("--keep requires --output")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
