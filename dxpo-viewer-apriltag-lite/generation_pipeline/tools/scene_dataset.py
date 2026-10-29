"""Build the self-contained dataset carried by an immutable Scene Package.

The MRP remains the reconstruction contract, but a Scene Package dataset owns
portable copies of every large artifact needed by packaged applications.  No
reader should need a runtime workspace or the canonical MRP tree when this
dataset is present.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageOps

from packages.scene_repository import SceneRepositoryLayout

from services.control_api.field_memory import (
    SCHEMA_MIGRATIONS_SQL,
    SCHEMA_SQL,
    SCHEMA_VERSION,
    SEARCH_INDEX_REBUILD_SQL,
    SEARCH_INDEX_SQL,
)


DATASET_SCHEMA_VERSION = "1.1.0"
PACKAGE_URI_PREFIX = "package://"


def self_contained_package_id(
    reconstruction_id: str,
    asset_hash: str,
    dataset_fingerprint: str,
) -> str:
    identity = hashlib.sha256(
        f"{asset_hash}:{dataset_fingerprint}".encode("ascii")
    ).hexdigest()
    return f"{reconstruction_id}-spzv4-{identity[:12]}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_directory(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(
        candidate for candidate in path.rglob("*") if candidate.is_file()
    ):
        relative = item.relative_to(path).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_file(item).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"scene_dataset_json_not_object:{path}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def make_metric_manifest_portable(path: Path) -> dict[str, Any]:
    """Rewrite copied MRP provenance so consumers never need its build workspace."""

    value = read_json(path)
    provenance = value.setdefault("provenance", {})
    provenance.pop("source_workspace_uri", None)
    provenance["source_workspace_included"] = False
    provenance["portable_package_uri"] = "package://dataset/metric_reconstruction"
    write_json(path, value)
    return value


def portable_scene_config(
    *,
    scene_id: str,
    reconstruction_id: str,
    scene_package_id: str,
    frame_interval_sec: float,
    mask_report: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "kind": "portable_scene_dataset_config",
        "scene_id": scene_id,
        "reconstruction_id": reconstruction_id,
        "scene_package_id": scene_package_id,
        "frame_interval_sec": frame_interval_sec,
        "registered_frames_uri": "package://dataset/frames/images",
        "person_mask": {
            "backend": mask_report.get("backend"),
            "status": mask_report.get("status"),
            "report_uri": "package://dataset/config/person_mask_report.json",
            "person_masks_uri": "package://dataset/frames/person_masks",
            "colmap_masks_uri": "package://dataset/frames/colmap_masks",
        },
        "colmap": {
            "database_uri": "package://dataset/colmap/database.db",
            "text_uri": "package://dataset/colmap/text",
            "sparse_uri": "package://dataset/colmap/sparse",
        },
        "field_memory": {
            "database_uri": "package://dataset/field_memory/field_memory.sqlite3",
        },
    }


def _inside(root: Path, path: Path, label: str) -> Path:
    root = root.resolve()
    path = (path if path.is_absolute() else root / path).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError(f"scene_dataset_{label}_outside_repository:{path}") from error
    return path


def _repo_path(root: Path, uri: object, label: str) -> Path:
    if not isinstance(uri, str) or not uri or uri.startswith(("/", PACKAGE_URI_PREFIX)):
        raise ValueError(f"scene_dataset_{label}_uri_invalid")
    path = _inside(root, SceneRepositoryLayout.from_root(root).resolve_uri(uri), label)
    if not path.exists():
        raise FileNotFoundError(f"scene_dataset_{label}_missing:{uri}")
    return path


def _source_directory(
    root: Path, source_file: Path, label: str
) -> tuple[Path, dict[str, Any]]:
    source = read_json(source_file)
    if source.get("status") != "verified":
        raise ValueError(f"scene_dataset_{label}_source_not_verified")
    path = _repo_path(root, source.get("source_uri"), label)
    if not path.is_dir():
        raise FileNotFoundError(f"scene_dataset_{label}_source_not_directory:{path}")
    return path, source


def _copy_file(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"scene_dataset_source_file_invalid:{source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _copy_tree(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise ValueError(f"scene_dataset_source_directory_invalid:{source}")
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"scene_dataset_symlink_not_allowed:{path}")
    shutil.copytree(source, destination)


def _invert_person_mask_tree(source: Path, destination: Path) -> None:
    """Create the COLMAP retain-mask set: exactly NOT(person)."""

    if source.is_symlink() or not source.is_dir():
        raise ValueError(f"scene_dataset_source_directory_invalid:{source}")
    destination.mkdir(parents=True, exist_ok=False)
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"scene_dataset_symlink_not_allowed:{path}")
        if not path.is_file():
            continue
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(path) as image:
            person = image.convert("L")
            ImageOps.invert(person).save(target)


def _normalize_mask_report(
    root: Path, report: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Recover the pinned model contract omitted by early verified reports."""

    if report.get("backend") != "grounding_dino_sam2":
        return report, False
    model = report.get("model")
    if not isinstance(model, dict):
        raise ValueError("scene_dataset_mask_report_model_missing")
    configuration = report.get("configuration")
    revisions_valid = all(
        isinstance(model.get(field), str) and len(model[field]) == 40
        for field in ("detector_revision", "segmenter_revision")
    )
    if isinstance(configuration, dict) and revisions_valid:
        return report, False

    pinned_path = root / "generation_pipeline/config/person_mask.default.json"
    pinned = read_json(pinned_path)
    pinned_model = pinned.get("model")
    if pinned.get("backend") != "grounding_dino_sam2" or not isinstance(
        pinned_model, dict
    ):
        raise ValueError("scene_dataset_pinned_mask_configuration_invalid")
    for field in ("detector", "segmenter", "runtime_package"):
        if model.get(field) != pinned_model.get(field):
            raise ValueError(
                f"scene_dataset_legacy_mask_model_contract_mismatch:{field}"
            )

    normalized = json.loads(json.dumps(report))
    normalized_model = dict(normalized["model"])
    for field in ("detector_revision", "segmenter_revision"):
        normalized_model[field] = pinned_model[field]
    normalized["model"] = normalized_model
    normalized["configuration"] = pinned
    normalized["configuration_recovery"] = {
        "status": "verified",
        "reason": "legacy_report_omitted_pinned_configuration",
        "source_uri": "generation_pipeline/config/person_mask.default.json",
        "source_sha256": sha256_file(pinned_path),
    }
    return normalized, True


def publish_scene_package_tree(source: Path, destination: Path) -> dict[str, int | str]:
    """Publish an immutable package without duplicating file payloads.

    Canonical and Viewer runtime directories live on the same filesystem in
    the normal installation, so regular files can share their inode.  A
    cross-device or policy failure falls back to ``copy2`` for that file; the
    caller still validates the complete staged runtime before publishing it.
    """

    if source.is_symlink() or not source.is_dir():
        raise ValueError(f"scene_package_publish_source_invalid:{source}")
    if destination.exists():
        raise FileExistsError(f"scene_package_publish_destination_exists:{destination}")
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"scene_package_publish_symlink_not_allowed:{path}")
        if path.is_file():
            path.chmod(path.stat().st_mode & ~0o222)

    linked = 0
    copied = 0

    def link_or_copy(source_file: str, destination_file: str) -> str:
        nonlocal linked, copied
        try:
            os.link(source_file, destination_file)
            linked += 1
        except OSError:
            shutil.copy2(source_file, destination_file)
            copied += 1
        return destination_file

    shutil.copytree(source, destination, copy_function=link_or_copy)
    return {
        "mode": "hardlink" if copied == 0 else "mixed",
        "hardlinked_file_count": linked,
        "copied_file_count": copied,
    }


def _file_count(path: Path) -> int:
    return sum(1 for item in path.rglob("*") if item.is_file())


def _directory_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def related_scene_package_ids(
    root: Path, scene_id: str, reconstruction_id: str
) -> list[str]:
    """Find older immutable snapshots of the same reconstruction.

    Their SceneVersion is the source for a new package-scoped Field Memory
    snapshot.  The target package ID is written into the snapshot, so the
    exported DB never points back to an unbundled package.
    """

    result: list[str] = []
    scene_root = SceneRepositoryLayout.from_root(root).scene_packages
    if not scene_root.is_dir():
        return result
    for manifest_path in scene_root.glob("*/scene_manifest.json"):
        try:
            manifest = read_json(manifest_path)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        package_id = manifest.get("scene_package_id")
        if (
            manifest.get("scene_id") == scene_id
            and manifest.get("reconstruction_id") == reconstruction_id
            and isinstance(package_id, str)
        ):
            result.append(package_id)
    return sorted(set(result))


def _workspace(root: Path, mrp_manifest: dict[str, Any]) -> Path:
    provenance = mrp_manifest.get("provenance")
    uri = (
        provenance.get("source_workspace_uri") if isinstance(provenance, dict) else None
    )
    path = _repo_path(root, uri, "workspace")
    if not path.is_dir():
        raise FileNotFoundError("scene_dataset_workspace_missing")
    return path


def _scene_config(root: Path, workspace: Path, reconstruction_id: str) -> Path | None:
    report_path = workspace / "pipeline_report.json"
    if report_path.is_file():
        report = read_json(report_path)
        value = report.get("scene_config")
        if isinstance(value, str):
            candidate = _repo_path(root, value, "scene_config")
            if candidate.is_file():
                return candidate
    layout = SceneRepositoryLayout.from_root(root)
    for jobs in layout.existing_roots(layout.jobs, layout.legacy_jobs):
        for path in sorted(jobs.glob("*.scene.json")):
            try:
                if read_json(path).get("reconstruction_id") == reconstruction_id:
                    return path
            except (OSError, json.JSONDecodeError, ValueError):
                continue
    return None


def _frame_interval(config_path: Path | None) -> float:
    if config_path is None:
        return 1.0
    config = read_json(config_path)
    preprocessing = config.get("preprocessing")
    value = (
        preprocessing.get("frame_interval_sec")
        if isinstance(preprocessing, dict)
        else None
    )
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and float(value) > 0
    ):
        return float(value)
    return 1.0


def source_fingerprint(
    root: Path,
    mrp_root: Path,
    *,
    person_mask_root: Path | None = None,
    colmap_mask_root: Path | None = None,
    mask_report_path: Path | None = None,
) -> str:
    """Fingerprint all mutable sources that define one dataset edition."""

    digest = hashlib.sha256()
    paths = [
        mrp_root / "manifest.json",
        mrp_root / "images" / ".source.json",
        mrp_root / "masks" / ".source.json",
        mrp_root / "colmap" / "text" / ".source.json",
        mrp_root / "colmap" / "sparse" / ".source.json",
    ]
    manifest = read_json(mrp_root / "manifest.json")
    workspace = _workspace(root, manifest)
    default_mask_root, _ = _source_directory(
        root, mrp_root / "masks" / ".source.json", "masks"
    )
    paths.append(workspace / "colmap_sequential" / "database.db")
    selected_mask_report = mask_report_path or (workspace / "mask_quality.json")
    selected_mask_report = _inside(root, selected_mask_report, "mask_report")
    if selected_mask_report.is_file():
        paths.append(selected_mask_report)
        report = read_json(selected_mask_report)
        if report.get("backend") == "grounding_dino_sam2":
            paths.append(root / "generation_pipeline/config/person_mask.default.json")
    field_db = SceneRepositoryLayout.from_root(root).site_registry
    if field_db.is_file():
        paths.append(field_db)
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(f"scene_dataset_fingerprint_source_missing:{path}")
        digest.update(path.name.encode("utf-8"))
        digest.update(sha256_file(path).encode("ascii"))
    effective_person_masks = person_mask_root or (workspace / "masks_person")
    effective_colmap_masks = colmap_mask_root or effective_person_masks
    for label, path in (
        ("person_masks", effective_person_masks),
        ("colmap_masks", effective_colmap_masks),
    ):
        path = _inside(root, path, label)
        if not path.is_dir():
            raise FileNotFoundError(f"scene_dataset_fingerprint_source_missing:{path}")
        digest.update(label.encode("ascii"))
        digest.update(sha256_directory(path).encode("ascii"))
    return digest.hexdigest()


def estimate_dataset_bytes(
    root: Path,
    mrp_root: Path,
    *,
    person_mask_root: Path | None = None,
    colmap_mask_root: Path | None = None,
) -> int:
    manifest = read_json(mrp_root / "manifest.json")
    workspace = _workspace(root, manifest)
    image_root, _ = _source_directory(
        root, mrp_root / "images" / ".source.json", "images"
    )
    default_mask_root, _ = _source_directory(
        root, mrp_root / "masks" / ".source.json", "masks"
    )
    colmap_mask_root = (
        _inside(root, colmap_mask_root, "colmap_masks")
        if colmap_mask_root
        else person_mask_root or (workspace / "masks_person")
    )
    if person_mask_root is None:
        candidate = workspace / "masks_person"
        if not candidate.is_dir():
            raise FileNotFoundError(f"scene_dataset_person_masks_missing:{candidate}")
        person_mask_root = candidate
    person_mask_root = _inside(root, person_mask_root, "person_masks")
    text_root, _ = _source_directory(
        root, mrp_root / "colmap" / "text" / ".source.json", "colmap_text"
    )
    sparse_root, _ = _source_directory(
        root, mrp_root / "colmap" / "sparse" / ".source.json", "colmap_sparse"
    )
    paths = tuple(
        dict.fromkeys(
            path.resolve()
            for path in (
                image_root,
                person_mask_root,
                colmap_mask_root,
                default_mask_root,
                text_root,
                sparse_root,
            )
        )
    )
    total = sum(_directory_bytes(path) for path in paths)
    total += (workspace / "colmap_sequential" / "database.db").stat().st_size
    total += _directory_bytes(mrp_root)
    field_root = SceneRepositoryLayout.from_root(root).site_records
    if field_root.is_dir():
        total += _directory_bytes(field_root)
    return total


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _insert_rows(
    target: sqlite3.Connection,
    table: str,
    rows: Iterable[sqlite3.Row],
    *,
    replacements: dict[str, Any] | None = None,
) -> int:
    count = 0
    for row in rows:
        value = dict(row)
        value.update(replacements or {})
        columns = list(value)
        marks = ",".join("?" for _ in columns)
        target.execute(
            f"INSERT OR IGNORE INTO {table} ({','.join(columns)}) VALUES ({marks})",
            [value[column] for column in columns],
        )
        count += 1
    return count


def _select_in(
    connection: sqlite3.Connection,
    table: str,
    field: str,
    values: list[str],
) -> list[sqlite3.Row]:
    if not values:
        return []
    marks = ",".join("?" for _ in values)
    return list(
        connection.execute(f"SELECT * FROM {table} WHERE {field} IN ({marks})", values)
    )


def _empty_field_memory(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA_SQL)
    connection.executescript(SEARCH_INDEX_SQL)
    connection.executescript(SCHEMA_MIGRATIONS_SQL)
    migration_checksum = hashlib.sha256(
        (SCHEMA_SQL + SEARCH_INDEX_SQL + SEARCH_INDEX_REBUILD_SQL).encode("utf-8")
    ).hexdigest()
    connection.execute(
        "INSERT INTO schema_migrations "
        "(migration_id, from_version, to_version, migration_sha256, applied_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            f"scene-dataset-snapshot-v{SCHEMA_VERSION}",
            0,
            SCHEMA_VERSION,
            migration_checksum,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return connection


def snapshot_field_memory(
    root: Path,
    output_root: Path,
    source_package_ids: list[str],
    target_package_id: str,
    *,
    fork_scene_version: bool = False,
) -> dict[str, Any]:
    """Export the newest matching SceneVersion and its site-scoped records."""

    output_db = output_root / "field_memory.sqlite3"
    target = _empty_field_memory(output_db)
    counts: dict[str, int] = {}
    content_hashes: set[str] = set()
    repository_layout = SceneRepositoryLayout.from_root(root)
    source_db = repository_layout.site_registry
    selected_scene_version_id: str | None = None
    try:
        if source_db.is_file() and source_package_ids:
            source = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
            source.row_factory = sqlite3.Row
            try:
                available = _tables(source)
                marks = ",".join("?" for _ in source_package_ids)
                scene = source.execute(
                    f"SELECT * FROM scene_versions WHERE scene_package_id IN ({marks}) "
                    "ORDER BY created_at DESC LIMIT 1",
                    source_package_ids,
                ).fetchone()
                if scene is not None:
                    selected_scene_version_id = str(scene["scene_version_id"])
                    target_scene_version_id = (
                        "scene_pkg_"
                        + hashlib.sha256(target_package_id.encode("utf-8")).hexdigest()[
                            :32
                        ]
                        if fork_scene_version
                        else selected_scene_version_id
                    )
                    site_id = str(scene["site_id"])
                    site = source.execute(
                        "SELECT * FROM sites WHERE site_id = ?", (site_id,)
                    ).fetchone()
                    if site is None:
                        raise ValueError("scene_dataset_field_memory_site_missing")
                    counts["sites"] = _insert_rows(target, "sites", [site])
                    counts["scene_versions"] = _insert_rows(
                        target,
                        "scene_versions",
                        [scene],
                        replacements={
                            "scene_version_id": target_scene_version_id,
                            "scene_package_id": target_package_id,
                            **(
                                {
                                    "label": f"{scene['label']} / person-mask edition",
                                    "created_at": datetime.now(
                                        timezone.utc
                                    ).isoformat(),
                                }
                                if fork_scene_version
                                else {}
                            ),
                        },
                    )

                    asset_rows = (
                        list(
                            source.execute(
                                "SELECT * FROM assets WHERE site_id = ?", (site_id,)
                            )
                        )
                        if "assets" in available
                        else []
                    )
                    asset_ids = [str(row["asset_id"]) for row in asset_rows]
                    counts["assets"] = _insert_rows(target, "assets", asset_rows)
                    observations = (
                        []
                        if fork_scene_version
                        else list(
                            source.execute(
                                "SELECT * FROM asset_observations WHERE scene_version_id = ?",
                                (selected_scene_version_id,),
                            )
                        )
                        if "asset_observations" in available
                        else []
                    )
                    counts["asset_observations"] = _insert_rows(
                        target, "asset_observations", observations
                    )
                    documents = (
                        _select_in(source, "asset_documents", "asset_id", asset_ids)
                        if "asset_documents" in available
                        else []
                    )
                    document_ids = [str(row["document_id"]) for row in documents]
                    counts["asset_documents"] = _insert_rows(
                        target, "asset_documents", documents
                    )
                    maintenance = (
                        _select_in(source, "maintenance_records", "asset_id", asset_ids)
                        if "maintenance_records" in available
                        else []
                    )
                    counts["maintenance_records"] = _insert_rows(
                        target, "maintenance_records", maintenance
                    )
                    features = (
                        _select_in(
                            source, "image_features", "document_id", document_ids
                        )
                        if "image_features" in available
                        else []
                    )
                    counts["image_features"] = _insert_rows(
                        target, "image_features", features
                    )

                    captures = (
                        []
                        if fork_scene_version
                        else list(
                            source.execute(
                                "SELECT * FROM captures WHERE scene_version_id = ? OR (site_id = ? AND scene_version_id IS NULL)",
                                (selected_scene_version_id, site_id),
                            )
                        )
                        if "captures" in available
                        else []
                    )
                    counts["captures"] = _insert_rows(target, "captures", captures)
                    jobs = (
                        []
                        if fork_scene_version
                        else list(
                            source.execute(
                                "SELECT * FROM image_index_jobs WHERE site_id = ?",
                                (site_id,),
                            )
                        )
                        if "image_index_jobs" in available
                        else []
                    )
                    counts["image_index_jobs"] = _insert_rows(
                        target, "image_index_jobs", jobs
                    )

                    inquiries = (
                        []
                        if fork_scene_version
                        else list(
                            source.execute(
                                "SELECT * FROM evidence_inquiries WHERE site_id = ?",
                                (site_id,),
                            )
                        )
                        if "evidence_inquiries" in available
                        else []
                    )
                    inquiry_ids = [str(row["inquiry_id"]) for row in inquiries]
                    counts["evidence_inquiries"] = _insert_rows(
                        target, "evidence_inquiries", inquiries
                    )
                    citations = (
                        _select_in(
                            source, "evidence_citations", "inquiry_id", inquiry_ids
                        )
                        if "evidence_citations" in available
                        else []
                    )
                    counts["evidence_citations"] = _insert_rows(
                        target, "evidence_citations", citations
                    )

                    runs = (
                        []
                        if fork_scene_version
                        else list(
                            source.execute(
                                "SELECT * FROM analysis_runs WHERE scene_version_id = ?",
                                (selected_scene_version_id,),
                            )
                        )
                        if "analysis_runs" in available
                        else []
                    )
                    run_ids = [str(row["analysis_run_id"]) for row in runs]
                    counts["analysis_runs"] = _insert_rows(
                        target, "analysis_runs", runs
                    )
                    contracts = (
                        _select_in(
                            source, "analysis_run_contracts", "analysis_run_id", run_ids
                        )
                        if "analysis_run_contracts" in available
                        else []
                    )
                    counts["analysis_run_contracts"] = _insert_rows(
                        target, "analysis_run_contracts", contracts
                    )
                    frames = (
                        _select_in(source, "frame_analyses", "analysis_run_id", run_ids)
                        if "frame_analyses" in available
                        else []
                    )
                    counts["frame_analyses"] = _insert_rows(
                        target, "frame_analyses", frames
                    )
                    frame_ids = [str(row["frame_analysis_id"]) for row in frames]
                    frame_reviews = []
                    if "frame_analysis_review_events" in available:
                        frame_reviews = sorted(
                            _select_in(
                                source,
                                "frame_analysis_review_events",
                                "frame_analysis_id",
                                frame_ids,
                            ),
                            key=lambda row: (
                                str(row["created_at"]),
                                str(row["review_event_id"]),
                            ),
                        )
                    counts["frame_analysis_review_events"] = _insert_rows(
                        target, "frame_analysis_review_events", frame_reviews
                    )
                    observation_sets = (
                        _select_in(
                            source,
                            "frame_observation_sets",
                            "frame_analysis_id",
                            frame_ids,
                        )
                        if "frame_observation_sets" in available
                        else []
                    )
                    observation_set_ids = [
                        str(row["observation_set_id"]) for row in observation_sets
                    ]
                    counts["frame_observation_sets"] = _insert_rows(
                        target, "frame_observation_sets", observation_sets
                    )
                    observation_items = (
                        _select_in(
                            source,
                            "frame_observation_items",
                            "observation_set_id",
                            observation_set_ids,
                        )
                        if "frame_observation_items" in available
                        else []
                    )
                    counts["frame_observation_items"] = _insert_rows(
                        target, "frame_observation_items", observation_items
                    )
                    activations = (
                        []
                        if fork_scene_version
                        else list(
                            source.execute(
                                "SELECT * FROM scene_analysis_activations WHERE scene_version_id = ?",
                                (selected_scene_version_id,),
                            )
                        )
                        if "scene_analysis_activations" in available
                        else []
                    )
                    counts["scene_analysis_activations"] = _insert_rows(
                        target, "scene_analysis_activations", activations
                    )
                    activation_events = (
                        []
                        if fork_scene_version
                        else list(
                            source.execute(
                                "SELECT * FROM scene_analysis_activation_events WHERE scene_version_id = ?",
                                (selected_scene_version_id,),
                            )
                        )
                        if "scene_analysis_activation_events" in available
                        else []
                    )
                    counts["scene_analysis_activation_events"] = _insert_rows(
                        target, "scene_analysis_activation_events", activation_events
                    )

                    candidates = (
                        []
                        if fork_scene_version
                        else list(
                            source.execute(
                                "SELECT * FROM change_candidates WHERE from_scene_version_id = ? AND to_scene_version_id = ?",
                                (selected_scene_version_id, selected_scene_version_id),
                            )
                        )
                        if "change_candidates" in available
                        else []
                    )
                    candidate_ids = [
                        str(row["change_candidate_id"]) for row in candidates
                    ]
                    counts["change_candidates"] = _insert_rows(
                        target, "change_candidates", candidates
                    )
                    reviews = (
                        _select_in(
                            source,
                            "change_review_events",
                            "change_candidate_id",
                            candidate_ids,
                        )
                        if "change_review_events" in available
                        else []
                    )
                    counts["change_review_events"] = _insert_rows(
                        target, "change_review_events", reviews
                    )
                    confirmed = (
                        _select_in(
                            source,
                            "confirmed_change_events",
                            "change_candidate_id",
                            candidate_ids,
                        )
                        if "confirmed_change_events" in available
                        else []
                    )
                    counts["confirmed_change_events"] = _insert_rows(
                        target, "confirmed_change_events", confirmed
                    )
                    audits = (
                        []
                        if fork_scene_version
                        else list(
                            source.execute(
                                "SELECT * FROM audit_events WHERE site_id = ?",
                                (site_id,),
                            )
                        )
                        if "audit_events" in available
                        else []
                    )
                    counts["audit_events"] = _insert_rows(
                        target, "audit_events", audits
                    )
            finally:
                source.close()

        target.executescript(SEARCH_INDEX_REBUILD_SQL)
        target.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        target.commit()

        for row in target.execute(
            "SELECT image_content_sha256, vlm_input_content_sha256, thumbnail_content_sha256 FROM frame_analyses"
        ):
            content_hashes.update(str(value) for value in row if value)
        for row in target.execute("SELECT content_sha256, uri FROM asset_documents"):
            if row[0]:
                content_hashes.add(str(row[0]))
            uri = str(row[1])
            if uri.startswith("content://sha256/"):
                content_hashes.add(uri.rsplit("/", 1)[-1])
        for row in target.execute("SELECT source_content_sha256 FROM captures"):
            if row[0]:
                content_hashes.add(str(row[0]))
    finally:
        target.close()

    content_root = output_root / "content" / "sha256"
    source_content = repository_layout.site_attachments
    files: list[dict[str, Any]] = []
    for digest in sorted(content_hashes):
        source = source_content / digest[:2] / digest
        if not source.is_file() or sha256_file(source) != digest:
            raise FileNotFoundError(
                f"scene_dataset_field_memory_content_missing:{digest}"
            )
        destination = content_root / digest[:2] / digest
        _copy_file(source, destination)
        files.append({"sha256": digest, "size": destination.stat().st_size})
    content_manifest = {
        "schema_version": "1.0.0",
        "kind": "field_memory_content_manifest",
        "file_count": len(files),
        "files": files,
    }
    content_manifest_path = output_root / "content_manifest.json"
    write_json(content_manifest_path, content_manifest)
    return {
        "database_sha256": sha256_file(output_db),
        "database_size": output_db.stat().st_size,
        "content_manifest_sha256": sha256_file(content_manifest_path),
        "content_file_count": len(files),
        "scene_version_id": (
            "scene_pkg_"
            + hashlib.sha256(target_package_id.encode("utf-8")).hexdigest()[:32]
            if selected_scene_version_id is not None and fork_scene_version
            else selected_scene_version_id
        ),
        "record_counts": counts,
    }


def _rewrite_source(path: Path, package_uri: str) -> None:
    value = read_json(path)
    value["source_uri"] = package_uri
    value["portable"] = True
    write_json(path, value)


def materialize_scene_dataset(
    root: Path,
    mrp_root: Path,
    output: Path,
    *,
    scene_id: str,
    reconstruction_id: str,
    scene_package_id: str,
    source_package_ids: list[str] | None = None,
    person_mask_root: Path | None = None,
    colmap_mask_root: Path | None = None,
    mask_report_path: Path | None = None,
) -> dict[str, Any]:
    """Copy all application-facing reconstruction data into ``output``."""

    root = root.resolve()
    mrp_root = _inside(root, mrp_root, "mrp")
    if output.exists():
        raise FileExistsError(f"scene_dataset_output_exists:{output}")
    mrp_manifest = read_json(mrp_root / "manifest.json")
    if (
        mrp_manifest.get("scene_id") != scene_id
        or mrp_manifest.get("reconstruction_id") != reconstruction_id
    ):
        raise ValueError("scene_dataset_mrp_identity_mismatch")
    workspace = _workspace(root, mrp_manifest)
    image_source, image_reference = _source_directory(
        root, mrp_root / "images" / ".source.json", "images"
    )
    default_mask_source, mask_reference = _source_directory(
        root, mrp_root / "masks" / ".source.json", "masks"
    )
    if person_mask_root is None:
        candidate = workspace / "masks_person"
        if not candidate.is_dir():
            raise FileNotFoundError(f"scene_dataset_person_masks_missing:{candidate}")
        person_mask_source = candidate
    else:
        person_mask_source = _inside(root, person_mask_root, "person_masks")
    if not person_mask_source.is_dir():
        raise FileNotFoundError(
            f"scene_dataset_person_masks_missing:{person_mask_source}"
        )
    regenerate_colmap_masks = colmap_mask_root is None
    colmap_mask_source = (
        _inside(root, colmap_mask_root, "colmap_masks")
        if colmap_mask_root is not None
        else person_mask_source
    )
    if not colmap_mask_source.is_dir():
        raise FileNotFoundError(
            f"scene_dataset_colmap_masks_missing:{colmap_mask_source}"
        )
    selected_mask_report = mask_report_path or (workspace / "mask_quality.json")
    selected_mask_report = _inside(root, selected_mask_report, "mask_report")
    if not selected_mask_report.is_file():
        raise FileNotFoundError(
            f"scene_dataset_mask_report_missing:{selected_mask_report}"
        )
    mask_report = read_json(selected_mask_report)
    if mask_report.get("status") != "verified":
        raise ValueError("scene_dataset_mask_report_not_verified")
    mask_report, mask_report_normalized = _normalize_mask_report(root, mask_report)
    person_mask_hash = sha256_directory(person_mask_source)
    colmap_mask_hash = (
        sha256_directory(colmap_mask_source) if not regenerate_colmap_masks else ""
    )
    image_source_hash = sha256_directory(image_source)
    reconstruction_mask_source = default_mask_source
    reconstruction_mask_hash = sha256_directory(reconstruction_mask_source)
    mask_edition_changed = regenerate_colmap_masks or (
        colmap_mask_source.resolve() != reconstruction_mask_source.resolve()
    )
    text_source, text_reference = _source_directory(
        root, mrp_root / "colmap" / "text" / ".source.json", "colmap_text"
    )
    sparse_source, sparse_reference = _source_directory(
        root, mrp_root / "colmap" / "sparse" / ".source.json", "colmap_sparse"
    )
    database_source = workspace / "colmap_sequential" / "database.db"
    if not database_source.is_file():
        raise FileNotFoundError("scene_dataset_colmap_database_missing")

    try:
        _copy_tree(mrp_root, output / "metric_reconstruction")
        _copy_tree(image_source, output / "frames" / "images")
        _copy_tree(person_mask_source, output / "frames" / "person_masks")
        if regenerate_colmap_masks:
            _invert_person_mask_tree(
                person_mask_source, output / "frames" / "colmap_masks"
            )
            colmap_mask_hash = sha256_directory(output / "frames" / "colmap_masks")
        else:
            _copy_tree(colmap_mask_source, output / "frames" / "colmap_masks")
        if mask_edition_changed:
            _copy_tree(
                reconstruction_mask_source, output / "frames" / "reconstruction_masks"
            )
        _copy_tree(text_source, output / "colmap" / "text")
        _copy_tree(sparse_source, output / "colmap" / "sparse")
        _copy_file(database_source, output / "colmap" / "database.db")

        config_path = _scene_config(root, workspace, reconstruction_id)
        frame_interval_sec = _frame_interval(config_path)
        write_json(
            output / "config" / "scene.json",
            portable_scene_config(
                scene_id=scene_id,
                reconstruction_id=reconstruction_id,
                scene_package_id=scene_package_id,
                frame_interval_sec=frame_interval_sec,
                mask_report=mask_report,
            ),
        )
        input_manifest = workspace / "input_manifest.json"
        if input_manifest.is_file():
            _copy_file(input_manifest, output / "config" / "input_manifest.json")
        packaged_mask_report = output / "config" / "person_mask_report.json"
        mask_report["outputs"] = {
            "person_masks": "package://dataset/frames/person_masks",
            "colmap_masks": "package://dataset/frames/colmap_masks",
        }
        recovery = mask_report.get("configuration_recovery")
        if isinstance(recovery, dict):
            recovery["source_uri"] = "embedded://person_mask_report.configuration"
        write_json(packaged_mask_report, mask_report)

        portable_mrp = output / "metric_reconstruction"
        _rewrite_source(
            portable_mrp / "images" / ".source.json", "package://dataset/frames/images"
        )
        _rewrite_source(
            portable_mrp / "masks" / ".source.json",
            (
                "package://dataset/frames/reconstruction_masks"
                if mask_edition_changed
                else "package://dataset/frames/colmap_masks"
            ),
        )
        _rewrite_source(
            portable_mrp / "colmap" / "text" / ".source.json",
            "package://dataset/colmap/text",
        )
        _rewrite_source(
            portable_mrp / "colmap" / "sparse" / ".source.json",
            "package://dataset/colmap/sparse",
        )
        # lfs_input is represented by the same registered frames and horizontalized
        # model without copying an additional 2+ GiB RGBA image tree.
        _rewrite_source(
            portable_mrp / "lfs_input" / ".source.json", "package://dataset"
        )
        make_metric_manifest_portable(portable_mrp / "manifest.json")

        field = snapshot_field_memory(
            root,
            output / "field_memory",
            source_package_ids
            or related_scene_package_ids(root, scene_id, reconstruction_id),
            scene_package_id,
            fork_scene_version=mask_edition_changed,
        )
        database_hash = sha256_file(output / "colmap" / "database.db")
        dataset = {
            "schema_version": DATASET_SCHEMA_VERSION,
            "kind": "spatial_scene_dataset",
            "status": "verified",
            "scene_id": scene_id,
            "reconstruction_id": reconstruction_id,
            "scene_package_id": scene_package_id,
            "frame_interval_sec": frame_interval_sec,
            "artifacts": {
                "metric_reconstruction": {
                    "uri": "metric_reconstruction",
                    "manifest_uri": "metric_reconstruction/manifest.json",
                    "manifest_sha256": sha256_file(portable_mrp / "manifest.json"),
                },
                "registered_frames": {
                    "uri": "frames/images",
                    "manifest_uri": "metric_reconstruction/images/manifest.json",
                    "file_count": _file_count(output / "frames" / "images"),
                    "source_sha256": image_source_hash,
                    "original_source_sha256": image_reference.get("source_sha256"),
                },
                "person_masks": {
                    "uri": "frames/person_masks",
                    "file_count": _file_count(output / "frames" / "person_masks"),
                    "source_sha256": person_mask_hash,
                    "convention": "white_is_person",
                },
                "colmap_masks": {
                    "uri": "frames/colmap_masks",
                    "file_count": _file_count(output / "frames" / "colmap_masks"),
                    "source_sha256": colmap_mask_hash,
                    "original_source_sha256": mask_reference.get("source_sha256"),
                    "convention": "white_is_retained",
                    "applied_to_packaged_colmap_database": not mask_edition_changed,
                },
                "reconstruction_masks": {
                    "uri": (
                        "frames/reconstruction_masks"
                        if mask_edition_changed
                        else "frames/colmap_masks"
                    ),
                    "file_count": _file_count(
                        output
                        / "frames"
                        / (
                            "reconstruction_masks"
                            if mask_edition_changed
                            else "colmap_masks"
                        )
                    ),
                    "manifest_uri": "metric_reconstruction/masks/manifest.json",
                    "source_sha256": reconstruction_mask_hash,
                    "convention": "mask_set_used_by_packaged_colmap_database",
                },
                "mask_generation": {
                    "report_uri": "config/person_mask_report.json",
                    "report_sha256": sha256_file(
                        output / "config" / "person_mask_report.json"
                    ),
                    "backend": mask_report.get("backend"),
                    "model": mask_report.get("model"),
                    "status": mask_report.get("status"),
                },
                "colmap": {
                    "database_uri": "colmap/database.db",
                    "database_sha256": database_hash,
                    "text_uri": "colmap/text",
                    "text_file_count": _file_count(output / "colmap" / "text"),
                    "text_sha256": text_reference.get("source_sha256"),
                    "sparse_uri": "colmap/sparse",
                    "sparse_file_count": _file_count(output / "colmap" / "sparse"),
                    "sparse_sha256": sparse_reference.get("source_sha256"),
                },
                "field_memory": {
                    "database_uri": "field_memory/field_memory.sqlite3",
                    "database_sha256": field["database_sha256"],
                    "content_uri": "field_memory/content/sha256",
                    "content_manifest_uri": "field_memory/content_manifest.json",
                    "content_manifest_sha256": field["content_manifest_sha256"],
                    "content_file_count": field["content_file_count"],
                    "scene_version_id": field["scene_version_id"],
                    "record_counts": field["record_counts"],
                },
            },
            "provenance": {
                "source_workspace_included": False,
                "portable_package_uri": "package://dataset",
                "source_package_ids": source_package_ids
                or related_scene_package_ids(root, scene_id, reconstruction_id),
                "source_video_included": False,
                "lfs_training_output_included": False,
            },
            "distribution_contract": {
                "portable": True,
                "host_workspace_required": False,
                "uri_scheme": "package://",
            },
        }
        write_json(output / "manifest.json", dataset)
        return dataset
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise


def materialize_to_temporary(
    root: Path,
    mrp_root: Path,
    *,
    scene_id: str,
    reconstruction_id: str,
    scene_package_id: str,
    source_package_ids: list[str] | None = None,
) -> tuple[tempfile.TemporaryDirectory[str], Path, dict[str, Any]]:
    temporary = tempfile.TemporaryDirectory(prefix="spatial-scene-dataset-")
    output = Path(temporary.name) / "dataset"
    value = materialize_scene_dataset(
        root,
        mrp_root,
        output,
        scene_id=scene_id,
        reconstruction_id=reconstruction_id,
        scene_package_id=scene_package_id,
        source_package_ids=source_package_ids,
    )
    return temporary, output, value
