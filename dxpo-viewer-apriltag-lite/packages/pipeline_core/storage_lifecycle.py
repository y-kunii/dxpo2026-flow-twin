"""Bounded cleanup for regenerable Scene Repository runtime storage only.

Canonical MRP, SPZ, Scene Package, source assets, and Viewer runtime paths are
intentionally not accepted by this module. A workspace becomes eligible only
after a verified self-contained Scene Package has preserved its dataset and
the publication has aged past the configured retention period. Cleanup is
always previewed from a closed set of roots before execution.
"""

from __future__ import annotations

import os
import json
import re
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from packages.scene_repository import SceneRepositoryLayout


CONTENT_HASH = re.compile(r"^[0-9a-f]{64}$")


def _positive_integer(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive integer") from error
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True)
class RetentionPolicy:
    upload_retention_days: int
    package_retention_days: int
    staging_retention_hours: int
    maximum_packages: int
    cache_max_bytes: int = 100 * 1024 * 1024 * 1024
    trash_retention_days: int = 14
    workspace_retention_days: int = 14

    @classmethod
    def from_environment(cls) -> "RetentionPolicy":
        return cls(
            upload_retention_days=_positive_integer("APP_PREVIEW_UPLOAD_RETENTION_DAYS", 30),
            package_retention_days=_positive_integer("APP_PREVIEW_PACKAGE_RETENTION_DAYS", 14),
            staging_retention_hours=_positive_integer("APP_PREVIEW_STAGING_RETENTION_HOURS", 24),
            maximum_packages=_positive_integer("APP_PREVIEW_MAX_DERIVED_PACKAGES", 20),
            cache_max_bytes=_positive_integer("SPATIAL_CACHE_MAX_BYTES", 100 * 1024 * 1024 * 1024),
            trash_retention_days=_positive_integer("SPATIAL_TRASH_RETENTION_DAYS", 14),
            workspace_retention_days=_positive_integer("SPATIAL_WORKSPACE_RETENTION_DAYS", 14),
        )


@dataclass(frozen=True)
class CleanupCandidate:
    category: str
    path: str
    size_bytes: int
    reason: str
    modified_at_epoch: float


class StorageLifecycle:
    """Inventory and remove only allowlisted, regenerable storage."""

    def __init__(self, root: Path, policy: RetentionPolicy | None = None) -> None:
        self.root = root.resolve()
        self.policy = policy or RetentionPolicy.from_environment()
        layout = SceneRepositoryLayout.from_root(self.root)
        self.source_content_root = layout.sources
        self.upload_content_root = self.source_content_root  # compatibility attribute; never auto-deleted
        self.upload_staging_root = layout.upload_staging
        self.cache_root = layout.cache
        self.trash_root = layout.scene_package_trash
        self.repository_cleanup_trash_root = layout.repository_cleanup_trash
        self.projects_root = layout.projects
        self.legacy_workspaces_root = layout.legacy_workspaces
        self.jobs_root = layout.jobs
        self.scene_packages_root = layout.scene_packages
        self.layout = layout
        self.app_packages_root = layout.application_deliverables
        self.allowed_roots = (
            self.upload_staging_root.resolve(),
            self.cache_root.resolve(),
            self.trash_root.resolve(),
            self.repository_cleanup_trash_root.resolve(),
            self.projects_root.resolve(),
            self.legacy_workspaces_root.resolve(),
            self.app_packages_root.resolve(),
        )

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _workspace_root_for_uri(self, uri: str) -> Path | None:
        source = self.layout.resolve_uri(uri)
        for workspace_root in (self.projects_root, self.legacy_workspaces_root):
            try:
                relative = source.relative_to(workspace_root.resolve())
            except ValueError:
                continue
            if not relative.parts:
                return None
            candidate = workspace_root / relative.parts[0]
            if candidate.is_dir() and not candidate.is_symlink():
                return candidate
        return None

    def _published_workspaces(self) -> dict[Path, float]:
        """Return workspaces protected by a verified, package-local dataset."""

        result: dict[Path, float] = {}
        if not self.scene_packages_root.is_dir():
            return result
        for package in self.scene_packages_root.iterdir():
            if not package.is_dir() or package.is_symlink():
                continue
            manifest = self._read_json(package / "scene_manifest.json")
            dataset = self._read_json(package / "dataset/manifest.json")
            if manifest.get("schema_version") != "1.3.0" or dataset.get("status") != "verified":
                continue
            provenance = dataset.get("provenance")
            source_uri = provenance.get("source_workspace_uri") if isinstance(provenance, dict) else None
            if not isinstance(source_uri, str):
                continue
            workspace = self._workspace_root_for_uri(source_uri)
            if workspace is None:
                continue
            try:
                published_at = package.stat().st_mtime
            except OSError:
                continue
            result[workspace] = max(result.get(workspace, 0.0), published_at)
        return result

    def _active_workspaces(self) -> set[Path]:
        result: set[Path] = set()
        if not self.jobs_root.is_dir():
            return result
        for job_path in self.jobs_root.glob("*.json"):
            if job_path.name.endswith(".scene.json"):
                continue
            job = self._read_json(job_path)
            if job.get("status") not in {"queued", "running", "cancelling"}:
                continue
            workspace_uri = job.get("workspace_uri") or job.get("workspace")
            if isinstance(workspace_uri, str):
                workspace = self._workspace_root_for_uri(workspace_uri)
                if workspace is not None:
                    result.add(workspace)
        return result

    @staticmethod
    def _size(path: Path) -> int:
        if path.is_symlink():
            return 0
        if path.is_file():
            return path.stat().st_size
        total = 0
        if path.is_dir():
            for item in path.rglob("*"):
                if item.is_file() and not item.is_symlink():
                    try:
                        total += item.stat().st_size
                    except OSError:
                        continue
        return total

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _is_allowed(self, path: Path) -> bool:
        if path.is_symlink():
            return False
        resolved = path.resolve()
        return any(root in resolved.parents for root in self.allowed_roots)

    def _candidate(self, path: Path, category: str, reason: str) -> CleanupCandidate | None:
        if not path.exists() or not self._is_allowed(path):
            return None
        try:
            modified = path.stat().st_mtime
            return CleanupCandidate(category, self._relative(path), self._size(path), reason, modified)
        except OSError:
            return None

    def candidates(self, now: float | None = None) -> list[CleanupCandidate]:
        timestamp = time.time() if now is None else now
        result: list[CleanupCandidate] = []

        workspace_cutoff = timestamp - self.policy.workspace_retention_days * 86400
        active_workspaces = self._active_workspaces()
        for path, published_at in self._published_workspaces().items():
            if path in active_workspaces or published_at >= workspace_cutoff:
                continue
            try:
                workspace_is_old = path.stat().st_mtime < workspace_cutoff
            except OSError:
                continue
            if workspace_is_old:
                candidate = self._candidate(
                    path,
                    "published_workspace",
                    "verified_package_workspace_retention_expired",
                )
                if candidate:
                    result.append(candidate)

        staging_cutoff = timestamp - self.policy.staging_retention_hours * 3600
        if self.upload_staging_root.is_dir():
            for path in self.upload_staging_root.iterdir():
                try:
                    expired = path.stat().st_mtime < staging_cutoff
                except OSError:
                    continue
                if expired:
                    candidate = self._candidate(path, "upload_staging", "staging_expired")
                    if candidate:
                        result.append(candidate)

        trash_cutoff = timestamp - self.policy.trash_retention_days * 86400
        for trash_root, category in (
            (self.trash_root, "scene_package_trash"),
            (self.repository_cleanup_trash_root, "repository_cleanup_trash"),
        ):
            if not trash_root.is_dir():
                continue
            for path in trash_root.iterdir():
                try:
                    expired = path.stat().st_mtime < trash_cutoff
                except OSError:
                    continue
                if expired:
                    candidate = self._candidate(path, category, "trash_retention_expired")
                    if candidate:
                        result.append(candidate)

        if self.cache_root.is_dir():
            cache_entries = sorted(
                (path for path in self.cache_root.iterdir() if not path.is_symlink()),
                key=lambda path: path.stat().st_mtime,
            )
            cache_bytes = sum(self._size(path) for path in cache_entries)
            for path in cache_entries:
                if cache_bytes <= self.policy.cache_max_bytes:
                    break
                candidate = self._candidate(path, "runtime_cache", "cache_capacity_exceeded")
                if candidate:
                    result.append(candidate)
                    cache_bytes -= candidate.size_bytes

        package_cutoff = timestamp - self.policy.package_retention_days * 86400
        packages = []
        if self.app_packages_root.is_dir():
            packages = sorted(
                (
                    path for path in self.app_packages_root.glob("*.zip")
                    if path.is_file() and not path.is_symlink()
                ),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        selected_paths: set[Path] = set()
        for index, path in enumerate(packages):
            reason = None
            if index >= self.policy.maximum_packages:
                reason = "package_count_exceeded"
            elif path.stat().st_mtime < package_cutoff:
                reason = "retention_expired"
            if reason and path not in selected_paths:
                candidate = self._candidate(path, "application_distribution", reason)
                if candidate:
                    result.append(candidate)
                    selected_paths.add(path)

        return sorted(result, key=lambda item: (item.category, item.path))

    def report(self, *, execute: bool = False) -> dict[str, Any]:
        candidates = self.candidates()
        removed: list[dict[str, Any]] = []
        failures: list[str] = []
        if execute:
            for candidate in candidates:
                path = self.root / candidate.path
                if not self._is_allowed(path):
                    failures.append(f"cleanup_target_rejected:{candidate.path}")
                    continue
                try:
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                    removed.append(asdict(candidate))
                except OSError as error:
                    failures.append(f"cleanup_failed:{candidate.path}:{error}")
        return {
            "status": "failed" if failures else "verified",
            "dry_run": not execute,
            "policy": asdict(self.policy),
            "candidate_count": len(candidates),
            "candidate_bytes": sum(item.size_bytes for item in candidates),
            "candidates": [asdict(item) for item in candidates],
            "removed_count": len(removed),
            "removed_bytes": sum(int(item["size_bytes"]) for item in removed),
            "removed": removed,
            "failure_reasons": failures,
        }
