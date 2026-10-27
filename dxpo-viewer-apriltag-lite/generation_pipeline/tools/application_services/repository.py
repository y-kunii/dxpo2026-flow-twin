"""Scene Repository inventory, lifecycle, and safe cleanup service extracted from the PreviewService compatibility facade."""

from __future__ import annotations

from .common import *  # noqa: F403


class RepositoryService:
    @staticmethod
    def _path_state_signature(path: Path) -> tuple[str, int | None, int | None, int | None]:
        """Return the bounded filesystem state used by metadata caches."""

        try:
            stat = path.stat()
        except OSError:
            return str(path), None, None, None
        return str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino

    def _invalidate_scene_package_caches(self) -> None:
        """Drop derived listing and sizing data after a repository mutation."""

        with self._scene_package_cache_lock:
            self._scene_packages_cache_signature = None
            self._scene_packages_cache_dependencies = ()
            self._scene_packages_cache = None
            self._scene_package_size_cache.clear()

    def _refresh_repository_catalog(self) -> list[str]:
        self._invalidate_scene_package_caches()
        try:
            with self._catalog_lock:
                rebuild_catalog(self.root)
        except (OSError, sqlite3.Error, ValueError):
            return ["scene_repository_catalog_refresh_failed"]
        return []

    def repository_status(self, *, rebuild: bool = False) -> dict[str, Any]:
        """Expose the repository catalog and migration boundary to all apps."""

        if rebuild or not self.repository_layout.catalog.is_file():
            rebuild_catalog(self.root)
        status = catalog_status(self.root)
        status["layout"] = {
            "repository": self.repository_layout.repository.relative_to(self.root).as_posix(),
            "catalog": self.repository_layout.catalog.relative_to(self.root).as_posix(),
            "sources": self.repository_layout.sources.relative_to(self.root).as_posix(),
            "scene_packages": self.repository_layout.scene_packages.relative_to(self.root).as_posix(),
            "metric_reconstructions": self.repository_layout.metric_reconstructions.relative_to(self.root).as_posix(),
            "site_records": self.repository_layout.site_records.relative_to(self.root).as_posix(),
            "deliverables": self.repository_layout.deliverables.relative_to(self.root).as_posix(),
            "runtime": self.repository_layout.runtime.relative_to(self.root).as_posix(),
        }
        status["legacy_compatibility"] = {
            "enabled": any(path.exists() for path in (
                self.repository_layout.legacy_work,
                self.repository_layout.legacy_scene_packages,
                self.repository_layout.legacy_metric_reconstructions,
                self.repository_layout.legacy_source_files,
                self.repository_layout.legacy_site_records,
                self.repository_layout.legacy_deliverables,
            )),
            "mode": "logical_uri_resolver_only",
        }
        try:
            with sqlite3.connect(f"file:{self.repository_layout.catalog}?mode=ro", uri=True) as connection:
                status["package_migration"] = {
                    "self_contained": connection.execute(
                        "SELECT COUNT(*) FROM scene_packages WHERE self_contained = 1"
                    ).fetchone()[0],
                    "retained_legacy": connection.execute(
                        "SELECT COUNT(*) FROM scene_packages WHERE self_contained = 0"
                    ).fetchone()[0],
                    "unmigrated_legacy": connection.execute(
                        "SELECT COUNT(*) FROM scene_packages legacy "
                        "WHERE legacy.self_contained = 0 AND NOT EXISTS ("
                        "SELECT 1 FROM scene_package_sources source "
                        "WHERE source.source_scene_package_id = legacy.scene_package_id)"
                    ).fetchone()[0],
                }
        except sqlite3.Error:
            status["package_migration"] = {"status": "unavailable"}
        migration = read_json(
            self.repository_layout.migrations / "scene_repository_v2.json",
            read_json(self.repository_layout.migrations / "scene_repository_v1.json", {}),
        )
        status["migration"] = {
            "status": migration.get("status", "not_applied") if isinstance(migration, dict) else "not_applied",
            "applied_at": migration.get("applied_at") if isinstance(migration, dict) else None,
            "legacy_work_retired": migration.get("legacy_work_retired", False) if isinstance(migration, dict) else False,
        }
        return status

    def _refresh_runtime_index(self) -> list[str]:
        """Refresh the generated Viewer index after a derived runtime copy changes."""
        self._invalidate_scene_package_caches()
        script = self.root / "web_viewer/app/scripts/refresh_scene_package_index.mjs"
        if not script.is_file():
            return []
        node = shutil.which("node")
        if node is None:
            return ["viewer_runtime_index_not_refreshed:node_not_found"]
        try:
            result = subprocess.run(
                [node, str(script)], cwd=self.root, text=True, capture_output=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ["viewer_runtime_index_not_refreshed:runner_unavailable"]
        if result.returncode:
            return ["viewer_runtime_index_not_refreshed:runner_failed"]
        return []

    def _flat_scene_package_path(self, scene_package_id: str) -> Path:
        return self.repository_layout.scene_packages / scene_package_id

    def _managed_scene_package_path(self, scene_id: str, scene_package_id: str) -> Path:
        """Prefer the flat Project path while retaining legacy read support."""
        flat = self._flat_scene_package_path(scene_package_id)
        legacy = self.repository_layout.scene_packages / scene_id / scene_package_id
        if flat.exists() or not legacy.exists():
            return flat
        return legacy

    def _field_memory_scene_package_reference_counts(self) -> dict[str, int] | None:
        """Return active overlay references, or ``None`` when they cannot be checked."""

        database = self.repository_layout.site_registry
        if not database.is_file():
            return {}
        try:
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
                if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    return None
                table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'scene_versions'"
                ).fetchone()
                if table is None:
                    return None
                return {
                    str(package_id): int(count)
                    for package_id, count in connection.execute(
                        "SELECT scene_package_id, COUNT(*) FROM scene_versions GROUP BY scene_package_id"
                    )
                }
        except (OSError, sqlite3.Error, TypeError, IndexError):
            return None

    @contextmanager
    def _scene_package_import_lock(self, reconstruction_id: str, scene_package_id: str):
        """Hold an advisory, process-safe lock for one immutable import target.

        The package ID includes the SPZ content hash.  Taking the lock only
        after that hash is known lets uploads remain independent while making
        canonical creation and runtime publication a single-writer operation.
        ``flock`` is intentionally backed by a file below the App A workspace,
        so independent Preview API processes on the same repository coordinate
        as well as threads in one server process.
        """
        safe_key = f"{reconstruction_id}--{scene_package_id}"
        lock_path = self.import_locks_root / f"{safe_key}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _directory_size_bytes(path: Path) -> int:
        """Return a best-effort recursive size without following symlinks."""
        if not path.is_dir():
            return 0
        total = 0
        try:
            for item in path.rglob("*"):
                if item.is_file() and not item.is_symlink():
                    total += item.stat().st_size
        except OSError:
            return total
        return total

    def storage_status(self) -> dict[str, Any]:
        """Return the filesystem headroom used by uploads and pipeline starts."""
        usage = shutil.disk_usage(self.root / "data")
        return {
            "status": "verified" if usage.free >= self.min_free_bytes else "blocked",
            "capacity_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "minimum_free_bytes": self.min_free_bytes,
            "headroom_bytes": max(0, usage.free - self.min_free_bytes),
            "retention_policy": {
                "source_assets": "retain_until_explicit_release",
                "automatic_source_deletion": False,
                "workspace_retention_days": self._storage_lifecycle.policy.workspace_retention_days,
                "package_retention_days": self._storage_lifecycle.policy.package_retention_days,
                "staging_retention_hours": self._storage_lifecycle.policy.staging_retention_hours,
                "trash_retention_days": self._storage_lifecycle.policy.trash_retention_days,
                "cache_max_bytes": self._storage_lifecycle.policy.cache_max_bytes,
                "maximum_packages": self._storage_lifecycle.policy.maximum_packages,
            },
        }

    def storage_cleanup_preview(self) -> dict[str, Any]:
        """List only regenerable retention targets; never canonical artifacts."""
        return self._storage_lifecycle.report(execute=False)

    def storage_cleanup(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Execute the exact closed cleanup policy after explicit confirmation."""
        if payload.get("confirm") is not True:
            raise ValueError("storage_cleanup_confirmation_required")
        with self._storage_cleanup_lock:
            return self._storage_lifecycle.report(execute=True)

    def _repository_item_metadata(self, path: Path) -> tuple[int, str | None, int | None]:
        """Return a cached byte count without hashing immutable payload files."""

        if not path.exists() or path.is_symlink():
            return 0, None, None
        stat = path.stat()
        signature = stat.st_mtime_ns
        cache_key = str(path.resolve())
        cached = self._repository_size_cache.get(cache_key)
        if cached is not None and cached[0] == signature:
            size_bytes = cached[1]
            self._repository_size_cache.move_to_end(cache_key)
        else:
            size_bytes = stat.st_size if path.is_file() else self._directory_size_bytes(path)
            self._repository_size_cache[cache_key] = (signature, size_bytes)
            self._repository_size_cache.move_to_end(cache_key)
            while len(self._repository_size_cache) > MAX_REPOSITORY_SIZE_CACHE_ENTRIES:
                self._repository_size_cache.popitem(last=False)
        modified_at = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
        return size_bytes, modified_at, signature

    def repository_inventory(self) -> dict[str, Any]:
        """Inventory durable repository objects and their deletion guards.

        This is intentionally a metadata/stat operation. It never hashes Scene
        Packages or opens the 3D/COLMAP payloads, so opening the management page
        remains bounded even when workspaces are large.
        """

        if not self.repository_layout.catalog.is_file():
            rebuild_catalog(self.root)
        references = self._field_memory_scene_package_reference_counts()
        items: list[dict[str, Any]] = []
        package_ids: set[str] = set()
        package_references_by_mrp: dict[tuple[str, str], list[str]] = {}
        superseded_by: dict[str, list[str]] = {}

        with sqlite3.connect(f"file:{self.repository_layout.catalog}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            package_rows = list(connection.execute("SELECT * FROM scene_packages ORDER BY scene_package_id"))
            mrp_rows = list(connection.execute(
                "SELECT * FROM metric_reconstructions ORDER BY scene_id, reconstruction_id"
            ))
            source_rows = list(connection.execute("SELECT * FROM source_assets ORDER BY source_id"))
            for row in connection.execute(
                "SELECT scene_package_id, source_scene_package_id FROM scene_package_sources"
            ):
                superseded_by.setdefault(str(row["source_scene_package_id"]), []).append(
                    str(row["scene_package_id"])
                )

        for row in package_rows:
            package_id = str(row["scene_package_id"])
            scene_id = str(row["scene_id"])
            reconstruction_id = str(row["reconstruction_id"])
            package_ids.add(package_id)
            package_references_by_mrp.setdefault((scene_id, reconstruction_id), []).append(package_id)
            path = self.repository_layout.resolve_uri(str(row["package_uri"]))
            size_bytes, modified_at, fingerprint = self._repository_item_metadata(path)
            reference_count = None if references is None else references.get(package_id, 0)
            legacy = row["schema_version"] != "1.3.0" or not bool(row["self_contained"])
            replacements = sorted(superseded_by.get(package_id, []))
            protection_reasons: list[str] = []
            if references is None:
                protection_reasons.append("field_memory_reference_status_unknown")
            elif reference_count:
                protection_reasons.append("referenced_by_field_memory")
            if not path.is_dir() or path.is_symlink():
                protection_reasons.append("canonical_path_invalid")
            removable = not protection_reasons
            state = "current"
            if legacy:
                state = "legacy_superseded" if replacements else "legacy_without_replacement"
            items.append({
                "item_id": f"scene_package:{package_id}",
                "category": "scene_package",
                "label": package_id,
                "path": str(row["package_uri"]),
                "size_bytes": size_bytes,
                "modified_at": modified_at,
                "fingerprint": fingerprint,
                "state": state,
                "legacy": legacy,
                "schema_version": row["schema_version"],
                "scene_id": scene_id,
                "reconstruction_id": reconstruction_id,
                "self_contained": bool(row["self_contained"]),
                "field_memory_reference_count": reference_count,
                "superseded_by": replacements,
                "removable": removable,
                "recommended_cleanup": removable and state == "legacy_superseded",
                "protection_reasons": protection_reasons,
            })

        for row in mrp_rows:
            scene_id = str(row["scene_id"])
            reconstruction_id = str(row["reconstruction_id"])
            path = self.repository_layout.resolve_uri(str(row["package_uri"]))
            size_bytes, modified_at, fingerprint = self._repository_item_metadata(path)
            package_references = sorted(package_references_by_mrp.get((scene_id, reconstruction_id), []))
            protection_reasons = ["referenced_by_scene_package"] if package_references else []
            if not path.is_dir() or path.is_symlink():
                protection_reasons.append("canonical_path_invalid")
            items.append({
                "item_id": f"metric_reconstruction:{scene_id}:{reconstruction_id}",
                "category": "metric_reconstruction",
                "label": reconstruction_id,
                "path": str(row["package_uri"]),
                "size_bytes": size_bytes,
                "modified_at": modified_at,
                "fingerprint": fingerprint,
                "state": "referenced" if package_references else "unreferenced",
                "legacy": False,
                "scene_id": scene_id,
                "reconstruction_id": reconstruction_id,
                "scene_package_references": package_references,
                "removable": not protection_reasons,
                "recommended_cleanup": False,
                "protection_reasons": protection_reasons,
            })

        for row in source_rows:
            path = self.repository_layout.resolve_uri(str(row["source_uri"]))
            modified_at = None
            fingerprint = None
            if path.is_file() and not path.is_symlink():
                stat = path.stat()
                modified_at = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
                fingerprint = stat.st_mtime_ns
            items.append({
                "item_id": f"source_asset:{row['source_id']}",
                "category": "source_asset",
                "label": row["original_filename"] or row["source_id"],
                "path": str(row["source_uri"]),
                "size_bytes": int(row["size_bytes"] or 0),
                "modified_at": modified_at,
                "fingerprint": fingerprint,
                "state": "retained_source",
                "legacy": False,
                "removable": False,
                "recommended_cleanup": False,
                "protection_reasons": ["explicit_source_release_workflow_required"],
            })

        if self.repository_layout.site_records.exists():
            size_bytes, modified_at, fingerprint = self._repository_item_metadata(
                self.repository_layout.site_records
            )
            items.append({
                "item_id": "site_records:registry",
                "category": "site_records",
                "label": "Site Records / Field Memory",
                "path": self.repository_layout.site_records.relative_to(self.root).as_posix(),
                "size_bytes": size_bytes,
                "modified_at": modified_at,
                "fingerprint": fingerprint,
                "state": "active_registry",
                "legacy": False,
                "removable": False,
                "recommended_cleanup": False,
                "protection_reasons": ["registry_backup_and_release_workflow_required"],
            })

        for deliverable_root in (
            self.repository_layout.application_deliverables,
            self.repository_layout.viewer_deliverables,
            self.repository_layout.reports,
        ):
            if not deliverable_root.is_dir():
                continue
            for path in sorted(deliverable_root.iterdir()):
                if path.name == ".gitkeep" or path.is_symlink():
                    continue
                size_bytes, modified_at, fingerprint = self._repository_item_metadata(path)
                relative = path.relative_to(self.root).as_posix()
                items.append({
                    "item_id": f"deliverable:{relative}",
                    "category": "deliverable",
                    "label": path.name,
                    "path": relative,
                    "size_bytes": size_bytes,
                    "modified_at": modified_at,
                    "fingerprint": fingerprint,
                    "state": "regenerable",
                    "legacy": False,
                    "removable": True,
                    "recommended_cleanup": False,
                    "protection_reasons": [],
                })

        viewer_runtime_root = self.root / "web_viewer/app/public/scene_package/runtime"
        if viewer_runtime_root.is_dir():
            for path in sorted(viewer_runtime_root.iterdir()):
                if path.name in package_ids or path.name == ".gitkeep" or path.is_symlink():
                    continue
                size_bytes, modified_at, fingerprint = self._repository_item_metadata(path)
                relative = path.relative_to(self.root).as_posix()
                items.append({
                    "item_id": f"viewer_runtime_orphan:{path.name}",
                    "category": "viewer_runtime_orphan",
                    "label": path.name,
                    "path": relative,
                    "size_bytes": size_bytes,
                    "modified_at": modified_at,
                    "fingerprint": fingerprint,
                    "state": "orphaned_derived_copy",
                    "legacy": True,
                    "removable": True,
                    "recommended_cleanup": True,
                    "protection_reasons": [],
                })

        items.sort(key=lambda item: (
            not bool(item["recommended_cleanup"]), str(item["category"]), str(item["label"])
        ))
        categories: dict[str, dict[str, int]] = {}
        for item in items:
            category = str(item["category"])
            summary = categories.setdefault(category, {"count": 0, "size_bytes": 0})
            summary["count"] += 1
            summary["size_bytes"] += int(item["size_bytes"])
        recommended = [item for item in items if item["recommended_cleanup"]]
        return {
            "schema_version": "1.0.0",
            "status": "verified",
            "items": items,
            "summary": {
                "item_count": len(items),
                "size_bytes": sum(int(item["size_bytes"]) for item in items),
                "recommended_count": len(recommended),
                "recommended_bytes": sum(int(item["size_bytes"]) for item in recommended),
                "protected_count": sum(not bool(item["removable"]) for item in items),
                "categories": categories,
            },
            "deletion_policy": {
                "mode": "recoverable_quarantine",
                "trash_uri": self.repository_layout.repository_cleanup_trash.relative_to(self.root).as_posix(),
                "trash_retention_days": self._storage_lifecycle.policy.trash_retention_days,
                "source_assets": "protected",
                "site_records": "protected",
            },
        }

    def repository_cleanup_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        inventory = self.repository_inventory()
        available = {str(item["item_id"]): item for item in inventory["items"]}
        if payload.get("selection") == "recommended":
            item_ids = [key for key, item in available.items() if item["recommended_cleanup"]]
        else:
            raw_ids = payload.get("item_ids")
            if not isinstance(raw_ids, list) or not all(isinstance(value, str) for value in raw_ids):
                raise ValueError("repository_cleanup_item_ids_required")
            if len(raw_ids) > 500:
                raise ValueError("repository_cleanup_selection_too_large")
            item_ids = list(dict.fromkeys(raw_ids))
        missing = [item_id for item_id in item_ids if item_id not in available]
        if missing:
            raise ValueError(f"repository_cleanup_items_not_found:{','.join(missing)}")
        selected = [available[item_id] for item_id in item_ids]
        blocked = [item for item in selected if not item["removable"]]
        fingerprint = [
            [item["item_id"], item["path"], item["size_bytes"], item["fingerprint"]]
            for item in selected
        ]
        confirmation_token = hashlib.sha256(
            json.dumps(fingerprint, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return {
            "schema_version": "1.0.0",
            "status": "verified",
            "ready": bool(selected) and not blocked,
            "selected_count": len(selected),
            "selected_bytes": sum(int(item["size_bytes"]) for item in selected),
            "selected_items": selected,
            "blocked_count": len(blocked),
            "blocked_items": blocked,
            "confirmation_token": confirmation_token if selected and not blocked else None,
            "operation": "move_to_recoverable_repository_trash",
        }

    def repository_cleanup(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Move an exact previewed selection to recoverable quarantine."""

        if payload.get("confirm") is not True:
            raise ValueError("repository_cleanup_confirmation_required")
        token = payload.get("confirmation_token")
        if not isinstance(token, str) or not token:
            raise ValueError("repository_cleanup_confirmation_token_required")
        selection_payload = {
            "selection": payload.get("selection"),
            "item_ids": payload.get("item_ids"),
        }
        with self._repository_cleanup_lock:
            preview = self.repository_cleanup_preview(selection_payload)
            if not preview["ready"]:
                raise ValueError("repository_cleanup_selection_not_removable")
            if token != preview["confirmation_token"]:
                raise ValueError("repository_cleanup_preview_stale")

            operation_id = (
                datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                + "-" + uuid.uuid4().hex[:8]
            )
            trash = self.repository_layout.repository_cleanup_trash / operation_id
            trash.mkdir(parents=True, exist_ok=False)
            moved: list[tuple[Path, Path]] = []

            def move(source: Path, destination: Path) -> None:
                if not source.exists() or source.is_symlink():
                    raise ValueError(f"repository_cleanup_source_invalid:{source.relative_to(self.root)}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise FileExistsError(f"repository_cleanup_destination_exists:{destination}")
                shutil.move(str(source), str(destination))
                moved.append((source, destination))

            try:
                for item in preview["selected_items"]:
                    category = str(item["category"])
                    source = (self.root / str(item["path"])).resolve()
                    if category == "scene_package":
                        base = self.repository_layout.scene_packages.resolve()
                        scene_package_id = str(item["label"])
                        with scene_package_reference_lock(self.root, scene_package_id):
                            references = self._field_memory_scene_package_reference_counts()
                            if references is None or references.get(scene_package_id, 0):
                                raise ValueError(
                                    f"scene_package_referenced_by_field_memory:{scene_package_id}"
                                )
                            relative = source.relative_to(base)
                            move(source, trash / "scene_packages" / relative)
                            runtime = (
                                self.root / "web_viewer/app/public/scene_package/runtime" / scene_package_id
                            )
                            if runtime.exists():
                                move(runtime, trash / "viewer_runtime" / scene_package_id)
                    elif category == "metric_reconstruction":
                        base = self.repository_layout.metric_reconstructions.resolve()
                        relative = source.relative_to(base)
                        move(source, trash / "metric_reconstructions" / relative)
                    elif category == "deliverable":
                        base = self.repository_layout.deliverables.resolve()
                        relative = source.relative_to(base)
                        move(source, trash / "deliverables" / relative)
                    elif category == "viewer_runtime_orphan":
                        base = (
                            self.root / "web_viewer/app/public/scene_package/runtime"
                        ).resolve()
                        relative = source.relative_to(base)
                        move(source, trash / "viewer_runtime_orphans" / relative)
                    else:
                        raise ValueError(f"repository_cleanup_category_not_allowed:{category}")

                atomic_json(trash / "cleanup_manifest.json", {
                    "schema_version": "1.0.0",
                    "operation_id": operation_id,
                    "created_at": utc_now(),
                    "status": "quarantined",
                    "retention_days": self._storage_lifecycle.policy.trash_retention_days,
                    "items": preview["selected_items"],
                })
            except Exception:
                for source, destination in reversed(moved):
                    if destination.exists() and not source.exists():
                        source.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(destination), str(source))
                shutil.rmtree(trash, ignore_errors=True)
                raise

            warnings = self._refresh_runtime_index()
            warnings.extend(self._refresh_repository_catalog())
            return {
                "schema_version": "1.0.0",
                "status": "verified",
                "operation": "quarantined",
                "operation_id": operation_id,
                "removed_count": preview["selected_count"],
                "removed_bytes": preview["selected_bytes"],
                "trashed_uri": trash.relative_to(self.root).as_posix(),
                "recoverable_for_days": self._storage_lifecycle.policy.trash_retention_days,
                "items": preview["selected_items"],
                "warnings": warnings,
            }
