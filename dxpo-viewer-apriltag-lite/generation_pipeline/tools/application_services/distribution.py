"""Application distribution recipe, job, and artifact service extracted from the PreviewService compatibility facade."""

from __future__ import annotations

from .common import *  # noqa: F403
from packages.distribution_contracts.lifecycle import artifact_inventory, retention_preview


class DistributionService:
    def product_release_inventory(self) -> dict[str, Any]:
        return artifact_inventory(self.root)

    def product_retention_preview(self) -> dict[str, Any]:
        return retention_preview(self.root)

    def app_package_catalog(self) -> dict[str, Any]:
        """Return the closed component catalog and verified bundle candidates."""
        catalog = package_catalog()
        catalog["scene_packages"] = [
            {
                "scene_package_id": item["scene_package_id"],
                "scene_id": item["scene_id"],
                "reconstruction_id": item["reconstruction_id"],
                "project_name": item.get("project_name"),
                "data_date": item.get("data_date"),
                "data_date_source": item.get("data_date_source"),
                "registered_at": item.get("registered_at"),
                "spz": item.get("spz"),
                "schema_version": item.get("schema_version"),
                "dataset": item.get("dataset"),
                "capabilities": item.get("capabilities", {}),
                "application_compatibility": item.get("application_compatibility", {}),
                "dynamic_tracking_compatible": item.get("dynamic_tracking_compatible") is True,
                "dynamic_tracking_failure_reasons": item.get("dynamic_tracking_failure_reasons", []),
            }
            for item in self.scene_packages()
            if item.get("managed") is True
            and item.get("status") == "verified"
            and isinstance(item.get("scene_package_id"), str)
        ]
        return catalog

    def app_packages(self) -> list[dict[str, Any]]:
        """List derived ZIPs and their closed, embedded distribution metadata.

        Reading the two small JSON manifests from the ZIP keeps the package
        builder useful after an API restart without creating mutable sidecar
        state.  Archive members are never extracted and malformed/legacy ZIPs
        remain downloadable with basic file metadata only.
        """
        packages: list[dict[str, Any]] = []
        for path in sorted(self.app_packages_root.glob("*.zip"), key=lambda item: item.stat().st_mtime, reverse=True):
            if path.is_symlink() or not path.is_file():
                continue
            stat = path.stat()
            metadata: dict[str, Any] = {}
            try:
                with zipfile.ZipFile(path) as archive:
                    names = archive.namelist()

                    def embedded_json(
                        filename: str,
                        *,
                        archive_names: tuple[str, ...] = tuple(names),
                    ) -> dict[str, Any]:
                        matches = [name for name in archive_names if name.endswith(f"/{filename}")]
                        if len(matches) != 1:
                            return {}
                        info = archive.getinfo(matches[0])
                        if info.file_size > 1024 * 1024:
                            return {}
                        value = json.loads(archive.read(info).decode("utf-8"))
                        return value if isinstance(value, dict) else {}

                    manifest = embedded_json("app_package_manifest.json")
                    recipe = embedded_json("package_recipe.json")
                    bundled = manifest.get("bundled_scene_packages")
                    analysis_overlay = manifest.get("analysis_overlay")
                    content_delivery = manifest.get("content_delivery")
                    product_release = manifest.get("product_release")
                    metadata = {
                        "application_id": manifest.get("application_id"),
                        "label": recipe.get("label"),
                        "ui_modules": recipe.get("ui_modules", []),
                        "backend_components": recipe.get("backend_components", []),
                        "scene_package_ids": recipe.get("scene_package_ids", []),
                        "viewer_variant": recipe.get("viewer_variant", "full"),
                        "requirements": manifest.get("requirements", {}),
                        "bundled_scene_packages": bundled if isinstance(bundled, list) else [],
                        "analysis_overlay": analysis_overlay if isinstance(analysis_overlay, dict) else None,
                        "content_delivery": content_delivery if isinstance(content_delivery, dict) else None,
                        "product_release": product_release if isinstance(product_release, dict) else None,
                        "setup_included": any(name.endswith("/SETUP.md") for name in names),
                    }
                    manifest_created_at = manifest.get("created_at")
                    if isinstance(manifest_created_at, str) and manifest_created_at:
                        metadata["created_at"] = manifest_created_at
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile, KeyError):
                metadata = {}
            packages.append({
                "filename": path.name,
                "size_bytes": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                "output_path": path.relative_to(self.root).as_posix(),
                "download_url": f"/api/app-packages/{path.name}/download",
                **metadata,
            })
        return packages[:50]

    def _scene_package_size_signature(self, package_root: Path) -> tuple[Any, ...]:
        """Return the mutation boundary for one immutable registered Package."""

        paths = [package_root, package_root / "scene_manifest.json"]
        try:
            paths.extend(package_root.iterdir())
        except OSError:
            pass
        return tuple(
            self._path_state_signature(path)
            for path in sorted(paths)
        )

    def _scene_package_tree_size_bytes(
        self,
        package_root: Path,
        package_id: str,
    ) -> tuple[int, bool]:
        """Measure an immutable Package once and reuse it until invalidation.

        Canonical Package contents cannot be edited in place. Supported
        import, unregister, runtime publication, and repository cleanup paths
        invalidate this cache explicitly. The bounded root/top-level signature
        also detects out-of-process replacement before a cached value is used.
        A cold scan still rejects every symlink and retries on concurrent
        filesystem changes.
        """

        if package_root.is_symlink() or not package_root.is_dir():
            raise ValueError(f"package_recipe_scene_package_path_invalid:{package_id}")
        cache_key = str(package_root.resolve())
        with self._scene_package_cache_lock:
            signature = self._scene_package_size_signature(package_root)
            cached = self._scene_package_size_cache.get(cache_key)
            if cached is not None and cached[0] == signature:
                self._scene_package_size_cache.move_to_end(cache_key)
                return cached[1], True

            for _attempt in range(2):
                before = self._scene_package_size_signature(package_root)
                package_bytes = 0
                for path in package_root.rglob("*"):
                    if path.is_symlink():
                        raise ValueError(
                            f"package_recipe_scene_package_symlink_not_allowed:{package_id}"
                        )
                    if path.is_file():
                        package_bytes += path.stat().st_size
                after = self._scene_package_size_signature(package_root)
                if before != after:
                    continue
                self._scene_package_size_cache[cache_key] = (after, package_bytes)
                self._scene_package_size_cache.move_to_end(cache_key)
                while (
                    len(self._scene_package_size_cache)
                    > MAX_SCENE_PACKAGE_SIZE_CACHE_ENTRIES
                ):
                    self._scene_package_size_cache.popitem(last=False)
                return package_bytes, False
        raise ValueError(f"package_recipe_scene_package_changed_during_preflight:{package_id}")

    def _validated_app_package_recipe(
        self,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
        recipe = normalize_package_recipe(payload)
        package_candidates = {
            str(item["scene_package_id"]): item
            for item in self.scene_packages()
            if item.get("managed") is True
            and item.get("status") == "verified"
            and isinstance(item.get("scene_package_id"), str)
        }
        bundled_bytes = 0
        package_details: list[dict[str, Any]] = []
        # Only a distribution that bundles the AprilTag-to-Viewer transport
        # requires a dynamic-tracking compatible Scene Package. Without a
        # bundled rosbridge, AprilTag checking and static viewing are separate.
        requires_dynamic_tracking = (
            recipe["include_rosbridge"]
            and "apriltag-check" in recipe["ui_modules"]
        )
        required_capabilities: list[str] = []
        profile_id = recipe.get("product_profile_id")
        if isinstance(profile_id, str):
            profile = next(
                item for item in package_catalog()["product_profiles"]
                if item["metadata"]["id"] == profile_id
            )
            required_capabilities = list(
                profile["content"].get("required_scene_capabilities", [])
            )
        for package_id in recipe["scene_package_ids"]:
            package_entry = package_candidates.get(package_id)
            if package_entry is None:
                raise ValueError(f"package_recipe_scene_package_not_verified:{package_id}")
            if requires_dynamic_tracking and package_entry.get("dynamic_tracking_compatible") is not True:
                failure_reasons = package_entry.get("dynamic_tracking_failure_reasons")
                reason_text = ",".join(str(reason) for reason in failure_reasons) \
                    if isinstance(failure_reasons, list) else "dynamic_tracking_contract_missing"
                raise ValueError(
                    f"package_recipe_scene_package_dynamic_tracking_incompatible:{package_id}:{reason_text}"
                )
            capabilities = package_entry.get("capabilities")
            missing_capabilities = [
                name for name in required_capabilities
                if not isinstance(capabilities, dict) or capabilities.get(name) is not True
            ]
            if missing_capabilities:
                raise ValueError(
                    "product_profile_scene_capability_missing:"
                    f"{package_id}:{','.join(missing_capabilities)}"
                )
            compatibility = package_entry.get("application_compatibility")
            for ui_module in recipe["ui_modules"]:
                requirement = (
                    compatibility.get(ui_module)
                    if isinstance(compatibility, dict) else None
                )
                if ui_module in SCENE_DATA_APP_MODULES and not isinstance(requirement, dict):
                    raise ValueError(
                        "package_recipe_scene_package_application_incompatible:"
                        f"{package_id}:{ui_module}:application_contract_missing"
                    )
                if isinstance(requirement, dict) and requirement.get("compatible") is not True:
                    reasons = requirement.get("failure_reasons")
                    reason_text = ",".join(str(reason) for reason in reasons) \
                        if isinstance(reasons, list) else "application_contract_missing"
                    raise ValueError(
                        "package_recipe_scene_package_application_incompatible:"
                        f"{package_id}:{ui_module}:{reason_text}"
                    )
            package_uri = package_entry.get("package_uri")
            if not isinstance(package_uri, str):
                raise ValueError(f"package_recipe_scene_package_uri_missing:{package_id}")
            package_root = self.root / safe_relative(package_uri, "scene_package_uri")
            source_package_bytes, _size_cache_hit = self._scene_package_tree_size_bytes(
                package_root,
                package_id,
            )
            package_manifest = read_json(package_root / "scene_manifest.json", {})
            package_bytes = (
                display_scene_package_projection_size(
                    package_root,
                    package_manifest,
                )
                if recipe.get("scene_content_mode") == "display"
                and isinstance(package_manifest, dict)
                else source_package_bytes
            )
            bundled_bytes += package_bytes
            package_details.append({
                "scene_package_id": package_id,
                "size_bytes": package_bytes,
                "package_uri": package_uri,
                "source_size_bytes": source_package_bytes,
                "scene_content_mode": recipe.get("scene_content_mode"),
            })
        return recipe, bundled_bytes, package_details

    def _application_source_estimate_bytes(self) -> int:
        cached = getattr(self, "_app_source_estimate_cache", None)
        if isinstance(cached, int):
            return cached
        ignored = {
            "__pycache__", "node_modules", "dist", ".vite",
            "playwright-report", "test-results", "runtime",
        }
        total = 0
        for relative in (
            "apps", "generation_pipeline", "packages", "scene_spec",
            "scripts", "services", "web_viewer", "docker",
        ):
            source = self.root / relative
            if not source.exists():
                continue
            for path in source.rglob("*"):
                if any(part in ignored for part in path.parts):
                    continue
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
        for name in (".dockerignore", "THIRD_PARTY_NOTICES.md", "LICENSE"):
            path = self.root / name
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        self._app_source_estimate_cache = total
        return total

    def app_package_preflight(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate a recipe and estimate peak disk usage before creating files."""

        recipe, bundled_bytes, package_details = self._validated_app_package_recipe(payload)
        overlay = (
            estimate_active_analysis_overlay(self.root, recipe["scene_package_ids"])
            if recipe["include_active_analysis"]
            else {
                "scene_package_ids": recipe["scene_package_ids"],
                "run_count": 0,
                "frame_count": 0,
                "attachment_count": 0,
                "attachment_bytes": 0,
                "database_estimate_bytes": 2 * 1024 * 1024 if "frame-analysis" in recipe["ui_modules"] else 0,
                "total_bytes": 2 * 1024 * 1024 if "frame-analysis" in recipe["ui_modules"] else 0,
                "packages_without_active_run": [],
                "runs": [],
            }
        )
        source_bytes = self._application_source_estimate_bytes()
        metadata_allowance = 32 * 1024 * 1024
        runtime_staging_bytes = (
            source_bytes
            + int(overlay["total_bytes"])
            + metadata_allowance
            + (0 if recipe["distribution_mode"] == "split" else bundled_bytes)
        )
        scene_copy_staging_bytes = bundled_bytes
        content_pack_estimate_bytes = (
            bundled_bytes + max(1024 * 1024, bundled_bytes // 1000)
            if recipe["distribution_mode"] == "split" and bundled_bytes
            else 0
        )
        # ZIP_DEFLATED cannot be assumed to shrink already-compressed SPZ/images.
        # Treat the complete staging tree plus ZIP headers as the safe upper bound.
        estimated_zip_bytes = runtime_staging_bytes + max(
            1024 * 1024, runtime_staging_bytes // 1000
        )
        peak_additional_bytes = (
            runtime_staging_bytes
            + scene_copy_staging_bytes
            + content_pack_estimate_bytes
            + estimated_zip_bytes
        )
        storage = self.storage_status()
        required_free_bytes = self.min_free_bytes + peak_additional_bytes
        return {
            "schema_version": "1.0.0",
            "status": "verified" if int(storage["free_bytes"]) >= required_free_bytes else "insufficient_storage",
            "recipe": recipe,
            "scene_packages": package_details,
            "application_source_bytes": source_bytes,
            "scene_package_bytes": bundled_bytes,
            "analysis_overlay": overlay,
            "metadata_allowance_bytes": metadata_allowance,
            "staging_copy_bytes": runtime_staging_bytes + scene_copy_staging_bytes,
            "runtime_staging_bytes": runtime_staging_bytes,
            "content_pack_estimate_bytes": content_pack_estimate_bytes,
            "estimated_zip_bytes": estimated_zip_bytes,
            "peak_additional_bytes": peak_additional_bytes,
            "minimum_free_reserve_bytes": self.min_free_bytes,
            "required_free_bytes": required_free_bytes,
            "available_free_bytes": int(storage["free_bytes"]),
            "can_build": int(storage["free_bytes"]) >= required_free_bytes,
        }

    def _app_package_result(
        self,
        result: dict[str, Any],
        recipe: dict[str, Any],
        output: Path,
    ) -> dict[str, Any]:
        filename = output.name
        return {
            **result,
            "output": output.relative_to(self.root).as_posix(),
            "output_path": output.relative_to(self.root).as_posix(),
            "filename": filename,
            "download_url": f"/api/app-packages/{filename}/download",
            "label": recipe["label"],
            "ui_modules": recipe["ui_modules"],
            "backend_components": recipe["backend_components"],
            "scene_package_ids": recipe["scene_package_ids"],
            "viewer_variant": recipe["viewer_variant"],
            "requirements": {
                "gpu": recipe["needs_gpu"],
                "camera": recipe["needs_camera"],
                "rosbridge": recipe["include_rosbridge"],
            },
            "setup_included": True,
        }

    def create_app_package(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Build one immutable derived ZIP synchronously for CLI/tests."""

        preflight = self.app_package_preflight(payload)
        if not preflight["can_build"]:
            raise ValueError(
                "insufficient_storage:create_app_package:"
                f"free_bytes={preflight['available_free_bytes']}:"
                f"required_bytes={preflight['required_free_bytes']}"
            )
        recipe = preflight["recipe"]
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        filename = f"{recipe['package_id']}-{timestamp}.zip"
        output = self.app_packages_root / filename
        with self._package_lock:
            if output.exists():
                raise FileExistsError(f"app_package_already_exists:{filename}")
            result = create_custom_app_distribution(self.root, recipe, output)
        return self._app_package_result(result, recipe, output)

    def _app_package_job_path(self, job_id: str) -> Path:
        if not JOB_ID_RE.fullmatch(job_id):
            raise ValueError("invalid_app_package_job_id")
        return self.app_package_jobs_root / f"{job_id}.json"

    def _write_app_package_job(self, job: dict[str, Any]) -> None:
        with self._app_package_jobs_lock:
            self.app_package_jobs_root.mkdir(parents=True, exist_ok=True)
            atomic_json(self._app_package_job_path(str(job["job_id"])), job)

    def _update_app_package_job(
        self,
        job_id: str,
        updates: dict[str, Any],
        *,
        expected_statuses: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        """Merge one job update under the same lock used for persistence."""

        with self._app_package_jobs_lock:
            path = self._app_package_job_path(job_id)
            current = read_json(path, {})
            if not isinstance(current, dict) or not current:
                raise FileNotFoundError(f"app_package_job_not_found:{job_id}")
            if expected_statuses is not None and current.get("status") not in expected_statuses:
                raise ValueError("app_package_job_status_changed")
            cancellation_requested = current.get("cancellation_requested") is True
            cancellation_message = current.get("message")
            current.update(updates)
            if cancellation_requested:
                current["cancellation_requested"] = True
                if updates.get("status") == "running":
                    current["status"] = "cancelling"
                    if isinstance(cancellation_message, str):
                        current["message"] = cancellation_message
            atomic_json(path, current)
            return current

    def _reconcile_interrupted_app_package_jobs(self) -> None:
        """Fail closed after API restart; completed ZIPs remain independently listed."""

        for path in self.app_package_jobs_root.glob("*.json"):
            record = read_json(path, {})
            if not isinstance(record, dict) or record.get("status") not in {"queued", "running", "cancelling"}:
                continue
            record["status"] = "interrupted"
            record["completed_at"] = utc_now()
            record["failure_reason"] = "application_api_restarted"
            record["can_retry"] = True
            record["message"] = "API再起動により中断しました。再実行できます。"
            atomic_json(path, record)

    def app_package_job(self, job_id: str) -> dict[str, Any]:
        record = read_json(self._app_package_job_path(job_id), {})
        if not isinstance(record, dict) or not record:
            raise FileNotFoundError(f"app_package_job_not_found:{job_id}")
        return record

    def app_package_jobs(self) -> list[dict[str, Any]]:
        records = [
            self.app_package_job(path.stem)
            for path in self.app_package_jobs_root.glob("*.json")
            if path.is_file() and not path.is_symlink()
        ]
        return sorted(records, key=lambda item: str(item.get("created_at", "")), reverse=True)[:50]

    @staticmethod
    def _app_package_overall_progress(stage: str, current: int, total: int) -> int:
        spans = {
            "application_source": (2, 15),
            "scene_packages": (15, 42),
            "content_packs": (42, 55),
            "analysis_database": (55, 60),
            "analysis_attachments": (60, 78),
            "metadata": (78, 84),
            "zip": (84, 99),
        }
        start, end = spans.get(stage, (0, 99))
        fraction = min(1.0, max(0.0, current / max(total, 1)))
        return int(round(start + (end - start) * fraction))

    def _run_app_package_job(self, job_id: str, output: Path) -> None:
        cancel_event = self._app_package_cancel_events[job_id]
        job = self.app_package_job(job_id)
        job = self._update_app_package_job(job_id, {
            "status": "running",
            "started_at": utc_now(),
            "message": "配布用ファイルを収録しています",
            "progress_percent": 1,
        })

        def progress(stage: str, current: int, total: int, message: str) -> None:
            self._update_app_package_job(job_id, {
                "status": "running",
                "stage": stage,
                "stage_current": current,
                "stage_total": total,
                "message": message,
                "progress_percent": self._app_package_overall_progress(stage, current, total),
                "updated_at": utc_now(),
            })

        try:
            with self._package_lock:
                result = create_custom_app_distribution(
                    self.root,
                    job["recipe"],
                    output,
                    progress_callback=progress,
                    cancel_requested=cancel_event.is_set,
                )
            completed = self.app_package_job(job_id)
            self._update_app_package_job(job_id, {
                "status": "completed",
                "stage": "completed",
                "progress_percent": 100,
                "message": "Docker ZIPの生成が完了しました",
                "completed_at": utc_now(),
                "can_retry": False,
                "result": self._app_package_result(result, completed["recipe"], output),
                "failure_reason": None,
            })
        except DistributionCancelled:
            self._update_app_package_job(job_id, {
                "status": "cancelled",
                "message": "生成を中止しました。途中ファイルは公開していません。",
                "completed_at": utc_now(),
                "can_retry": True,
                "failure_reason": "cancelled_by_operator",
            })
        except BaseException as error:  # noqa: BLE001 - background job must persist failure
            self._update_app_package_job(job_id, {
                "status": "failed",
                "message": "Docker ZIPを生成できませんでした",
                "completed_at": utc_now(),
                "can_retry": True,
                "failure_reason": str(error),
            })
        finally:
            self._app_package_job_threads.pop(job_id, None)
            self._app_package_cancel_events.pop(job_id, None)

    def start_app_package_job(
        self,
        payload: dict[str, Any],
        *,
        retry_of_job_id: str | None = None,
    ) -> dict[str, Any]:
        preflight = self.app_package_preflight(payload)
        if not preflight["can_build"]:
            raise ValueError(
                "insufficient_storage:create_app_package:"
                f"free_bytes={preflight['available_free_bytes']}:"
                f"required_bytes={preflight['required_free_bytes']}"
            )
        recipe = preflight["recipe"]
        job_id = f"app-package-{uuid.uuid4().hex[:16]}"
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        filename = f"{recipe['package_id']}-{timestamp}.zip"
        output = self.app_packages_root / filename
        record = {
            "schema_version": "1.0.0",
            "kind": "application_package_build_job",
            "job_id": job_id,
            "status": "queued",
            "stage": "queued",
            "progress_percent": 0,
            "message": "生成待ちです",
            "created_at": utc_now(),
            "started_at": None,
            "completed_at": None,
            "updated_at": utc_now(),
            "cancellation_requested": False,
            "can_retry": False,
            "failure_reason": None,
            "filename": filename,
            "output_path": output.relative_to(self.root).as_posix(),
            "recipe": recipe,
            "preflight": preflight,
        }
        if retry_of_job_id is not None:
            record["retry_of_job_id"] = retry_of_job_id
        self._write_app_package_job(record)
        event = threading.Event()
        thread = threading.Thread(
            target=self._run_app_package_job,
            args=(job_id, output),
            name=f"app-package-{job_id}",
            daemon=True,
        )
        self._app_package_cancel_events[job_id] = event
        self._app_package_job_threads[job_id] = thread
        thread.start()
        return record

    def cancel_app_package_job(self, job_id: str) -> dict[str, Any]:
        job = self.app_package_job(job_id)
        if job.get("status") not in {"queued", "running"}:
            if job.get("status") in {"cancelling", "cancelled"}:
                return job
            raise ValueError("app_package_job_not_cancellable")
        event = self._app_package_cancel_events.get(job_id)
        if event is None:
            raise ValueError("app_package_job_process_not_available")
        event.set()
        try:
            return self._update_app_package_job(job_id, {
                "status": "cancelling",
                "cancellation_requested": True,
                "message": "安全なファイル境界で停止しています",
                "updated_at": utc_now(),
            }, expected_statuses=frozenset({"queued", "running"}))
        except ValueError as error:
            if str(error) != "app_package_job_status_changed":
                raise
            current = self.app_package_job(job_id)
            if current.get("status") in {"cancelling", "cancelled"}:
                return current
            raise ValueError("app_package_job_not_cancellable") from error

    def retry_app_package_job(self, job_id: str) -> dict[str, Any]:
        job = self.app_package_job(job_id)
        if job.get("status") not in {"failed", "cancelled", "interrupted"}:
            raise ValueError("app_package_job_not_retryable")
        return self.start_app_package_job(
            dict(job["recipe"]),
            retry_of_job_id=job_id,
        )

    def app_package_download(self, filename: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.zip", filename):
            raise ValueError("app_package_filename_invalid")
        path = self.app_packages_root / filename
        if path.is_symlink() or not path.is_file() or path.resolve().parent != self.app_packages_root.resolve():
            raise FileNotFoundError("app_package_not_found")
        return path

    def content_pack_download(self, digest: str) -> Path:
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("content_pack_digest_invalid")
        root = self.repository_layout.content_packs.resolve()
        path = self.repository_layout.content_packs / f"{digest}.zip"
        if path.is_symlink() or not path.is_file() or path.resolve().parent != root:
            raise FileNotFoundError("content_pack_not_found")
        if sha256(path) != digest:
            raise ValueError("content_pack_digest_mismatch")
        return path

    def _require_storage_headroom(self, additional_bytes: int, operation: str) -> None:
        if additional_bytes < 0:
            raise ValueError("storage_additional_bytes_invalid")
        status = self.storage_status()
        required = self.min_free_bytes + additional_bytes
        if int(status["free_bytes"]) < required:
            raise ValueError(
                f"insufficient_storage:{operation}:free_bytes={status['free_bytes']}:required_bytes={required}"
            )
