"""Install and report allowlisted local VLM snapshots.

Downloads are deliberately restricted to the reviewed catalog and pinned
commits.  The API never accepts an arbitrary repository URL or revision.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from services.vlm_model_catalog import (
    DEFAULT_VLM,
    REVIEWED_VLMS,
    VRAM_RECOMMENDATIONS,
    ReviewedVlm,
    reviewed_vlm,
    reviewed_vlm_directory,
)


PROVENANCE_FILENAME = "SPATIAL_MODEL_PROVENANCE.json"
DownloadFunction = Callable[..., Any]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_downloader(**kwargs: Any) -> Any:
    try:
        from huggingface_hub import snapshot_download
    except ImportError as error:
        raise RuntimeError("vlm_model_huggingface_hub_not_installed") from error
    return snapshot_download(**kwargs)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


class VlmModelManager:
    """Small background installer shared by the frame-analysis API."""

    def __init__(
        self,
        repo_root: Path,
        *,
        downloader: DownloadFunction | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.downloader = downloader or _default_downloader
        self.state_dir = self.repo_root / "var/spatial/model-downloads"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        for state_path in sorted(self.state_dir.glob("vlm_*.json")):
            job = _read_json(state_path)
            if not job or not isinstance(job.get("job_id"), str):
                continue
            if job.get("status") in {"queued", "downloading"}:
                job.update(
                    status="failed",
                    error="model_download_interrupted_by_api_restart",
                    updated_at=_now(),
                )
                self._persist_job(job)
            self._jobs[job["job_id"]] = job

    @staticmethod
    def _provenance(model: ReviewedVlm) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "kind": "reviewed_local_vlm_snapshot",
            "model_id": model.model_id,
            "revision": model.revision,
            "license": model.license_id,
            "parameter_scale": model.parameter_scale,
            "minimum_vram_gib": model.minimum_vram_gib,
            "recommended_vram_gib": model.recommended_vram_gib,
            "quality_tier": model.quality_tier,
            "source": f"https://huggingface.co/{model.model_id}/tree/{model.revision}",
            "installed_at": _now(),
        }

    def _installed(self, model: ReviewedVlm, target: Path) -> bool:
        provenance = _read_json(target / PROVENANCE_FILENAME)
        return bool(
            target.is_dir()
            and (target / "config.json").is_file()
            and provenance
            and provenance.get("model_id") == model.model_id
            and provenance.get("revision") == model.revision
        )

    def _persist_job(self, job: dict[str, Any]) -> None:
        path = self.state_dir / f"{job['job_id']}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def _active_job(self, model_id: str) -> dict[str, Any] | None:
        return next(
            (
                dict(job)
                for job in self._jobs.values()
                if job["model_id"] == model_id and job["status"] in {"queued", "downloading"}
            ),
            None,
        )

    def status(self, model_id: str) -> dict[str, Any]:
        model = reviewed_vlm(model_id)
        target = reviewed_vlm_directory(self.repo_root, model_id)
        with self._lock:
            active = self._active_job(model_id)
            latest = next(
                (
                    dict(job)
                    for job in reversed(list(self._jobs.values()))
                    if job["model_id"] == model_id
                ),
                None,
            )
        installed = self._installed(model, target)
        state = (
            "installed"
            if installed
            else str(active["status"])
            if active
            else "failed"
            if latest and latest.get("status") == "failed"
            else "not_installed"
        )
        return {
            "model_id": model.model_id,
            "display_name": f"Qwen3-VL {model.parameter_scale} Instruct",
            "revision": model.revision,
            "license": model.license_id,
            "source_provider": "Hugging Face / Qwen",
            "source_url": f"https://huggingface.co/{model.model_id}/tree/{model.revision}",
            "storage_directory": model.storage_directory,
            "download_contents": [
                "model_weights",
                "configuration",
                "image_processor",
                "tokenizer",
            ],
            "parameter_scale": model.parameter_scale,
            "approximate_download_gib": model.approximate_download_gib,
            "minimum_vram_gib": model.minimum_vram_gib,
            "recommended_vram_gib": model.recommended_vram_gib,
            "quality_tier": model.quality_tier,
            "installed": installed,
            "status": state,
            "job_id": (active or latest or {}).get("job_id"),
            "error": (active or latest or {}).get("error"),
        }

    def catalog(self) -> dict[str, Any]:
        return {
            "schema_version": "1.1.0",
            "default_model_id": DEFAULT_VLM.model_id,
            "vram_estimate_notice": (
                "BF16・画像1枚推論の運用目安です。画像解像度、CUDA、driver、"
                "他processの使用量により必要VRAMは変動します。"
            ),
            "vram_recommendations": [
                {
                    "vram_gib": item.vram_gib,
                    "model_id": item.model_id,
                    "label": item.label,
                    "note": item.note,
                }
                for item in VRAM_RECOMMENDATIONS
            ],
            "models": [self.status(model_id) for model_id in REVIEWED_VLMS],
        }

    def install(self, model_id: str) -> dict[str, Any]:
        model = reviewed_vlm(model_id)
        target = reviewed_vlm_directory(self.repo_root, model_id)
        if self._installed(model, target):
            return self.status(model_id)
        existing_job = False
        with self._lock:
            active = self._active_job(model_id)
            if active:
                existing_job = True
            else:
                job = {
                    "schema_version": "1.0.0",
                    "kind": "vlm_model_download_job",
                    "job_id": f"vlm_{uuid.uuid4().hex}",
                    "model_id": model.model_id,
                    "revision": model.revision,
                    "status": "queued",
                    "created_at": _now(),
                    "updated_at": _now(),
                    "error": None,
                }
                self._jobs[job["job_id"]] = job
                self._persist_job(job)
        if existing_job:
            return self.status(model_id)
        thread = threading.Thread(
            target=self._download,
            args=(model, target, job["job_id"]),
            name=f"vlm-download-{model.storage_directory}",
            daemon=True,
        )
        thread.start()
        return self.status(model_id)

    def _update_job(self, job_id: str, **changes: Any) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.update(changes, updated_at=_now())
            self._persist_job(job)

    def _download(self, model: ReviewedVlm, target: Path, job_id: str) -> None:
        staging = target.parent / f".{target.name}.download-{job_id}"
        try:
            self._update_job(job_id, status="downloading")
            target.parent.mkdir(parents=True, exist_ok=True)
            staging.mkdir(parents=False, exist_ok=False)
            self.downloader(
                repo_id=model.model_id,
                revision=model.revision,
                local_dir=str(staging),
            )
            if not (staging / "config.json").is_file():
                raise ValueError("vlm_model_download_missing_config")
            if not any(staging.glob("*.safetensors")):
                raise ValueError("vlm_model_download_missing_weights")
            (staging / PROVENANCE_FILENAME).write_text(
                json.dumps(self._provenance(model), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            if target.exists():
                existing = {path.name for path in target.iterdir()}
                if existing <= {"README.md", ".distribution-model-store"}:
                    shutil.rmtree(target)
                else:
                    raise FileExistsError("vlm_model_target_contains_unreviewed_files")
            os.replace(staging, target)
            self._update_job(job_id, status="completed")
        except Exception as error:
            self._update_job(job_id, status="failed", error=f"{type(error).__name__}: {error}")
        finally:
            if staging.is_dir():
                shutil.rmtree(staging)
