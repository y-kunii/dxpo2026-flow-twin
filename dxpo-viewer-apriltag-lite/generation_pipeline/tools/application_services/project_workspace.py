"""Project workspace, scene configuration, and AprilTag setup service extracted from the PreviewService compatibility facade."""

from __future__ import annotations

from .common import *  # noqa: F403


class ProjectWorkspaceService:
    @staticmethod
    def _repair_workspace_ownership(path: Path) -> None:
        """Repair Docker-created ownership only inside an approved workspace.

        The cleanup gate has already established that ``path`` is a hidden,
        unreferenced rejected project below ``projects_root`` before this
        helper can be called. ``sudo -n`` keeps the local API non-interactive
        and fails closed on a host without the documented local permission.
        """
        result = subprocess.run(
            ["sudo", "-n", "chown", "-R", f"{os.getuid()}:{os.getgid()}", str(path)],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()[:240]
            raise PermissionError(f"workspace_ownership_repair_failed:{detail or result.returncode}")

    def _raw_job_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in sorted(self.jobs_root.glob("*.json")):
            if path.name.endswith(".scene.json"):
                continue
            record = read_json(path, {})
            if isinstance(record, dict) and isinstance(record.get("job_id"), str):
                records.append(record)
        return records

    @staticmethod
    def _record_project_name(record: dict[str, Any]) -> str | None:
        project_name = record.get("project_name")
        if isinstance(project_name, str) and PROJECT_NAME_RE.fullmatch(project_name):
            return project_name
        return project_name_from_workspace_uri(record.get("workspace_uri"))

    def _project_has_active_job(self, project_name: str, records: list[dict[str, Any]]) -> bool:
        for record in records:
            if self._record_project_name(record) != project_name:
                continue
            if record.get("status") not in {"queued", "running"}:
                continue
            job_id = record.get("job_id")
            process = self._processes.get(job_id) if isinstance(job_id, str) else None
            if process is not None and process.poll() is None:
                return True
            if self._pid_is_alive(record.get("pid")):
                return True
        return False

    def project_workspaces(self) -> list[dict[str, Any]]:
        """Inventory durable App A workspaces and expose a fail-safe cleanup gate.

        A project is removable only when every known job is terminal and
        rejected, and there is no MRP or Scene Package associated with it.
        This deliberately keeps successful, published, unknown, and active
        projects out of the destructive path.
        """
        records = self._raw_job_records()
        records_by_project: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            project_name = self._record_project_name(record)
            if project_name:
                records_by_project.setdefault(project_name, []).append(record)

        mrp_by_project: dict[str, list[dict[str, Any]]] = {}
        mrp_project_by_identity: dict[tuple[str, str], str] = {}
        for mrp in self.mrps():
            manifest = mrp.get("manifest", {})
            provenance = manifest.get("provenance", {}) if isinstance(manifest, dict) else {}
            workspace_uri = provenance.get("source_workspace_uri") if isinstance(provenance, dict) else None
            project_name = project_name_from_workspace_uri(workspace_uri)
            scene_id = mrp.get("scene_id")
            reconstruction_id = mrp.get("reconstruction_id")
            if project_name:
                mrp_by_project.setdefault(project_name, []).append(mrp)
                if isinstance(scene_id, str) and isinstance(reconstruction_id, str):
                    mrp_project_by_identity[(scene_id, reconstruction_id)] = project_name

        packages_by_project: dict[str, list[dict[str, Any]]] = {}
        for package in self.scene_packages():
            project_name = package.get("project_name")
            if not isinstance(project_name, str) or not PROJECT_NAME_RE.fullmatch(project_name):
                scene_id = package.get("scene_id")
                reconstruction_id = package.get("reconstruction_id")
                if isinstance(scene_id, str) and isinstance(reconstruction_id, str):
                    project_name = mrp_project_by_identity.get((scene_id, reconstruction_id))
            if isinstance(project_name, str) and PROJECT_NAME_RE.fullmatch(project_name):
                packages_by_project.setdefault(project_name, []).append(package)

        project_names = {path.name for path in self.projects_root.iterdir() if path.is_dir() and not path.name.startswith(".")}
        project_names.update(records_by_project)
        result: list[dict[str, Any]] = []
        for project_name in sorted(project_names):
            project_root = self.projects_root / project_name
            project_records = records_by_project.get(project_name, [])
            project_records.sort(key=lambda item: str(item.get("completed_at") or item.get("started_at") or ""), reverse=True)
            mrps = mrp_by_project.get(project_name, [])
            packages = packages_by_project.get(project_name, [])
            active = self._project_has_active_job(project_name, project_records)
            statuses = {str(item.get("final_status", item.get("status", ""))) for item in project_records}
            if active:
                lifecycle = "running"
                retention_reason = "実行中または再開可能な処理があるため削除できません。"
            elif mrps or packages:
                lifecycle = "retained"
                retention_reason = "MRPまたはScene Packageが参照しているため保持します。"
            elif project_records and statuses and statuses.issubset({"failed", "unverified"}):
                lifecycle = "rejected"
                retention_reason = "MRP・Scene Package未生成の品質不合格プロジェクトです。完全削除できます。"
            elif any(status == "verified" for status in statuses):
                lifecycle = "retained"
                retention_reason = "検証済みの処理履歴があるため自動削除しません。"
            else:
                lifecycle = "review"
                retention_reason = "参照関係を自動判定できないため削除しません。"
            result.append({
                "project_name": project_name,
                "project_uri": str(project_root.relative_to(self.root)) if project_root.exists() else None,
                "size_bytes": self._directory_size_bytes(project_root),
                "lifecycle": lifecycle,
                "retention_reason": retention_reason,
                "removable": lifecycle == "rejected" and project_root.is_dir(),
                "active_job": active,
                "job_ids": [item.get("job_id") for item in project_records if isinstance(item.get("job_id"), str)],
                "last_job": project_records[0] if project_records else None,
                "mrp_count": len(mrps),
                "scene_package_count": len(packages),
            })
        return result

    def delete_rejected_project(self, project_name: str) -> dict[str, Any]:
        """Permanently remove only a terminal, unreferenced failed workspace."""
        if not PROJECT_NAME_RE.fullmatch(project_name):
            raise ValueError("invalid_project_name")
        inventory = {item["project_name"]: item for item in self.project_workspaces()}
        project = inventory.get(project_name)
        if project is None or not project.get("removable"):
            lifecycle = project.get("lifecycle") if isinstance(project, dict) else "not_found"
            raise ValueError(f"project_delete_not_allowed:{lifecycle}")
        project_root = self.projects_root / project_name
        if not project_root.is_dir() or project_root.resolve().parent != self.projects_root.resolve():
            raise ValueError("project_delete_path_invalid")

        records = [record for record in self._raw_job_records() if self._record_project_name(record) == project_name]
        if self._project_has_active_job(project_name, records):
            raise ValueError("project_delete_active_job")
        job_ids = [str(record["job_id"]) for record in records if isinstance(record.get("job_id"), str) and JOB_ID_RE.fullmatch(str(record["job_id"]))]
        candidate_configs = {str(record.get("config_uri")) for record in records if isinstance(record.get("config_uri"), str)}
        remaining_configs = {
            str(record.get("config_uri"))
            for record in self._raw_job_records()
            if self._record_project_name(record) != project_name and isinstance(record.get("config_uri"), str)
        }
        freed_bytes = self._directory_size_bytes(project_root)
        transient = self.projects_root / f".{project_name}.deleting-{uuid.uuid4().hex[:8]}"
        os.replace(project_root, transient)
        ownership_repaired = False
        try:
            shutil.rmtree(transient)
        except PermissionError:
            try:
                self._repair_workspace_ownership(transient)
                ownership_repaired = True
                shutil.rmtree(transient)
            except Exception:
                if transient.exists() and not project_root.exists():
                    os.replace(transient, project_root)
                raise
        except Exception:
            if transient.exists() and not project_root.exists():
                os.replace(transient, project_root)
            raise

        for job_id in job_ids:
            self._job_path(job_id).unlink(missing_ok=True)
            job_directory = self.jobs_root / job_id
            if job_directory.is_dir() and job_directory.resolve().parent == self.jobs_root.resolve():
                shutil.rmtree(job_directory)
        for config_uri in candidate_configs - remaining_configs:
            try:
                config_path = self.root / safe_relative(config_uri, "job_config_uri")
            except ValueError:
                continue
            if config_path.parent == self.jobs_root and config_path.name.endswith(".scene.json"):
                config_path.unlink(missing_ok=True)
        self._refresh_repository_catalog()
        return {
            "status": "verified",
            "operation": "deleted_rejected_project",
            "project_name": project_name,
            "freed_bytes": freed_bytes,
            "removed_job_ids": job_ids,
            "warnings": [
                "MRP、Scene Package、元動画は削除対象に含めません。",
                *(["過去のDocker生成物の所有権を修復してから削除しました。"] if ownership_repaired else []),
            ],
        }

    def _config_path(self, scene_id: str) -> Path:
        if not SCENE_ID_RE.fullmatch(scene_id):
            raise ValueError("invalid_scene_id")
        path = self.root / "generation_pipeline/config/scenes" / f"{scene_id}.json"
        if not path.is_file():
            raise FileNotFoundError(f"scene_config_not_found:{scene_id}")
        return path

    def scene_configs(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for path in sorted((self.root / "generation_pipeline/config/scenes").glob("*.json")):
            config = read_json(path, {})
            if not isinstance(config, dict):
                continue
            result.append({
                "scene_id": config.get("scene_id", path.stem),
                "config_uri": str(path.relative_to(self.root)),
                "status": "available",
                "config": config,
            })
        return result

    @staticmethod
    def apriltag_families() -> dict[str, Any]:
        """Expose the detector-backed family choices used by App A."""
        return apriltag_family_catalog()

    def detect_apriltag_frame(
        self,
        frame: bytes,
        *,
        family: str,
        tag_size_m: str,
        fx: str,
        fy: str,
        cx: str,
        cy: str,
        distortion_coefficients: str = "",
    ) -> dict[str, Any]:
        """Detect one transient browser-camera frame without persisting it."""
        return self._apriltag_live_detector.detect(
            frame,
            family=family,
            tag_size_m=tag_size_m,
            fx=fx,
            fy=fy,
            cx=cx,
            cy=cy,
            distortion_coefficients=distortion_coefficients,
        )

    def scene_config(self, scene_id: str) -> dict[str, Any]:
        path = self._config_path(scene_id)
        config = load_scene_config(path, repo_root=self.root, check_paths=False)
        return {"scene_id": scene_id, "config_uri": str(path.relative_to(self.root)), "config": config}
