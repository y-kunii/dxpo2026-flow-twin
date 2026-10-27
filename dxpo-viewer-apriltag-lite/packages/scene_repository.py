"""Canonical layout and rebuildable catalog for the local Scene Repository.

Durable artifacts live below ``data/scene_repository`` and are grouped by
meaning: packages, source assets, registries, deliverables, and indexes.
Mutable execution state lives below ``var/spatial``. Old flat ``data`` paths
are accepted only as read fallbacks while an installation is being migrated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterable


CATALOG_SCHEMA_VERSION = "1.0.0"
ACTIVE_JOB_STATUSES = frozenset({"queued", "running", "cancelling"})


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _repo_relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


@dataclass(frozen=True)
class SceneRepositoryLayout:
    """All repository paths used by package applications and the pipeline."""

    root: Path

    @classmethod
    def from_root(cls, root: Path) -> "SceneRepositoryLayout":
        return cls(root.resolve())

    @property
    def repository(self) -> Path:
        return self.root / "data/scene_repository"

    @property
    def index(self) -> Path:
        return self.repository / "index"

    @property
    def catalog(self) -> Path:
        return self.index / "catalog.sqlite3"

    @property
    def packages(self) -> Path:
        return self.repository / "packages"

    @property
    def scene_packages(self) -> Path:
        return self.packages / "scenes"

    @property
    def metric_reconstructions(self) -> Path:
        return self.packages / "metric_reconstructions"

    @property
    def source_files(self) -> Path:
        return self.repository / "assets/source_files"

    @property
    def sources(self) -> Path:
        return self.source_files / "sha256"

    @property
    def registries(self) -> Path:
        return self.repository / "registries"

    @property
    def site_records(self) -> Path:
        return self.registries / "site_records"

    @property
    def site_registry(self) -> Path:
        return self.site_records / "registry.sqlite3"

    @property
    def site_attachments(self) -> Path:
        return self.site_records / "attachments/sha256"

    @property
    def deliverables(self) -> Path:
        return self.repository / "deliverables"

    @property
    def application_deliverables(self) -> Path:
        return self.deliverables / "applications"

    @property
    def content_packs(self) -> Path:
        """Content-addressed, application-independent Scene/overlay packs."""

        return self.deliverables / "content-packs/sha256"

    @property
    def product_releases(self) -> Path:
        """Small release descriptors that reference runtime and content artifacts."""

        return self.deliverables / "releases"

    @property
    def acceptance_records(self) -> Path:
        return self.deliverables / "acceptance"

    @property
    def viewer_deliverables(self) -> Path:
        return self.deliverables / "viewers"

    @property
    def reports(self) -> Path:
        return self.deliverables / "reports"

    @property
    def runtime(self) -> Path:
        configured = os.environ.get("SPATIAL_RUNTIME_ROOT")
        if not configured:
            return self.root / "var/spatial"
        candidate = Path(configured)
        resolved = candidate.resolve() if candidate.is_absolute() else (self.root / candidate).resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("SPATIAL_RUNTIME_ROOT must resolve inside the repository root")
        return resolved

    @property
    def projects(self) -> Path:
        return self.runtime / "workspaces/projects"

    @property
    def legacy_workspaces(self) -> Path:
        return self.runtime / "workspaces/legacy"

    @property
    def jobs(self) -> Path:
        return self.runtime / "jobs"

    @property
    def logs(self) -> Path:
        return self.runtime / "logs"

    @property
    def upload_staging(self) -> Path:
        return self.runtime / "staging/uploads"

    @property
    def site_record_staging(self) -> Path:
        return self.runtime / "staging/site_records"

    @property
    def imports(self) -> Path:
        return self.runtime / "staging/imports"

    @property
    def locks(self) -> Path:
        return self.runtime / "locks"

    @property
    def secrets(self) -> Path:
        return self.runtime / "secrets"

    @property
    def cache(self) -> Path:
        return self.runtime / "cache"

    @property
    def site_record_indexes(self) -> Path:
        return self.cache / "site_records"

    @property
    def container_home(self) -> Path:
        return self.runtime / "home"

    @property
    def scene_package_trash(self) -> Path:
        return self.runtime / "trash/scene_packages"

    @property
    def repository_cleanup_trash(self) -> Path:
        """Recoverable quarantine for user-selected repository cleanup."""

        return self.runtime / "trash/repository"

    @property
    def migrations(self) -> Path:
        return self.runtime / "migrations"

    @property
    def legacy_work(self) -> Path:
        return self.root / "data/work"

    @property
    def legacy_catalog(self) -> Path:
        return self.root / "data/catalog.sqlite3"

    @property
    def legacy_scene_packages(self) -> Path:
        return self.root / "data/scene_packages"

    @property
    def legacy_metric_reconstructions(self) -> Path:
        return self.root / "data/metric_reconstruction_packages"

    @property
    def legacy_source_files(self) -> Path:
        return self.root / "data/sources"

    @property
    def legacy_site_records(self) -> Path:
        return self.root / "data/field_memory"

    @property
    def legacy_deliverables(self) -> Path:
        return self.root / "data/output"

    @property
    def legacy_projects(self) -> Path:
        return self.legacy_work / "application_preview/projects"

    @property
    def legacy_jobs(self) -> Path:
        return self.legacy_work / "application_preview/jobs"

    @property
    def legacy_sources(self) -> Path:
        return self.legacy_work / "application_preview/uploads/content"

    def ensure_runtime_roots(self) -> None:
        for path in (
            self.sources,
            self.index,
            self.scene_packages,
            self.metric_reconstructions,
            self.site_records,
            self.site_attachments,
            self.application_deliverables,
            self.content_packs,
            self.product_releases,
            self.acceptance_records,
            self.viewer_deliverables,
            self.reports,
            self.projects,
            self.legacy_workspaces,
            self.jobs,
            self.logs,
            self.upload_staging,
            self.site_record_staging,
            self.imports,
            self.locks,
            self.locks / "imports",
            self.secrets,
            self.cache,
            self.cache / "models",
            self.site_record_indexes,
            self.container_home,
            self.scene_package_trash,
            self.repository_cleanup_trash,
            self.migrations,
        ):
            path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def existing_roots(primary: Path, legacy: Path) -> tuple[Path, ...]:
        roots = [path for path in (primary, legacy) if path.is_dir()]
        return tuple(dict.fromkeys(path.resolve() for path in roots))

    def rewrite_legacy_uri(self, uri: str) -> str:
        """Map a known legacy URI to its final repository location."""

        normalized = uri.replace("\\", "/").lstrip("./")
        prefixes = (
            ("data/scene_packages/", "data/scene_repository/packages/scenes/"),
            ("data/metric_reconstruction_packages/", "data/scene_repository/packages/metric_reconstructions/"),
            ("data/sources/sha256/", "data/scene_repository/assets/source_files/sha256/"),
            ("data/field_memory/field_memory.sqlite3", "data/scene_repository/registries/site_records/registry.sqlite3"),
            ("data/field_memory/registry.sqlite3", "data/scene_repository/registries/site_records/registry.sqlite3"),
            ("data/field_memory/content/sha256/", "data/scene_repository/registries/site_records/attachments/sha256/"),
            ("data/field_memory/derived/", "var/spatial/cache/site_records/"),
            ("data/field_memory/staging/", "var/spatial/staging/site_records/"),
            ("data/field_memory/", "data/scene_repository/registries/site_records/"),
            ("data/output/app_distribution/", "data/scene_repository/deliverables/applications/"),
            ("data/output/viewer_distribution/", "data/scene_repository/deliverables/viewers/"),
            ("data/output/", "data/scene_repository/deliverables/reports/"),
            ("data/work/application_preview/projects/", "var/spatial/workspaces/projects/"),
            ("data/work/application_preview/jobs/", "var/spatial/jobs/"),
            ("data/work/application_preview/uploads/content/", "data/scene_repository/assets/source_files/sha256/"),
            ("data/work/application_preview/uploads/.staging/", "var/spatial/staging/uploads/"),
            ("data/work/application_preview/imports/", "var/spatial/staging/imports/"),
            ("data/work/application_preview/deleted_scene_packages/", "var/spatial/trash/scene_packages/"),
            ("data/work/application_preview/locks/", "var/spatial/locks/"),
            ("data/work/application_preview/.secrets/", "var/spatial/secrets/"),
            ("data/work/.model_cache/", "var/spatial/cache/models/"),
            ("data/work/.container_home/", "var/spatial/home/"),
        )
        for old, new in prefixes:
            if normalized.startswith(old):
                return new + normalized[len(old):]
        if normalized.startswith("data/work/"):
            return "var/spatial/workspaces/legacy/" + normalized[len("data/work/"):]
        if normalized == "data/catalog.sqlite3":
            return "data/scene_repository/index/catalog.sqlite3"
        return normalized

    def resolve_uri(self, uri: str) -> Path:
        """Resolve a new URI, falling back to the existing legacy object."""

        normalized = uri.replace("\\", "/").lstrip("./")
        rewritten = self.rewrite_legacy_uri(normalized)
        primary = (self.root / rewritten).resolve()
        if primary.exists():
            return primary
        if normalized.startswith("data/input/") and self.sources.is_dir():
            for metadata_path in self.sources.glob("*/upload.json"):
                metadata = _read_json(metadata_path)
                legacy_paths = metadata.get("legacy_paths")
                if not isinstance(legacy_paths, list) or normalized not in legacy_paths:
                    continue
                assets = [path for path in metadata_path.parent.glob("asset.*") if path.is_file()]
                if len(assets) == 1:
                    return assets[0].resolve()
        return (self.root / normalized).resolve()


def _unique_directories(*roots: Path) -> Iterable[Path]:
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.iterdir()):
            if not path.is_dir() or path.is_symlink() or path.name.startswith("."):
                continue
            if path.name in seen:
                continue
            seen.add(path.name)
            yield path


def _create_catalog_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE repository_metadata (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        CREATE TABLE projects (
          project_id TEXT PRIMARY KEY,
          workspace_uri TEXT NOT NULL,
          storage_class TEXT NOT NULL,
          pipeline_status TEXT,
          updated_at TEXT
        );
        CREATE TABLE jobs (
          job_id TEXT PRIMARY KEY,
          project_id TEXT,
          status TEXT,
          job_uri TEXT NOT NULL,
          scene_config_uri TEXT,
          reconstruction_id TEXT,
          updated_at TEXT,
          FOREIGN KEY(project_id) REFERENCES projects(project_id)
        );
        CREATE TABLE metric_reconstructions (
          scene_id TEXT NOT NULL,
          reconstruction_id TEXT NOT NULL,
          package_uri TEXT NOT NULL,
          source_workspace_uri TEXT,
          status TEXT,
          created_at TEXT,
          PRIMARY KEY(scene_id, reconstruction_id)
        );
        CREATE TABLE scene_packages (
          scene_package_id TEXT PRIMARY KEY,
          scene_id TEXT NOT NULL,
          reconstruction_id TEXT NOT NULL,
          package_uri TEXT NOT NULL,
          schema_version TEXT NOT NULL,
          status TEXT,
          self_contained INTEGER NOT NULL,
          source_workspace_uri TEXT,
          created_at TEXT
        );
        CREATE TABLE scene_package_sources (
          scene_package_id TEXT NOT NULL,
          source_scene_package_id TEXT NOT NULL,
          PRIMARY KEY(scene_package_id, source_scene_package_id),
          FOREIGN KEY(scene_package_id) REFERENCES scene_packages(scene_package_id)
        );
        CREATE TABLE source_assets (
          source_id TEXT PRIMARY KEY,
          sha256 TEXT NOT NULL,
          source_uri TEXT NOT NULL,
          original_filename TEXT,
          size_bytes INTEGER,
          retention_policy TEXT NOT NULL,
          stored_at TEXT
        );
        CREATE INDEX jobs_project_idx ON jobs(project_id);
        CREATE INDEX reconstructions_identity_idx
          ON metric_reconstructions(scene_id, reconstruction_id);
        CREATE INDEX scene_packages_reconstruction_idx
          ON scene_packages(scene_id, reconstruction_id);
        """
    )


def rebuild_catalog(root: Path) -> dict[str, Any]:
    """Atomically rebuild the repository index from canonical manifests."""

    layout = SceneRepositoryLayout.from_root(root)
    layout.catalog.parent.mkdir(parents=True, exist_ok=True)
    temporary = layout.catalog.with_name(f".{layout.catalog.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    counts = {
        "projects": 0,
        "jobs": 0,
        "metric_reconstructions": 0,
        "scene_packages": 0,
        "source_assets": 0,
    }
    try:
        with sqlite3.connect(temporary) as connection:
            _create_catalog_schema(connection)
            generated_at = datetime.now(timezone.utc).isoformat()
            connection.executemany(
                "INSERT INTO repository_metadata(key, value) VALUES (?, ?)",
                (
                    ("schema_version", CATALOG_SCHEMA_VERSION),
                    ("generated_at", generated_at),
                    ("authority", "rebuildable_index; manifests_are_canonical"),
                ),
            )

            for project in _unique_directories(layout.projects, layout.legacy_projects):
                reconstruction = project / "reconstruction"
                report = _read_json(reconstruction / "pipeline_report.json")
                connection.execute(
                    "INSERT INTO projects VALUES (?, ?, ?, ?, ?)",
                    (
                        project.name,
                        _repo_relative(layout.root, project),
                        "runtime_workspace",
                        report.get("status"),
                        report.get("finished_at") or report.get("started_at"),
                    ),
                )
                counts["projects"] += 1

            for jobs_root in layout.existing_roots(layout.jobs, layout.legacy_jobs):
                for job_path in sorted(jobs_root.glob("*.json")):
                    if job_path.name.endswith(".scene.json"):
                        continue
                    job = _read_json(job_path)
                    if not job:
                        continue
                    job_id = str(job.get("job_id") or job_path.stem)
                    workspace_uri = job.get("workspace_uri") or job.get("workspace")
                    project_id = None
                    if isinstance(workspace_uri, str):
                        parts = workspace_uri.replace("\\", "/").split("/")
                        if "projects" in parts and parts.index("projects") + 1 < len(parts):
                            project_id = parts[parts.index("projects") + 1]
                    if project_id and not connection.execute(
                        "SELECT 1 FROM projects WHERE project_id = ?", (project_id,)
                    ).fetchone():
                        project_id = None
                    scene_config = job_path.with_name(f"{job_id}.scene.json")
                    connection.execute(
                        "INSERT OR IGNORE INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            job_id,
                            project_id,
                            job.get("status"),
                            _repo_relative(layout.root, job_path),
                            _repo_relative(layout.root, scene_config) if scene_config.is_file() else None,
                            job.get("reconstruction_id"),
                            job.get("finished_at") or job.get("started_at") or job.get("created_at"),
                        ),
                    )
                    counts["jobs"] += 1

            metric_root = (
                layout.metric_reconstructions
                if layout.metric_reconstructions.is_dir()
                else layout.legacy_metric_reconstructions
            )
            if metric_root.is_dir():
                for scene in sorted(metric_root.iterdir()):
                    if not scene.is_dir() or scene.is_symlink():
                        continue
                    for reconstruction in sorted(scene.iterdir()):
                        manifest = _read_json(reconstruction / "manifest.json")
                        if not manifest:
                            continue
                        provenance = manifest.get("provenance")
                        source_workspace = (
                            provenance.get("source_workspace_uri")
                            if isinstance(provenance, dict) else None
                        )
                        connection.execute(
                            "INSERT INTO metric_reconstructions VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                manifest.get("scene_id") or scene.name,
                                manifest.get("reconstruction_id") or reconstruction.name,
                                _repo_relative(layout.root, reconstruction),
                                source_workspace,
                                manifest.get("status") or "published",
                                manifest.get("created_at"),
                            ),
                        )
                        counts["metric_reconstructions"] += 1

            for package in _unique_directories(
                layout.scene_packages, layout.legacy_scene_packages
            ):
                if not package.is_dir() or package.is_symlink():
                    continue
                manifest = _read_json(package / "scene_manifest.json")
                package_id = manifest.get("scene_package_id")
                if not isinstance(package_id, str):
                    continue
                dataset = _read_json(package / "dataset/manifest.json")
                provenance = dataset.get("provenance") if dataset else manifest.get("provenance")
                source_workspace = (
                    provenance.get("source_workspace_uri")
                    if isinstance(provenance, dict) else None
                )
                self_contained = manifest.get("schema_version") == "1.3.0" and bool(dataset)
                connection.execute(
                    "INSERT INTO scene_packages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        package_id,
                        manifest.get("scene_id"),
                        manifest.get("reconstruction_id"),
                        _repo_relative(layout.root, package),
                        manifest.get("schema_version"),
                        dataset.get("status") or manifest.get("status") or "registered",
                        int(self_contained),
                        source_workspace,
                        manifest.get("created_at") or dataset.get("created_at"),
                    ),
                )
                counts["scene_packages"] += 1
                source_ids = provenance.get("source_package_ids") if isinstance(provenance, dict) else None
                if isinstance(source_ids, list):
                    connection.executemany(
                        "INSERT OR IGNORE INTO scene_package_sources VALUES (?, ?)",
                        ((package_id, value) for value in source_ids if isinstance(value, str)),
                    )

            for source in _unique_directories(
                layout.sources,
                layout.legacy_source_files / "sha256",
                layout.legacy_sources,
            ):
                metadata = _read_json(source / "upload.json")
                asset = next((path for path in source.glob("asset.*") if path.is_file()), None)
                sha256 = metadata.get("sha256") or source.name
                if asset is None or not isinstance(sha256, str) or len(sha256) != 64:
                    continue
                connection.execute(
                    "INSERT OR IGNORE INTO source_assets VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        sha256,
                        sha256,
                        _repo_relative(layout.root, asset),
                        metadata.get("original_filename"),
                        asset.stat().st_size,
                        "retain_until_explicit_release",
                        metadata.get("stored_at"),
                    ),
                )
                counts["source_assets"] += 1
            connection.commit()
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("scene_repository_catalog_integrity_check_failed")
        os.replace(temporary, layout.catalog)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "status": "verified",
        "schema_version": CATALOG_SCHEMA_VERSION,
        "catalog_uri": _repo_relative(layout.root, layout.catalog),
        "counts": counts,
    }


def catalog_status(root: Path) -> dict[str, Any]:
    layout = SceneRepositoryLayout.from_root(root)
    if not layout.catalog.is_file():
        return {
            "status": "missing",
            "catalog_uri": "data/scene_repository/index/catalog.sqlite3",
        }
    try:
        with sqlite3.connect(f"file:{layout.catalog}?mode=ro", uri=True) as connection:
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            metadata = dict(connection.execute("SELECT key, value FROM repository_metadata"))
            counts = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("projects", "jobs", "metric_reconstructions", "scene_packages", "source_assets")
            }
    except (OSError, sqlite3.Error) as error:
        return {"status": "failed", "failure_reasons": [str(error)]}
    return {
        "status": "verified" if integrity == "ok" else "failed",
        "catalog_uri": "data/scene_repository/index/catalog.sqlite3",
        "metadata": metadata,
        "counts": counts,
    }


__all__ = [
    "ACTIVE_JOB_STATUSES",
    "CATALOG_SCHEMA_VERSION",
    "SceneRepositoryLayout",
    "catalog_status",
    "rebuild_catalog",
]
