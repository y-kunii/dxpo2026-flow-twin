#!/usr/bin/env python3
"""Build verified dense/proxy geometry and publish a new Package edition.

This is the supported successor to the retired pipeline stages that modified a
Scene Package in place. It reads only from a self-contained v1.3 Package,
writes all expensive intermediate data below ``var/spatial``, and delegates the
final atomic publication to ``create_proxy_scene_package_edition``.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from generation_pipeline.tools.docker_runtime import (  # noqa: E402
    PINNED_COLMAP_IMAGE,
    docker_workspace_arguments,
)
from generation_pipeline.tools.validate_scene_package import validate_scene_package  # noqa: E402
from packages.proxy_scene_package_edition import create_proxy_scene_package_edition  # noqa: E402
from packages.scene_repository import SceneRepositoryLayout  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _container_path(path: Path, root: Path) -> str:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError as error:
        raise ValueError(f"proxy_build_path_outside_repository:{path}") from error
    return "/workspace/" + relative.as_posix()


def _run(command: list[str], root: Path, step: str, commands: list[dict[str, Any]]) -> None:
    started_at = _now()
    result = subprocess.run(command, cwd=root, text=True, capture_output=True, check=False)
    commands.append({
        "step": step,
        "command": command,
        "started_at": started_at,
        "completed_at": _now(),
        "return_code": result.returncode,
        "stdout_tail": result.stdout[-4000:],
        "stderr_tail": result.stderr[-4000:],
    })
    if result.returncode:
        raise RuntimeError(f"proxy_build_step_failed:{step}:exit_{result.returncode}")


def build_proxy_scene_package_edition(
    root: Path,
    source_scene_package_id: str,
    workspace: Path,
    *,
    gpu: str = "all",
    docker_image: str = PINNED_COLMAP_IMAGE,
) -> dict[str, Any]:
    repository_root = root.resolve()
    layout = SceneRepositoryLayout.from_root(repository_root)
    source = layout.scene_packages / source_scene_package_id
    validation = validate_scene_package(source)
    if validation.get("status") != "verified":
        raise ValueError("source_scene_package_not_verified")
    manifest = json.loads((source / "scene_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "1.3.0":
        raise ValueError("proxy_build_requires_self_contained_v1_3_package")
    if manifest.get("assets", {}).get("proxy_mesh") is not None:
        raise ValueError("source_scene_package_already_has_proxy")
    work = workspace.resolve()
    if not work.is_relative_to(layout.runtime.resolve()):
        raise ValueError("proxy_build_workspace_must_be_below_runtime_root")
    if work.exists():
        raise FileExistsError(f"proxy_build_workspace_exists:{work}")

    limits = json.loads(
        (repository_root / "generation_pipeline/config/acceptance/phase9_limits.json").read_text(
            encoding="utf-8"
        )
    )
    minimum_free = int(limits["minimum_repository_free_bytes"])
    free = shutil.disk_usage(layout.repository).free
    if free < minimum_free:
        raise RuntimeError(f"proxy_build_free_space_below_policy:{free}:{minimum_free}")

    dataset = source / "dataset"
    input_model = dataset / "colmap/text"
    images = dataset / "frames/images"
    masks = dataset / "frames/reconstruction_masks"
    alignment = source / str(manifest["configs"]["world_alignment"])
    config = repository_root / "generation_pipeline/config/proxy_geometry.default.json"
    for required in (input_model, images, masks, alignment, config):
        if not required.exists():
            raise FileNotFoundError(f"proxy_build_input_missing:{required}")

    commands: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "schema_version": "1.0.0",
        "stage": "proxy_scene_package_edition",
        "status": "running",
        "source_scene_package_id": source_scene_package_id,
        "started_at": _now(),
        "workspace": work.relative_to(repository_root).as_posix(),
        "minimum_free_bytes": minimum_free,
        "free_bytes_at_start": free,
        "commands": commands,
    }
    work.mkdir(parents=True)
    report_path = work / "proxy_edition_report.json"

    def save() -> None:
        temporary = report_path.with_name(f".{report_path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, report_path)

    save()
    try:
        dense = work / "dense"
        _run(
            [
                sys.executable,
                str(repository_root / "generation_pipeline/tools/run_colmap_dense_geometry_poc.py"),
                "--repo_root", str(repository_root),
                "--input_model", str(input_model),
                "--image_path", str(images),
                "--mask_path", str(masks),
                "--workspace", str(dense),
                "--world_alignment", str(alignment),
                "--config", str(config),
                "--docker_image", docker_image,
                "--gpu", gpu,
            ],
            repository_root,
            "colmap_dense",
            commands,
        )
        dense_quality = json.loads((dense / "dense_quality.json").read_text(encoding="utf-8"))
        if dense_quality.get("status") != "verified":
            raise RuntimeError("proxy_build_dense_quality_not_verified")
        fused = dense / "fused.ply"
        inner_dense = dense / "dense"
        for suffix in ("", ".vis"):
            target = dense / f"fused.ply{suffix}"
            link = inner_dense / f"fused.ply{suffix}"
            if not target.is_file():
                raise FileNotFoundError(f"proxy_build_dense_artifact_missing:{target}")
            if not link.exists():
                link.symlink_to(os.path.relpath(target, link.parent))

        surface = dense / "surface_mesher.ply"
        _run(
            [
                "docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
                *docker_workspace_arguments(repository_root),
                "--entrypoint", "colmap", docker_image,
                "delaunay_mesher",
                "--input_path", _container_path(inner_dense, repository_root),
                "--input_type", "dense",
                "--output_path", _container_path(surface, repository_root),
            ],
            repository_root,
            "delaunay_mesher",
            commands,
        )
        proxy = work / "proxy"
        _run(
            [
                sys.executable,
                str(repository_root / "generation_pipeline/tools/generate_proxy_geometry.py"),
                "--mode", "surface_mesh",
                "--fused_ply", str(fused),
                "--surface_ply", str(surface),
                "--world_alignment", str(alignment),
                "--config", str(config),
                "--dense_quality", str(dense / "dense_quality.json"),
                "--output_dir", str(proxy),
            ],
            repository_root,
            "verified_surface_proxy",
            commands,
        )
        report["workspace_bytes"] = sum(
            path.stat().st_size for path in work.rglob("*") if path.is_file() and not path.is_symlink()
        )
        maximum_workspace = int(limits["disk_limits_bytes"]["selected_workspace"])
        report["maximum_workspace_bytes"] = maximum_workspace
        if report["workspace_bytes"] > maximum_workspace:
            raise RuntimeError(
                f"proxy_build_workspace_exceeds_policy:{report['workspace_bytes']}:{maximum_workspace}"
            )
        result = create_proxy_scene_package_edition(
            repository_root,
            source_scene_package_id,
            proxy,
        )
        report["status"] = "verified"
        report["result"] = result
        report["completed_at"] = _now()
        save()
        return report
    except Exception as error:
        report["status"] = "failed"
        report["failure_reasons"] = [str(error)]
        report["completed_at"] = _now()
        save()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build dense geometry and publish a verified proxy-enabled Scene Package edition."
    )
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--source-scene-package-id", required=True)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--gpu", default="all")
    parser.add_argument("--docker-image", default=PINNED_COLMAP_IMAGE)
    args = parser.parse_args()
    root = args.repo_root.resolve()
    layout = SceneRepositoryLayout.from_root(root)
    workspace = args.workspace or (
        layout.runtime / "workspaces/proxy-editions" / f"{args.source_scene_package_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    result = build_proxy_scene_package_edition(
        root,
        args.source_scene_package_id,
        workspace,
        gpu=args.gpu,
        docker_image=args.docker_image,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
