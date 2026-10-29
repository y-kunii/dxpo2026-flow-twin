"""Reproducible runtime release and build provenance contracts."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
from pathlib import Path
from typing import Any


RUNTIME_CONTRACT_PATH = Path(__file__).resolve().parent / "runtime_release.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def application_runtime_contract() -> dict[str, Any]:
    value = json.loads(RUNTIME_CONTRACT_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("version"), str):
        raise RuntimeError("application_runtime_contract_invalid")
    return value


def git_source_state(root: Path) -> dict[str, Any]:
    try:
        revision = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
            check=True, capture_output=True, text=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "dirty": True, "dirty_path_count": None}
    return {"commit": revision or None, "dirty": bool(status), "dirty_path_count": len(status)}


def require_formal_build_source(root: Path, lifecycle: str) -> dict[str, Any]:
    state = git_source_state(root)
    if lifecycle in {"candidate", "qualified", "released"}:
        if state["commit"] is None:
            raise ValueError("formal_product_build_requires_git_revision")
        if state["dirty"]:
            raise ValueError("formal_product_build_requires_clean_worktree")
    return state


def build_provenance(root: Path, recipe: dict[str, Any]) -> dict[str, Any]:
    lock_candidates = [
        "web_viewer/app/package-lock.json",
        "generation_pipeline/runtime_environment.lock.json",
        "generation_pipeline/requirements-core.txt",
        "generation_pipeline/requirements-gpu.txt",
        "generation_pipeline/requirements-test.txt",
        "docker-compose.yml",
        "services/vlm_model_catalog.py",
    ]
    locks = []
    for relative in lock_candidates:
        path = root / relative
        if path.is_file() and not path.is_symlink():
            locks.append({"uri": relative, "sha256": _sha256(path), "size_bytes": path.stat().st_size})
    runtime = application_runtime_contract()
    source = require_formal_build_source(root, str(recipe.get("release_lifecycle", "draft")))
    profile_id = recipe.get("product_profile_id")
    profile_digest = None
    if isinstance(profile_id, str):
        profile_path = Path(__file__).resolve().parent / "profiles" / f"{profile_id}.json"
        profile_digest = _sha256(profile_path)
    return {
        "schema_version": "1.0.0",
        "source": source,
        "runtime": runtime,
        "runtime_contract_sha256": _sha256(RUNTIME_CONTRACT_PATH),
        "product_profile_sha256": profile_digest,
        "recipe_sha256": _json_digest(recipe),
        "component_lock_sha256": _json_digest({
            "ui_modules": recipe.get("ui_modules", []),
            "backend_components": recipe.get("backend_components", []),
            "viewer_variant": recipe.get("viewer_variant"),
        }),
        "dependency_locks": locks,
        "toolchain": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "target_os": os.environ.get("SPATIAL_TARGET_OS", "linux"),
            "target_architecture": os.environ.get("SPATIAL_TARGET_ARCH", platform.machine()),
        },
        "ci": {
            "provider": "github-actions" if os.environ.get("GITHUB_ACTIONS") == "true" else "local",
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "workflow": os.environ.get("GITHUB_WORKFLOW"),
        },
        "oci_images": {"included": False, "digests": [], "reason": "connected_or_source_build"},
    }
