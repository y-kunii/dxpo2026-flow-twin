"""Generation pipeline job, progress, upload, and MRP service extracted from the PreviewService compatibility facade."""

from __future__ import annotations

import sys

from .common import *  # noqa: F403


class PipelineJobService:
    def _resolve_persisted_uri(self, value: Any, label: str) -> Path | None:
        """Resolve a stored repository URI through the layout-v2 compatibility map."""
        if not isinstance(value, str) or not value.strip():
            return None
        uri = safe_relative(value, label)
        path = self.repository_layout.resolve_uri(uri)
        resolved = path.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise ValueError(f"{label}_outside_repository")
        return resolved

    def _canonicalize_record_uri(self, record: dict[str, Any], key: str) -> Path | None:
        """Expose the canonical URI while retaining the historical value in-memory."""
        original = record.get(key)
        path = self._resolve_persisted_uri(original, key)
        if path is None:
            return None
        if path.exists():
            canonical = path.relative_to(self.root).as_posix()
            if canonical != original:
                record.setdefault(f"legacy_{key}", original)
                record[key] = canonical
        return path

    def _rewrite_legacy_config_uris(self, value: Any) -> Any:
        """Rewrite only known repository-layout prefixes in a copied config tree."""
        if isinstance(value, dict):
            return {
                key: self._rewrite_legacy_config_uris(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._rewrite_legacy_config_uris(item) for item in value]
        if isinstance(value, str):
            normalized = value.replace("\\", "/").lstrip("./")
            legacy_prefixes = (
                "data/work/",
                "data/scene_packages/",
                "data/metric_reconstruction_packages/",
                "data/sources/",
                "data/field_memory/",
                "data/output/",
            )
            if normalized == "data/catalog.sqlite3" or normalized.startswith(legacy_prefixes):
                return self.repository_layout.rewrite_legacy_uri(value)
            return value
        return value

    def _materialize_resume_config(
        self,
        job_id: str,
        source: Path,
    ) -> tuple[Path, dict[str, Any] | None]:
        """Create a new config only when a historical job still embeds legacy URIs."""
        config = read_json(source)
        if not isinstance(config, dict):
            raise ValueError("scene_config_not_object")
        normalized = self._rewrite_legacy_config_uris(config)
        relinked_input_sha256: str | None = None
        input_uri = normalized.get("input_video")
        input_path = self.root / str(input_uri or "")
        if not input_path.is_file():
            workspace_uri = normalized.get("workspace")
            manifest = read_json(
                self.root / str(workspace_uri or "") / "input_manifest.json",
                {},
            )
            digest = manifest.get("sha256") if isinstance(manifest, dict) else None
            if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
                candidates = [
                    path
                    for path in (self.repository_layout.sources / digest).glob("asset.*")
                    if path.is_file()
                ]
                if len(candidates) == 1:
                    expected_size = manifest.get("size")
                    if not isinstance(expected_size, int) or candidates[0].stat().st_size == expected_size:
                        normalized["input_video"] = candidates[0].relative_to(self.root).as_posix()
                        relinked_input_sha256 = digest
        normalized_input = normalized.get("input_video")
        if isinstance(normalized_input, str) and not (self.root / normalized_input).is_file():
            raise ValueError("resume_input_video_unresolved")
        if normalized == config:
            return source, None
        target = self.jobs_root / f"{job_id}.scene.json"
        atomic_json(target, normalized)
        provenance = {
            "source_config_uri": source.relative_to(self.root).as_posix(),
            "source_config_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "normalization": "scene_repository_layout_v2_uri_rewrite",
        }
        if relinked_input_sha256 is not None:
            provenance["input_video_relinked_from_sha256"] = relinked_input_sha256
        return target, provenance

    def _job_path(self, job_id: str) -> Path:
        if not JOB_ID_RE.fullmatch(job_id):
            raise ValueError("invalid_job_id")
        return self.jobs_root / f"{job_id}.json"

    def _job(self, job_id: str) -> dict[str, Any]:
        record = read_json(self._job_path(job_id))
        if not isinstance(record, dict) and job_id.startswith("existing-"):
            scene_id = job_id.removeprefix("existing-")
            if SCENE_ID_RE.fullmatch(scene_id):
                config_path = self._config_path(scene_id)
                config = read_json(config_path, {})
                workspace = self.root / str(config.get("workspace", ""))
                report_path = workspace / "pipeline_report.json"
                report = read_json(report_path, {})
                if isinstance(report, dict) and report:
                    return self._enrich_job_record({
                        "schema_version": "1.0.0",
                        "job_id": job_id,
                        "scene_id": scene_id,
                        "status": report.get("status", "not_available"),
                        "started_at": report.get("started_at"),
                        "completed_at": report.get("completed_at"),
                        "exit_code": report.get("exit_code", 0 if report.get("status") == "verified" else 2),
                        "existing": True,
                        "config_uri": str(config_path.relative_to(self.root)),
                        "report_uri": str(report_path.relative_to(self.root)),
                        "log_uri": str((workspace / "pipeline.log").relative_to(self.root)),
                        "failure_reasons": report.get("failure_reasons", []),
                        "warnings": report.get("warnings", []),
                    })
        if not isinstance(record, dict):
            raise FileNotFoundError(f"job_not_found:{job_id}")
        process = self._processes.get(job_id)
        if process is not None and process.poll() is not None:
            self._refresh_process(job_id, process)
            record = read_json(self._job_path(job_id), record)
        # A pipeline can be resumed from its CLI as well as from this local
        # API.  In that case the runner updates its canonical report, but no
        # API process exists to update this small UI bookkeeping record.
        # Reconcile only a newer report that explicitly names this job's
        # immutable config; a report from another project must never change a
        # job's displayed result.
        if self._reconcile_terminal_job_record(record):
            self._write_job(record)
        return self._enrich_job_record(record)

    def jobs(self) -> list[dict[str, Any]]:
        job_records = [self._job(path.stem) for path in sorted(self.jobs_root.glob("*.json")) if not path.name.endswith(".scene.json")]
        records: list[dict[str, Any]] = []
        for config_path in sorted((self.root / "generation_pipeline/config/scenes").glob("*.json")):
            config = read_json(config_path, {})
            scene_id = config.get("scene_id") if isinstance(config, dict) else None
            if isinstance(scene_id, str):
                try:
                    existing = self._job(f"existing-{scene_id}")
                except (FileNotFoundError, ValueError):
                    continue
                if existing.get("existing"):
                    # A failed exploratory App A job must never hide the
                    # verified immutable scene that users can still reuse.
                    records.append(existing)
        return [*records, *job_records]

    def _write_job(self, job: dict[str, Any]) -> None:
        atomic_json(self._job_path(job["job_id"]), job)
        self._refresh_repository_catalog()

    @staticmethod
    def _pid_is_alive(pid: Any) -> bool:
        try:
            value = int(pid)
        except (TypeError, ValueError):
            return False
        if value <= 0:
            return False
        try:
            os.kill(value, 0)
        except (OSError, ProcessLookupError):
            return False
        return True

    def _active_job_for_scene(self, scene_id: str | None) -> dict[str, Any] | None:
        """Return an observable running job, optionally scoped to one scene."""
        for path in sorted(self.jobs_root.glob("*.json")):
            if path.name.endswith(".scene.json"):
                continue
            record = read_json(path, {})
            if not isinstance(record, dict) or (
                scene_id is not None and str(record.get("scene_id")) != scene_id
            ):
                continue
            if record.get("status") not in {"queued", "running"}:
                continue
            process = self._processes.get(str(record.get("job_id")))
            if process is not None and process.poll() is None:
                return record
            if self._pid_is_alive(record.get("pid")):
                return record
        return None

    @staticmethod
    def _file_count(path: Path, modified_since: float | None = None) -> int:
        """Count generated files without treating directories as completed work."""
        if not path.is_dir():
            return 0
        try:
            return sum(
                1
                for item in path.rglob("*")
                if item.is_file() and (modified_since is None or item.stat().st_mtime >= modified_since)
            )
        except OSError:
            return 0

    @staticmethod
    def _started_datetime(value: Any) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed

    @staticmethod
    def _elapsed_seconds(started_at: Any) -> int:
        value = PipelineJobService._started_datetime(started_at)
        if value is None:
            return 0
        return max(0, round((datetime.now(timezone.utc) - value).total_seconds()))

    def _reconcile_terminal_job_record(self, record: dict[str, Any]) -> bool:
        """Synchronize an inactive API job with a newer matching runner report.

        The pipeline report is the canonical result for its workspace.  This
        covers a supported recovery path where a failed App A job is resumed
        with the exact command from its job record.  It intentionally refuses
        reports without a matching ``scene_config`` or reports older than the
        recorded terminal outcome.
        """
        if record.get("status") in {"queued", "running"}:
            return False
        report_uri = record.get("report_uri")
        config_uri = record.get("config_uri")
        if not isinstance(report_uri, str) or not isinstance(config_uri, str):
            return False
        report_path = self._resolve_persisted_uri(report_uri, "report_uri")
        report = read_json(report_path, {}) if report_path is not None else {}
        if not isinstance(report, dict):
            return False
        status = report.get("status")
        if status not in {"verified", "unverified", "failed"}:
            return False
        report_config = report.get("scene_config")
        if not isinstance(report_config, str) or not report_config:
            return False
        try:
            report_config_path = Path(report_config)
            if report_config_path.is_absolute():
                report_config_path = report_config_path.resolve().relative_to(self.root.resolve())
            report_config_uri = report_config_path.as_posix()
        except (OSError, ValueError):
            return False
        if (
            self.repository_layout.rewrite_legacy_uri(report_config_uri)
            != self.repository_layout.rewrite_legacy_uri(Path(config_uri).as_posix())
        ):
            return False
        report_completed = self._started_datetime(report.get("completed_at"))
        recorded_completed = self._started_datetime(record.get("completed_at"))
        if report_completed is None or (recorded_completed is not None and report_completed <= recorded_completed):
            return False
        record["status"] = status
        record["final_status"] = status
        report_exit_code = report.get("exit_code")
        record["exit_code"] = (
            report_exit_code
            if isinstance(report_exit_code, int)
            else (0 if status == "verified" else 2)
        )
        record["started_at"] = report.get("started_at") or record.get("started_at")
        record["completed_at"] = report.get("completed_at")
        record["failure_reasons"] = report.get("failure_reasons", [])
        record["warnings"] = report.get("warnings", [])
        record["status_source"] = "pipeline_report"
        return True

    def _live_progress(
        self,
        record: dict[str, Any],
        config: dict[str, Any],
        report: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Derive a live, read-only progress sample from the active workspace.

        Pipeline reports are intentionally written at stage boundaries.  This
        fills the UI gap within a long stage without adding another persisted
        status document or changing the runner's output contract.
        """
        if record.get("status") not in {"queued", "running"}:
            return None
        job_id = str(record.get("job_id", ""))
        now = time.monotonic()
        cached = self._live_progress_cache.get(job_id)
        if cached and now - cached[0] < 4.0:
            return dict(cached[1])

        workspace_value = config.get("workspace")
        if not isinstance(workspace_value, str) or not workspace_value:
            return None
        workspace = self.root / workspace_value
        if self.root not in workspace.resolve().parents:
            return None
        source = {
            str(item.get("stage_id")): str(item.get("status"))
            for item in report.get("stages", [])
            if isinstance(item, dict)
        }

        def complete(stage_id: str) -> bool:
            return source.get(stage_id) in {"verified", "reused", "skipped"}

        input_manifest = read_json(workspace / "input_manifest.json", {})
        video = input_manifest.get("video", {}) if isinstance(input_manifest, dict) else {}
        projection = str(config.get("input_projection", "equirectangular_360"))
        preprocessing = config.get("preprocessing", {})
        interval = preprocessing.get("frame_interval_sec", 1.0) if isinstance(preprocessing, dict) else 1.0
        try:
            panorama_total = max(1, int(float(video.get("duration_sec", 0.0)) / float(interval)))
        except (TypeError, ValueError, ZeroDivisionError):
            panorama_total = max(1, self._file_count(workspace / "panoramas"))
        panorama_count = self._file_count(workspace / "panoramas")
        rig = config.get("virtual_rig", {})
        rig_path = self.root / str(rig.get("config", "")) if isinstance(rig, dict) else Path()
        rig_config = read_json(rig_path, {}) if rig_path.is_file() else {}
        try:
            camera_count = max(1, len(rig_config.get("pitches_deg", [])) * int(rig_config.get("yaw_steps", 1)))
        except (TypeError, ValueError):
            camera_count = 1
        image_count = self._file_count(workspace / "images")
        static_mask_count = self._file_count(workspace / "masks")
        started = self._started_datetime(record.get("started_at"))
        person_started_at = started.timestamp() if started else None
        person_mask_count = self._file_count(workspace / "masks_person", person_started_at)
        colmap_mask_count = self._file_count(workspace / "masks_person_colmap", person_started_at)

        stage_id: str
        label: str
        processed: int
        total: int
        if not complete("input_validation"):
            stage_id, label, processed, total = "input_validation", "入力動画の検証", 0, 1
        elif projection == "perspective" and not complete("preprocessing") and image_count < panorama_total:
            stage_id, label, processed, total = "panorama_extraction", "通常動画フレーム抽出", image_count, panorama_total
        elif projection == "perspective" and not complete("preprocessing"):
            stage_id, label, processed, total = "virtual_images", "単一カメラ幾何マスク生成", static_mask_count, max(1, image_count)
        elif not complete("preprocessing") and panorama_count < panorama_total:
            stage_id, label, processed, total = "panorama_extraction", "360パノラマ抽出", panorama_count, panorama_total
        elif not complete("preprocessing"):
            stage_id, label, processed, total = "virtual_images", "仮想画像・幾何マスク生成", min(image_count, static_mask_count), panorama_total * camera_count
        elif not complete("person_mask"):
            stage_id, label, processed, total = "person_masks", "人物マスク生成", min(person_mask_count, colmap_mask_count), max(1, image_count)
        elif not complete("sfm"):
            label = "単一カメラ SfM" if projection == "perspective" else "fixed-rig SfM"
            stage_id, processed, total = "fixed_rig_sfm", 0, 1
        elif bool(config.get("metric_marker", {}).get("enabled", True)) and not complete("metric"):
            stage_id, label, processed, total = "apriltag_metric_alignment", "AprilTag実寸scale・水平化", 0, 1
        elif not complete("lfs_export"):
            stage_id, label, processed, total = "lichtfeld_export", "LichtFeld Studio入力export", 0, 1
        else:
            stage_id, label, processed, total = "metric_reconstruction_package", "Metric Reconstruction Package検証", 0, 1

        stage_index = next((index for index, item in enumerate(DISPLAY_STAGES) if item[0] == stage_id), 0)
        stage_percent = min(100, round(100 * min(processed, total) / max(1, total)))
        sample = {
            "stage_id": stage_id,
            "label": label,
            "processed": processed,
            "total": total,
            "unit": "枚" if stage_id in {"panorama_extraction", "virtual_images", "person_masks"} else "工程",
            "stage_percent": stage_percent,
            "overall_percent": min(99, round(100 * (stage_index + min(processed, total) / max(1, total)) / len(DISPLAY_STAGES))),
            "elapsed_seconds": self._elapsed_seconds(record.get("started_at")),
            "completed_display_stage_count": stage_index,
        }
        self._live_progress_cache[job_id] = (now, sample)
        return dict(sample)

    def _enrich_job_record(self, record: dict[str, Any]) -> dict[str, Any]:
        """Attach persisted project/artifact pointers without copying artifacts."""
        config_path = self._canonicalize_record_uri(record, "config_uri")
        report_path = self._canonicalize_record_uri(record, "report_uri")
        self._canonicalize_record_uri(record, "log_uri")
        config = read_json(config_path, {}) if config_path is not None else {}
        if isinstance(config, dict):
            config = self._rewrite_legacy_config_uris(config)
        scene_id = str(record.get("scene_id", ""))
        reconstruction_id = str(
            config.get("reconstruction_id")
            if isinstance(config, dict) and config.get("reconstruction_id")
            else f"{scene_id}-fixed-rig"
        )
        mrp = self.repository_layout.metric_reconstructions / scene_id / reconstruction_id
        workspace_uri = config.get("workspace") if isinstance(config, dict) else None
        if isinstance(workspace_uri, str) and workspace_uri:
            record["workspace_uri"] = workspace_uri
            lfs_manifest_path = self.root / workspace_uri / "lfs_ready/manifest.json"
            if not lfs_manifest_path.is_file():
                lfs_manifest_path = mrp / "lfs_input/manifest.json"
            if lfs_manifest_path.is_file():
                lfs = read_json(lfs_manifest_path, {})
                if isinstance(lfs, dict):
                    lfs_input_path = lfs_manifest_path.parent.resolve()
                    record["lfs_input_uri"] = str(lfs_input_path.relative_to(self.root))
                    record["lfs_input_path"] = str(lfs_input_path)
                    distro = os.environ.get("WSL_DISTRO_NAME")
                    if distro:
                        explorer_suffix = str(lfs_input_path).replace("/", "\\")
                        record["lfs_input_explorer_path"] = f"\\\\wsl.localhost\\{distro}{explorer_suffix}"
                    record["lfs_export_status"] = lfs.get("status", "failed")
                    record["lfs_image_count"] = lfs.get("images", {}).get("file_count") if isinstance(lfs.get("images"), dict) else None
                    record["lfs_alpha_stats"] = lfs.get("mask", {}).get("alpha_stats", {}) if isinstance(lfs.get("mask"), dict) else {}
                    record["latest_valid_artifact"] = record["lfs_input_uri"] if lfs.get("status") == "verified" else record.get("latest_valid_artifact")
        if (mrp / "manifest.json").is_file():
            record["mrp_uri"] = str(mrp.relative_to(self.root))
            mrp_lfs = read_json(mrp / "lfs_input/manifest.json", {})
            record["mrp_lfs_status"] = mrp_lfs.get("status", "failed") if isinstance(mrp_lfs, dict) else "failed"
            if record["mrp_lfs_status"] == "verified" and not record.get("latest_valid_artifact"):
                record["latest_valid_artifact"] = record["mrp_uri"]
        project_name = str(record.get("project_name") or record.get("project") or scene_id)
        record["project_name"] = project_name
        record["project_id"] = str(record.get("project_id") or f"{scene_id}:{project_name}")
        report = read_json(report_path, {}) if report_path is not None else {}
        if isinstance(report, dict):
            stages = [item for item in report.get("stages", []) if isinstance(item, dict)]
            if stages:
                current = next((item for item in reversed(stages) if item.get("status") == "running"), stages[-1])
                completed = sum(1 for item in stages if item.get("status") in {"verified", "reused", "skipped"})
                record["current_stage"] = current.get("stage_id") or record.get("current_stage")
                record["completed_stage_count"] = completed
                record["total_stage_count"] = len(DISPLAY_STAGES)
                record["progress_percent"] = (
                    min(99, round(completed / len(DISPLAY_STAGES) * 100))
                    if record.get("status") in {"queued", "running"}
                    else (100 if record.get("status") == "verified" else round(completed / len(DISPLAY_STAGES) * 100))
                )
                record["last_stage_status"] = current.get("status")
        live_progress = self._live_progress(record, config, report) if isinstance(config, dict) and isinstance(report, dict) else None
        if live_progress:
            record["live_progress"] = live_progress
            record["current_stage"] = live_progress["stage_id"]
            record["completed_stage_count"] = live_progress["completed_display_stage_count"]
            record["total_stage_count"] = len(DISPLAY_STAGES)
            record["progress_percent"] = live_progress["overall_percent"]
        else:
            record.pop("live_progress", None)
        outputs = {}
        if isinstance(config, dict):
            sfm = config.get("sfm", {})
            if isinstance(sfm, dict):
                for key in ("model", "manifest", "trajectory"):
                    if sfm.get(key):
                        outputs[f"sfm_{key}"] = sfm[key]
        if record.get("workspace_uri"):
            outputs["workspace"] = record["workspace_uri"]
        if record.get("lfs_input_uri"):
            outputs["lfs_input"] = record["lfs_input_uri"]
        if record.get("mrp_uri"):
            outputs["mrp"] = record["mrp_uri"]
        if record.get("report_uri"):
            outputs["pipeline_report"] = record["report_uri"]
        if record.get("log_uri"):
            outputs["log"] = record["log_uri"]
        record["output_uris"] = outputs
        record["final_status"] = record.get("status")
        return record

    def projects(self) -> list[dict[str, Any]]:
        projects: dict[str, dict[str, Any]] = {}
        for job in self.jobs():
            enriched = self._enrich_job_record(dict(job))
            project_id = str(enriched.get("project_id"))
            candidate = {
                "project_id": project_id,
                "project_name": enriched.get("project_name"),
                "scene_id": enriched.get("scene_id"),
                "job_id": enriched.get("job_id"),
                "workspace_uri": enriched.get("workspace_uri"),
                "status": enriched.get("final_status", enriched.get("status")),
                "updated_at": enriched.get("completed_at") or enriched.get("started_at"),
                "lfs_input_uri": enriched.get("lfs_input_uri"),
                "lfs_input_path": enriched.get("lfs_input_path"),
                "lfs_input_explorer_path": enriched.get("lfs_input_explorer_path"),
                "mrp_uri": enriched.get("mrp_uri"),
            }
            previous = projects.get(project_id)
            if previous is None or str(candidate.get("updated_at") or "") >= str(previous.get("updated_at") or ""):
                projects[project_id] = candidate
        return sorted(projects.values(), key=lambda item: str(item.get("project_id")))

    def _allocate_project_name(self, requested: str, scene_id: str) -> str:
        """Return a deterministic non-colliding project name for a new job."""
        name = requested.strip() or scene_id
        if not PROJECT_NAME_RE.fullmatch(name):
            raise ValueError("invalid_project_name")
        existing_names = {
            str(item.get("project_name"))
            for item in self.jobs()
            if isinstance(item, dict) and item.get("project_name")
        }
        existing_names.update(path.name for path in self.projects_root.iterdir() if path.is_dir())
        if name not in existing_names:
            return name
        suffix = 2
        while f"{name}_{suffix}" in existing_names:
            suffix += 1
        return f"{name}_{suffix}"

    def _link_project_input(self, project_name: str, source: Path) -> str:
        """Attach a validated local input without duplicating video blocks."""
        if not source.is_file() or source.suffix.lower() not in VIDEO_SUFFIXES:
            raise ValueError("input_video_source_invalid")
        input_dir = self.projects_root / project_name / "input"
        input_dir.mkdir(parents=True, exist_ok=True)
        link = input_dir / source.name
        if link.exists() or link.is_symlink():
            link.unlink()
        try:
            resolved_source = source.resolve()
            if self.uploads_root in resolved_source.parents:
                try:
                    os.link(resolved_source, link)
                except OSError as error:
                    if error.errno != errno.EXDEV:
                        raise
                    relative_source = Path(os.path.relpath(resolved_source, link.parent))
                    link.symlink_to(relative_source)
            else:
                target = (
                    Path(os.path.relpath(resolved_source, link.parent))
                    if self.root in resolved_source.parents
                    else resolved_source
                )
                link.symlink_to(target)
        except OSError as error:
            link.unlink(missing_ok=True)
            raise ValueError(f"input_video_link_failed:{error}") from error
        return str(link.relative_to(self.root)).replace("\\", "/")

    def _validate_metric_marker_sampling(self, scene_id: str, payload: dict[str, Any]) -> None:
        """Reject marker-scale jobs whose sampling cannot meet the scene contract."""
        config = read_json(self._config_path(scene_id), {})
        if not isinstance(config, dict):
            raise ValueError("scene_config_not_object")
        marker = config.get("metric_marker", {})
        preprocessing = config.get("preprocessing", {})
        if not isinstance(marker, dict) or not isinstance(preprocessing, dict):
            raise ValueError("scene_config_metric_marker_invalid")
        marker_enabled = bool(payload.get("apriltag_enabled", marker.get("enabled", True)))
        if not marker_enabled:
            return
        try:
            interval = float(payload.get("frame_interval_sec", preprocessing.get("frame_interval_sec", 1.0)))
            maximum = float(preprocessing.get("max_frame_interval_for_metric_marker_sec", 1.0))
        except (TypeError, ValueError) as error:
            raise ValueError("frame_interval_invalid") from error
        if interval <= 0 or maximum <= 0:
            raise ValueError("frame_interval_invalid")
        if interval > maximum:
            raise ValueError(
                f"apriltag_frame_interval_exceeds_max:{interval:g}s>{maximum:g}s"
            )

    @staticmethod
    def _parse_fixed_tag_ids(value: Any) -> list[int]:
        """Parse one or more same-family fixed AprilTag IDs from the UI."""
        if isinstance(value, str):
            raw_values: list[Any] = [item.strip() for item in value.split(",")]
        elif isinstance(value, list):
            raw_values = value
        else:
            raise ValueError("tag_ids_invalid")
        if not raw_values or len(raw_values) > 32:
            raise ValueError("tag_ids_invalid")
        result: list[int] = []
        for raw in raw_values:
            if isinstance(raw, bool) or not isinstance(raw, (str, int)):
                raise ValueError("tag_ids_invalid")
            if isinstance(raw, str) and not re.fullmatch(r"[0-9]+", raw):
                raise ValueError("tag_ids_invalid")
            try:
                tag_id = int(raw)
            except (TypeError, ValueError) as error:
                raise ValueError("tag_ids_invalid") from error
            # The camera-side/Viewer contract reserves tag25h9 IDs 0-9 for
            # fixed scene anchors.  IDs 10-19 are reserved and 20+ identify
            # tags attached to dynamic boxes, so neither may enter an MRP.
            if tag_id < 0 or tag_id > 9 or tag_id in result:
                raise ValueError("tag_ids_invalid")
            result.append(tag_id)
        return result

    def _write_job_config(
        self,
        job_id: str,
        scene_template_id: str,
        payload: dict[str, Any],
        *,
        logical_scene_id: str | None = None,
    ) -> Path:
        source_path = self._config_path(scene_template_id)
        config = read_json(source_path)
        if not isinstance(config, dict):
            raise ValueError("scene_config_not_object")
        if logical_scene_id is not None:
            if not SCENE_ID_RE.fullmatch(logical_scene_id):
                raise ValueError("invalid_scene_id")
            config["scene_template_id"] = scene_template_id
            config["scene_id"] = logical_scene_id
        effective_scene_id = logical_scene_id or scene_template_id
        config.setdefault("person_mask", {})["enabled"] = True
        # A new external video must never reuse the scene's verified workspace.
        # Generated project artifacts live independently of transient job
        # metadata.  This keeps one durable workspace per project while jobs
        # only retain their config, report pointer, and log.
        if payload.get("workspace") in (None, ""):
            payload = dict(payload)
            project = str(payload.get("project", "")).strip()
            if project and not PROJECT_NAME_RE.fullmatch(project):
                raise ValueError("invalid_project_name")
            project_root = project or effective_scene_id
            payload["workspace"] = f"var/spatial/workspaces/projects/{project_root}/reconstruction"
        allowed = {
            "apriltag_enabled": ("metric_marker", "enabled"),
            "input_video": ("input_video",),
            "tag_family": ("metric_marker", "family"),
            "tag_id": ("metric_marker", "id"),
            "tag_size_m": ("metric_marker", "size_m"),
            "person_mask_profile": ("person_mask", "config"),
            "person_mask_enabled": ("person_mask", "enabled"),
            "colmap_profile": ("sfm", "profile"),
            "frame_interval_sec": ("preprocessing", "frame_interval_sec"),
        }
        requested_projection = payload.get("input_projection")
        if requested_projection in (None, ""):
            projection = str(config.get("input_projection", "equirectangular_360"))
        else:
            projection = str(requested_projection)
        if projection not in {"equirectangular_360", "perspective"}:
            raise ValueError("input_projection_invalid")
        config["input_projection"] = projection
        virtual_rig_layout = payload.get("virtual_rig_layout")
        if projection == "equirectangular_360" and virtual_rig_layout not in (None, ""):
            virtual_rig_config = VIRTUAL_RIG_LAYOUT_CONFIGS.get(str(virtual_rig_layout))
            if virtual_rig_config is None:
                raise ValueError("virtual_rig_layout_invalid")
            # The layout is a per-job input.  Point to a versioned immutable
            # config instead of changing the shared 12-face default.
            config.setdefault("virtual_rig", {})["config"] = virtual_rig_config
        if payload.get("person_mask_enabled") is False:
            raise ValueError("person_mask_required")
        for name, keys in allowed.items():
            if name not in payload or payload[name] in (None, ""):
                continue
            value: Any = payload[name]
            if name in {"input_video", "person_mask_profile"}:
                value = safe_relative(str(value), name)
            if name == "person_mask_profile" and value not in APPROVED_PERSON_MASK_CONFIGS:
                raise ValueError("person_mask_profile_not_approved")
            if name == "tag_family":
                value = require_supported_apriltag_family(value)
            if name == "colmap_profile" and value not in {"fixed_rig", "perspective", "external"}:
                raise ValueError("colmap_profile_invalid")
            if name in {"person_mask_enabled", "apriltag_enabled"}:
                value = bool(value)
            if name == "tag_id":
                value = int(value)
            if name == "tag_size_m":
                value = float(value)
            target: dict[str, Any] = config
            for key in keys[:-1]:
                target = target.setdefault(key, {})
            target[keys[-1]] = value

        # The input projection chooses the reconstruction topology.  Do not
        # let a stale UI value pair a normal camera with the virtual fixed rig.
        if requested_projection not in (None, ""):
            config.setdefault("sfm", {})["profile"] = (
                "perspective" if projection == "perspective" else "fixed_rig"
            )

        if payload.get("tag_ids") not in (None, ""):
            tag_ids = self._parse_fixed_tag_ids(payload["tag_ids"])
            marker = config.setdefault("metric_marker", {})
            family = marker.get("family")
            try:
                size_m = float(marker.get("size_m"))
            except (TypeError, ValueError) as error:
                raise ValueError("tag_size_m_invalid") from error
            try:
                family = require_supported_apriltag_family(family)
            except ValueError as error:
                raise ValueError("metric_marker_invalid:" + str(error)) from error
            if not math.isfinite(size_m) or size_m <= 0:
                raise ValueError("metric_marker_invalid")
            marker["markers"] = [
                {"type": "apriltag", "family": family, "id": tag_id, "size_m": size_m}
                for tag_id in tag_ids
            ]
            # Legacy fields remain the primary-tag compatibility view for
            # existing tooling. New pipeline stages consume ``markers``.
            marker["id"] = tag_ids[0]

        if payload.get("workspace") not in (None, ""):
            workspace = safe_relative(str(payload["workspace"]), "workspace")
            project_name = str(payload.get("project") or effective_scene_id).strip()
            if not PROJECT_NAME_RE.fullmatch(project_name):
                raise ValueError("invalid_project_name")
            expected_workspace = (
                Path("var/spatial/workspaces/projects")
                / project_name
                / "reconstruction"
            ).as_posix()
            if workspace != expected_workspace:
                raise ValueError("workspace_must_match_project_boundary")
            config["workspace"] = workspace
            # A new Application A project owns a distinct reconstruction.
            # Keep the scene as the package namespace and use the collision-
            # resolved project name as the reconstruction id.
            reconstruction_suffix = "perspective" if projection == "perspective" else "fixed-rig"
            config["reconstruction_id"] = f"{project_name}-{reconstruction_suffix}"
            workspace_root = Path(workspace)
            config.setdefault("preprocessing", {})["panoramas"] = (workspace_root / "panoramas").as_posix()
            config.setdefault("virtual_rig", {})["render_manifest"] = (workspace_root / "render_manifest.json").as_posix()
            person_mask = config.setdefault("person_mask", {})
            for key, suffix in (("static_masks", "masks"), ("person_masks", "masks_person"), ("colmap_masks", "masks_person_colmap"), ("report", "mask_quality.json")):
                person_mask[key] = (workspace_root / suffix).as_posix()
            sfm = config.setdefault("sfm", {})
            sfm.update({
                "model": (workspace_root / "colmap_sequential" / "text").as_posix(),
                "manifest": (workspace_root / "colmap_sequential" / "manifest.json").as_posix(),
                "images": (workspace_root / "images").as_posix(),
                "masks": (workspace_root / "masks_person_colmap").as_posix(),
                "rig_config": (workspace_root / "rig_config.json").as_posix(),
                "workspace": (workspace_root / "colmap_sequential").as_posix(),
            })
            metric = config.setdefault("metric_marker", {})
            metric["world_alignment"] = (workspace_root / "world_alignment.json").as_posix()
            metric["corners_world"] = (workspace_root / "apriltag_corners_world.json").as_posix()
            metric["quality"] = (workspace_root / "apriltag" / "quality.json").as_posix()
            dense = config.setdefault("dense", {})
            dense["workspace"] = (workspace_root / "proxy_dense" / "dense").as_posix()
            dense["profile"] = "full"
            dense["world_alignment"] = (workspace_root / "world_alignment.json").as_posix()
            # A job-specific workspace represents a new reconstruction.  Do
            # not silently reuse artifacts from the selected scene.
            config["reuse"] = {
                **dict(config.get("reuse", {})),
                "person_mask": False,
                "sfm": False,
                "spz": False,
                "dense": False,
                "proxy": False,
            }
            execution_profiles = config.setdefault("execution", {}).setdefault("profiles", {})
            for profile_settings in execution_profiles.values():
                if isinstance(profile_settings, dict):
                    for key in ("sfm", "spz", "dense", "proxy"):
                        profile_settings[f"reuse_{key}"] = False

        project_name = str(payload.get("project") or effective_scene_id).strip()
        if not PROJECT_NAME_RE.fullmatch(project_name):
            raise ValueError("invalid_project_name")
        external_video = payload.get("input_video_path")
        if external_video not in (None, ""):
            source = self._resolve_external_path(str(external_video))
            if not source.is_file():
                raise ValueError("input_video_path_not_found")
            if source.suffix.lower() not in {".mp4", ".mov", ".mkv"}:
                raise ValueError("input_video_extension_invalid")
            config["input_video"] = self._link_project_input(project_name, source)
        elif payload.get("input_video") not in (None, ""):
            uploaded = self.root / safe_relative(str(config["input_video"]), "input_video")
            if self.uploads_root in uploaded.resolve().parents:
                config["input_video"] = self._link_project_input(project_name, uploaded)

        path = self.jobs_root / f"{job_id}.scene.json"
        atomic_json(path, config)
        return path

    @staticmethod
    def _resolve_external_path(value: str) -> Path:
        """Resolve Linux, WSL and Windows absolute paths without shell parsing."""
        if WINDOWS_ABSOLUTE_PATH_RE.match(value):
            drive = value[0].lower()
            remainder = value[2:].replace("\\", "/").lstrip("/")
            return Path(f"/mnt/{drive}") / remainder
        unc_match = WINDOWS_WSL_UNC_PATH_RE.match(value)
        if unc_match:
            return Path("/") / unc_match.group("path").replace("\\", "/")
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("input_video_path_must_be_absolute")
        return path

    def uploaded_videos(self) -> list[dict[str, Any]]:
        """Return reusable browser uploads without copying them into a project."""
        return list_uploaded_videos(self.root, self.uploads_root, VIDEO_SUFFIXES)

    def store_uploaded_video(self, filename: str, stream: Any, content_length: int) -> dict[str, Any]:
        """Content-address browser uploads so the same video is stored once."""
        result = store_uploaded_video(
            root=self.root,
            upload_content_root=self.upload_content_root,
            upload_staging_root=self.upload_staging_root,
            suffixes=VIDEO_SUFFIXES,
            filename=filename,
            stream=stream,
            content_length=content_length,
            require_headroom=self._require_storage_headroom,
        )
        result["warnings"] = self._refresh_repository_catalog()
        return result

    @staticmethod
    def _windows_path(path: Path) -> str:
        """Return a Windows-accessible UNC path for a repository-local file."""
        distro = os.environ.get("WSL_DISTRO_NAME")
        if not distro:
            raise ValueError("windows_file_picker_unavailable")
        explorer_suffix = str(path.resolve()).replace("/", "\\")
        return f"\\\\wsl.localhost\\{distro}{explorer_suffix}"

    def open_lfs_folder(self, job_id: str) -> dict[str, Any]:
        job = self._job(job_id)
        value = job.get("lfs_input_path")
        path = Path(str(value)) if value else None
        if path is None or not path.is_dir() or self.root not in path.resolve().parents:
            raise ValueError("lfs_input_not_available")
        bridge_path = self.repository_layout.locks / "open-folder-bridge.sock"
        bridge_failure: str | None = None
        if bridge_path.exists():
            try:
                relative = path.resolve().relative_to(self.root).as_posix()
                payload = json.dumps({"command": "open", "path": relative}) + "\n"
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                    connection.settimeout(5)
                    connection.connect(str(bridge_path))
                    connection.sendall(payload.encode("utf-8"))
                    response = bytearray()
                    while not response.endswith(b"\n") and len(response) <= 8192:
                        chunk = connection.recv(8193 - len(response))
                        if not chunk:
                            break
                        response.extend(chunk)
                result = json.loads(response.decode("utf-8"))
                if isinstance(result, dict) and result.get("opened") is True:
                    return result
                bridge_failure = str(
                    result.get("failure_reason", "open_folder_bridge_failed")
                    if isinstance(result, dict)
                    else "open_folder_bridge_response_invalid"
                )
            except (OSError, ValueError, json.JSONDecodeError) as error:
                bridge_failure = str(error)
        distro = os.environ.get("WSL_DISTRO_NAME")
        explorer_path = self._windows_path(path) if distro else str(path)
        executable = shutil.which("explorer.exe")
        if not executable or not distro:
            return {
                "status": "manual",
                "path": explorer_path,
                "opened": False,
                "failure_reason": bridge_failure or "explorer_bridge_not_available",
            }
        subprocess.Popen(
            [executable, explorer_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        return {"status": "verified", "path": explorer_path, "opened": True}

    def start_job(self, payload: dict[str, Any], *, resume: bool = False, reuse: bool = False) -> dict[str, Any]:
        self._require_storage_headroom(0, "pipeline_start")
        scene_id = str(payload.get("scene_id", ""))
        scene_template_id = str(payload.get("scene_template_id") or scene_id)
        if not SCENE_ID_RE.fullmatch(scene_id):
            raise ValueError("invalid_scene_id")
        active = self._active_job_for_scene(None)
        requested_job_id = str(payload.get("job_id") or "")
        if active and str(active.get("job_id")) != requested_job_id:
            raise ValueError(f"job_already_running:{active.get('job_id')}")
        stored_config_uri = payload.get("config_uri")
        if stored_config_uri not in (None, ""):
            config_path = self._resolve_persisted_uri(stored_config_uri, "config_uri")
            if config_path is None or not config_path.is_file():
                raise FileNotFoundError("job_config_not_found")
        else:
            config_path = self._config_path(scene_template_id)
        is_new_project = stored_config_uri in (None, "")
        if is_new_project:
            self._validate_metric_marker_sampling(scene_template_id, payload)
        job_id = uuid.uuid4().hex[:12]
        resume_config_provenance: dict[str, Any] | None = None
        if not is_new_project:
            config_path, resume_config_provenance = self._materialize_resume_config(
                job_id,
                config_path,
            )
        job_dir = self.jobs_root / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        requested_project_name = str(payload.get("project") or scene_id)
        project_name = (
            self._allocate_project_name(requested_project_name, scene_id)
            if is_new_project
            else str(payload.get("project_name") or payload.get("project") or scene_id)
        )
        effective_payload = dict(payload)
        if is_new_project:
            effective_payload["project"] = project_name
        configurable = {
            "input_video", "input_video_path", "apriltag_enabled", "tag_family", "tag_id", "tag_ids", "tag_size_m",
            "person_mask_profile", "person_mask_enabled", "colmap_profile", "frame_interval_sec", "workspace", "project",
            "input_projection", "virtual_rig_layout", "scene_template_id",
        }
        if is_new_project and configurable.intersection(payload):
            config_path = self._write_job_config(
                job_id,
                scene_template_id,
                effective_payload,
                logical_scene_id=scene_id if "scene_template_id" in payload else None,
            )
        profile = str(payload.get("profile", "quality"))
        if profile not in {"fast", "quality", "auto"}:
            raise ValueError("invalid_profile")
        command = [
            sys.executable,
            str(self.root / "generation_pipeline/run_proxy_geometry_pipeline.py"),
            "--scene-config", str(config_path),
            "--repo-root", str(self.root),
            "--profile", profile,
        ]
        if resume or bool(payload.get("resume")):
            command.append("--resume")
        if reuse:
            command.extend(["--reuse-sfm", "--reuse-spz", "--reuse-dense", "--reuse-proxy"])
        if bool(payload.get("dry_run")):
            command.append("--dry-run")
        stop_after = payload.get("stop_after") or "metric_reconstruction_package"
        if payload.get("apriltag_enabled") is False:
            # No physical marker means no honest meter-scale package.  The
            # horizontalized LFS input remains useful and is the boundary.
            stop_after = "lfs_export"
        if stop_after not in (None, ""):
            if stop_after not in STOP_AFTER_STAGES:
                raise ValueError("invalid_stop_after")
            command.extend(["--stop-after", str(stop_after)])
        configured = read_json(config_path, {})
        if isinstance(configured, dict) and isinstance(configured.get("workspace"), str):
            report_path = self.root / configured["workspace"] / "pipeline_report.json"
        else:
            report_path = (
                self.repository_layout.legacy_workspaces
                / scene_id
                / "pipeline_report.json"
            )
        log_path = job_dir / "job.log"
        record: dict[str, Any] = {
            "schema_version": "1.0.0",
            "job_id": job_id,
            "scene_id": scene_id,
            "scene_template_id": scene_template_id,
            "status": "queued",
            "started_at": None,
            "completed_at": None,
            "exit_code": None,
            "command": command,
            "log_uri": str(log_path.relative_to(self.root)),
            "report_uri": str(report_path.relative_to(self.root)),
            "config_uri": str(config_path.relative_to(self.root)),
            "failure_reasons": [],
            "warnings": [],
            "profile": profile,
            "stop_after": stop_after,
            "project_name": project_name,
            "project_id": f"{scene_id}:{project_name}",
            "workspace_uri": str(configured.get("workspace")) if isinstance(configured, dict) and configured.get("workspace") else None,
            "current_stage": "queued",
            "final_status": "queued",
            "latest_valid_artifact": None,
            "mrp_uri": None,
            "lfs_input_uri": None,
            "resume": {"requested": bool(resume or payload.get("resume")), "reuse": bool(reuse)},
        }
        if resume_config_provenance is not None:
            record["resume_config_provenance"] = resume_config_provenance
        if is_new_project and project_name != requested_project_name:
            record["requested_project_name"] = requested_project_name
        self._write_job(record)
        log_handle = log_path.open("w", encoding="utf-8")
        process = subprocess.Popen(command, cwd=self.root, stdout=log_handle, stderr=subprocess.STDOUT, text=True)
        record["status"] = "running"
        record["pid"] = process.pid
        record["started_at"] = utc_now()
        self._write_job(record)
        with self._lock:
            self._processes[job_id] = process
        threading.Thread(target=self._watch_job, args=(job_id, process, log_handle), daemon=True).start()
        return record

    def _watch_job(self, job_id: str, process: subprocess.Popen[str], log_handle: Any) -> None:
        exit_code = process.wait()
        log_handle.close()
        self._refresh_process(job_id, process, exit_code)

    def _refresh_process(self, job_id: str, process: subprocess.Popen[str], exit_code: int | None = None) -> None:
        record = read_json(self._job_path(job_id), {})
        if not isinstance(record, dict) or record.get("status") not in {"running", "queued"}:
            return
        code = process.poll() if exit_code is None else exit_code
        if code is None:
            return
        report_path = self._resolve_persisted_uri(record.get("report_uri"), "report_uri")
        report = read_json(report_path, {}) if report_path is not None else {}
        record["exit_code"] = code
        record["completed_at"] = utc_now()
        report_status = report.get("status") if isinstance(report, dict) else None
        record["status"] = report_status if report_status in {"verified", "unverified", "failed"} else "failed"
        record["failure_reasons"] = report.get("failure_reasons", []) if isinstance(report, dict) else []
        record["warnings"] = report.get("warnings", []) if isinstance(report, dict) else []
        record["final_status"] = record["status"]
        if isinstance(report, dict):
            stages = [item for item in report.get("stages", []) if isinstance(item, dict)]
            if stages:
                record["current_stage"] = stages[-1].get("stage_id") or stages[-1].get("stage")
        enriched = self._enrich_job_record(record)
        if enriched.get("lfs_export_status") == "verified":
            enriched["latest_valid_artifact"] = enriched.get("lfs_input_uri")
        scene_id = str(record.get("scene_id", ""))
        config_path = self._resolve_persisted_uri(record.get("config_uri"), "config_uri")
        config = read_json(config_path, {}) if config_path is not None else {}
        if isinstance(config, dict):
            config = self._rewrite_legacy_config_uris(config)
        reconstruction_id = str(config.get("reconstruction_id") if isinstance(config, dict) and config.get("reconstruction_id") else f"{scene_id}-fixed-rig")
        mrp = self.repository_layout.metric_reconstructions / scene_id / reconstruction_id
        if (mrp / "manifest.json").is_file():
            validation = validate_metric_reconstruction_package(
                mrp,
                scene_id,
                reconstruction_id,
            )
            if validation.get("status") == "verified":
                enriched["mrp_uri"] = str(mrp.relative_to(self.root))
                enriched["latest_valid_artifact"] = enriched["mrp_uri"]
        record = enriched
        self._write_job(record)
        self._processes.pop(job_id, None)
        self._live_progress_cache.pop(job_id, None)

    def job_report(self, job_id: str) -> dict[str, Any]:
        job = self._job(job_id)
        report_path = self._resolve_persisted_uri(job.get("report_uri"), "report_uri")
        report = read_json(report_path, None) if report_path is not None else None
        return report if isinstance(report, dict) else {"status": "not_available", "job": job}

    def job_stages(self, job_id: str) -> list[dict[str, Any]]:
        report = self.job_report(job_id)
        source = {stage.get("stage_id"): stage for stage in report.get("stages", []) if isinstance(stage, dict)}
        job = self._job(job_id)
        config_path = self._resolve_persisted_uri(job.get("config_uri"), "config_uri")
        config = read_json(config_path, {}) if config_path is not None else {}
        if isinstance(config, dict):
            config = self._rewrite_legacy_config_uris(config)
        projection = (
            str(config.get("input_projection", "equirectangular_360"))
            if isinstance(config, dict)
            else "equirectangular_360"
        )
        perspective_labels = {
            "panorama_extraction": "video frame extraction",
            "virtual_images": "full-frame validity mask generation",
            "fixed_rig_sfm": "single-camera SfM",
        }
        result = []
        for stage_id, label in DISPLAY_STAGES:
            if projection == "perspective":
                label = perspective_labels.get(stage_id, label)
            source_id = SOURCE_STAGE[stage_id]
            stage = source.get(source_id)
            status = stage.get("status") if stage else "not_run"
            if stage_id in {"panorama_extraction", "virtual_images"} and stage:
                status = stage.get("status")
            if stage_id == "lichtfeld_export":
                workspace_uri = job.get("workspace_uri")
                lfs_manifest = self.root / str(workspace_uri or "") / "lfs_ready/manifest.json"
                if not lfs_manifest.is_file():
                    lfs_manifest = self.root / str(job.get("lfs_input_uri") or "") / "manifest.json"
                lfs = read_json(lfs_manifest, {})
                status = lfs.get("status") if isinstance(lfs, dict) else "not_run"
                if status not in {"verified", "failed", "unverified"}:
                    status = "not_run"
            result.append({"stage_id": stage_id, "label": label, "status": status, "source_stage": source_id})
        return result

    def job_log(self, job_id: str) -> dict[str, Any]:
        job = self._job(job_id)
        path = self._resolve_persisted_uri(job.get("log_uri"), "log_uri")
        content = b""
        if path is not None and path.is_file():
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - 200_000), os.SEEK_SET)
                content = handle.read(200_000)
        return {
            "job_id": job_id,
            "log_uri": job.get("log_uri"),
            "text": content.decode("utf-8", errors="replace"),
        }

    def mrps(self) -> list[dict[str, Any]]:
        """List MRP candidates without claiming they are currently verified.

        Full MRP validation decodes every exported RGBA image.  It is run for
        the selected package by ``validate_mrp`` and again immediately before
        SPZ registration, rather than blocking the App B landing screen or
        presenting a stale manifest status as a verification result.
        """
        result = []
        root = self.repository_layout.metric_reconstructions
        for manifest_path in sorted(root.glob("*/*/manifest.json")):
            manifest = read_json(manifest_path, {})
            if not isinstance(manifest, dict):
                continue
            package = manifest_path.parent
            result.append({
                "scene_id": manifest.get("scene_id"),
                "reconstruction_id": manifest.get("reconstruction_id"),
                "package_uri": str(package.relative_to(self.root)),
                "manifest": manifest,
                "status": "not_validated",
                "validation": None,
            })
        return sorted(result, key=lambda item: str((item.get("manifest") or {}).get("created_at", "")), reverse=True)

    def mrp_manifest(self, scene_id: str, reconstruction_id: str) -> dict[str, Any]:
        if not SCENE_ID_RE.fullmatch(scene_id) or not SCENE_ID_RE.fullmatch(reconstruction_id):
            raise ValueError("invalid_mrp_id")
        package = self.repository_layout.metric_reconstructions / scene_id / reconstruction_id
        manifest = read_json(package / "manifest.json")
        if not isinstance(manifest, dict):
            raise FileNotFoundError("mrp_manifest_not_found")
        validation = validate_metric_reconstruction_package(
            package,
            scene_id,
            reconstruction_id,
            source_root=self.root,
            source_verification="presence",
        )
        return {"manifest": manifest, "validation": validation}

    def validate_mrp(self, scene_id: str, reconstruction_id: str) -> dict[str, Any]:
        return self.mrp_manifest(scene_id, reconstruction_id)["validation"]
