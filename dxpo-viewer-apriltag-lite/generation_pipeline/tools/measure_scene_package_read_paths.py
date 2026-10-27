"""Measure cached Scene Package listing and distribution preflight paths.

Run from the repository root:

    python3 -m generation_pipeline.tools.measure_scene_package_read_paths

The command is read-only with respect to canonical Scene Packages. It emits a
machine-readable JSON report so Phase 9 measurements use one stable method.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

from generation_pipeline.tools.application_preview_api import PreviewService


def _git(root: Path, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def _duration_ms(operation: Callable[[], Any]) -> tuple[float, Any]:
    started = perf_counter()
    result = operation()
    return (perf_counter() - started) * 1000.0, result


def _summary(values: list[float]) -> dict[str, float]:
    return {
        "median_ms": round(statistics.median(values), 3),
        "minimum_ms": round(min(values), 3),
        "maximum_ms": round(max(values), 3),
    }


def measure(
    root: Path,
    *,
    iterations: int,
    scene_package_id: str | None,
) -> dict[str, Any]:
    listing_service = PreviewService(root)
    cold_listing_ms, packages = _duration_ms(listing_service.scene_packages)
    warm_listing_values = [
        _duration_ms(listing_service.scene_packages)[0]
        for _ in range(iterations)
    ]
    uncached_listing_values: list[float] = []
    for _ in range(iterations):
        listing_service._invalidate_scene_package_caches()
        uncached_listing_values.append(
            _duration_ms(listing_service.scene_packages)[0]
        )
    verified = [
        item
        for item in packages
        if item.get("managed") is True
        and item.get("status") == "verified"
        and isinstance(item.get("scene_package_id"), str)
    ]
    if not verified:
        raise RuntimeError("verified_managed_scene_package_required")
    selected = next(
        (
            item
            for item in verified
            if item["scene_package_id"] == scene_package_id
        ),
        None,
    ) if scene_package_id else verified[0]
    if selected is None:
        raise ValueError(f"scene_package_not_found:{scene_package_id}")

    preflight_service = PreviewService(root)
    # Populate the independently measured listing projection so this workload
    # isolates source estimation and immutable Package sizing.
    preflight_service.scene_packages()
    payload = {
        "package_id": "phase9-measurement-app",
        "label": "Phase 9 measurement",
        "ui_modules": ["simple-viewer"],
        "backend_components": ["scene-manager-api"],
        "scene_package_ids": [selected["scene_package_id"]],
        "viewer_variant": "simple",
    }
    cold_preflight_ms, cold_preflight = _duration_ms(
        lambda: preflight_service.app_package_preflight(payload)
    )
    warm_preflight_values: list[float] = []
    for _ in range(iterations):
        duration_ms, _result = _duration_ms(
            lambda: preflight_service.app_package_preflight(payload)
        )
        warm_preflight_values.append(duration_ms)
    uncached_preflight_values: list[float] = []
    for _ in range(iterations):
        preflight_service._invalidate_scene_package_caches()
        duration_ms, _result = _duration_ms(
            lambda: preflight_service.app_package_preflight(payload)
        )
        uncached_preflight_values.append(duration_ms)

    status = _git(root, "status", "--porcelain")
    return {
        "schema_version": "1.0.0",
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "git_commit": _git(root, "rev-parse", "HEAD"),
            "git_worktree_clean": status == "" if status is not None else None,
            "git_status": status.splitlines() if status else [],
        },
        "workload": {
            "repository_root": str(root),
            "scene_package_count": len(packages),
            "scene_package_id": selected["scene_package_id"],
            "scene_package_bytes": cold_preflight["scene_package_bytes"],
            "warm_iterations": iterations,
        },
        "measurements": {
            "scene_packages": {
                "cold_ms": round(cold_listing_ms, 3),
                "warm": _summary(warm_listing_values),
                "uncached_reference": _summary(uncached_listing_values),
            },
            "app_package_preflight": {
                "cold_ms": round(cold_preflight_ms, 3),
                "warm": _summary(warm_preflight_values),
                "uncached_reference": _summary(uncached_preflight_values),
            },
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--scene-package-id")
    args = parser.parse_args()
    if args.iterations < 1 or args.iterations > 1000:
        parser.error("--iterations must be between 1 and 1000")
    report = measure(
        args.root.resolve(),
        iterations=args.iterations,
        scene_package_id=args.scene_package_id,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
