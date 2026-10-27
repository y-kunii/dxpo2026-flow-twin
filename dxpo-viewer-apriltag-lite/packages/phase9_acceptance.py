#!/usr/bin/env python3
"""Collect one fail-closed Phase 9 real-data acceptance report.

The command never mutates canonical Scene Packages, MRP, source assets, or
workspaces. It joins existing pipeline, package, Viewer, VLM and ROS evidence
by identity, measures read paths and capacity without directory hashing, and
records missing external evidence as ``incomplete`` rather than success.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from time import perf_counter
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from generation_pipeline.tools.application_preview_api import PreviewService  # noqa: E402
from generation_pipeline.tools.measure_scene_package_read_paths import measure  # noqa: E402
from packages.public_deployment_acceptance import public_deployment_failures  # noqa: E402
from packages.scene_repository import SceneRepositoryLayout  # noqa: E402
from services.vlm_model_catalog import REVIEWED_VLMS, reviewed_vlm_directory  # noqa: E402


TERMINAL_STATUSES = frozenset({"verified", "incomplete", "failed", "not_applicable"})


def _stage(identifier: str, status: str, summary: str, **evidence: Any) -> dict[str, Any]:
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"unsupported_phase9_status:{status}")
    return {"id": identifier, "status": status, "summary": summary, "evidence": evidence}


def _read_json(path: Path | None) -> tuple[dict[str, Any], str | None]:
    if path is None:
        return {}, "evidence_not_supplied"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {}, f"evidence_unreadable:{error}"
    if not isinstance(value, dict):
        return {}, "evidence_must_be_object"
    return value, None


def _git(root: Path, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def _du_bytes(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False, "bytes": None, "elapsed_ms": None}
    started = perf_counter()
    try:
        result = subprocess.run(
            ["du", "-sb", "--one-file-system", str(path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        size = int(result.stdout.split(maxsplit=1)[0])
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return {
            "path": str(path),
            "exists": True,
            "bytes": None,
            "elapsed_ms": round((perf_counter() - started) * 1000.0, 3),
            "error": str(error),
        }
    return {
        "path": str(path),
        "exists": True,
        "bytes": size,
        "elapsed_ms": round((perf_counter() - started) * 1000.0, 3),
        "method": "du -sb --one-file-system (no content hash)",
    }


def _run_command(
    root: Path,
    identifier: str,
    command: list[str],
    timeout: int,
    *,
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    started = perf_counter()
    try:
        result = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, **environment} if environment else None,
        )
        return {
            "id": identifier,
            "status": "verified" if result.returncode == 0 else "failed",
            "command": command,
            "exit_code": result.returncode,
            "elapsed_sec": round(perf_counter() - started, 3),
            "environment": environment or {},
            "stdout_tail": result.stdout[-4000:],
            "stderr_tail": result.stderr[-4000:],
        }
    except (OSError, subprocess.SubprocessError) as error:
        return {
            "id": identifier,
            "status": "failed",
            "command": command,
            "exit_code": None,
            "elapsed_sec": round(perf_counter() - started, 3),
            "environment": environment or {},
            "error": str(error),
        }


def _automated_checks(root: Path) -> list[dict[str, Any]]:
    playwright_output = root / "var/spatial/test-results" / f"phase9-playwright-{os.getpid()}"
    coverage_file = root / "var/spatial/test-results" / f"phase9-coverage-{os.getpid()}"
    coverage_environment = {"COVERAGE_FILE": str(coverage_file)}
    return [
        _run_command(root, "repository_structure", [sys.executable, "scripts/check_repository_structure.py"], 120),
        _run_command(
            root,
            "python_lint",
            [sys.executable, "-m", "ruff", "check", "generation_pipeline", "packages", "services", "scripts", "tests"],
            300,
        ),
        _run_command(root, "python_types", [sys.executable, "-m", "mypy"], 300),
        _run_command(
            root,
            "python_tests",
            [sys.executable, "-m", "coverage", "run", "-m", "unittest", "discover"],
            900,
            environment=coverage_environment,
        ),
        _run_command(
            root,
            "python_coverage",
            [sys.executable, "-m", "coverage", "report"],
            120,
            environment=coverage_environment,
        ),
        _run_command(root, "compose_contract", ["docker", "compose", "config", "--quiet"], 120),
        _run_command(root, "viewer_lint", ["npm", "run", "lint", "--prefix", "web_viewer/app"], 300),
        _run_command(root, "viewer_build", ["npm", "run", "build", "--prefix", "web_viewer/app"], 900),
        _run_command(root, "viewer_app_builds", ["npm", "run", "build:apps", "--prefix", "web_viewer/app"], 1200),
        _run_command(root, "viewer_smoke", ["npm", "run", "test:smoke", "--prefix", "web_viewer/app"], 300),
        _run_command(root, "app_package_smoke", ["npm", "run", "test:app-packages", "--prefix", "web_viewer/app"], 300),
        _run_command(
            root,
            "viewer_browser_e2e",
            ["npm", "run", "test:e2e", "--prefix", "web_viewer/app"],
            1200,
            environment={"PW_OUTPUT_DIR": str(playwright_output)},
        ),
    ]


def _matching_project(projects: list[dict[str, Any]], package: dict[str, Any]) -> dict[str, Any] | None:
    project_id = str(package.get("project_id", ""))
    return next(
        (
            item
            for item in projects
            if item.get("project_name") == project_id
            or str(item.get("project_id", "")).endswith(f":{project_id}")
        ),
        None,
    )


def _assess_vlm_evidence(path: Path | None, scene_package_id: str) -> dict[str, Any]:
    evidence, error = _read_json(path)
    reasons: list[str] = []
    if error:
        reasons.append(error)
    if evidence and evidence.get("status") != "passed":
        reasons.append("vlm_acceptance_not_passed")
    if evidence and evidence.get("scene_package_id") != scene_package_id:
        reasons.append("vlm_acceptance_scene_package_mismatch")
    if evidence and evidence.get("immutable_inputs_unchanged") is not True:
        reasons.append("vlm_acceptance_immutable_input_not_proven")
    if evidence and int(evidence.get("output_count") or 0) < 1:
        reasons.append("vlm_acceptance_output_missing")
    viewer = evidence.get("viewer") if isinstance(evidence.get("viewer"), dict) else {}
    if evidence and scene_package_id not in str(viewer.get("url", "")):
        reasons.append("vlm_acceptance_viewer_identity_mismatch")
    return _stage(
        "frame_analysis_search_viewer",
        "verified" if not reasons else "incomplete",
        "同一Scene PackageのVLM・検索・Viewer視点解決を確認" if not reasons else "同一Scene PackageのVLM統合受入証拠が不足",
        report_path=str(path) if path else None,
        analysis_run_id=evidence.get("analysis_run_id"),
        output_count=evidence.get("output_count"),
        model=evidence.get("model"),
        performance=evidence.get("performance"),
        reasons=reasons,
    )


def _assess_ros_evidence(path: Path | None, scene_package_id: str | None = None) -> dict[str, Any]:
    evidence, error = _read_json(path)
    reasons: list[str] = []
    if error:
        reasons.append(error)
    if evidence and evidence.get("status") != "verified":
        reasons.append("real_ros_status_not_verified")
    if evidence and evidence.get("real_hardware") is not True:
        reasons.append("real_ros_hardware_not_proven")
    if evidence and evidence.get("visual_confirmation") is not True:
        reasons.append("real_ros_visual_confirmation_missing")
    if evidence and evidence.get("topic") != "/box/relative_transform":
        reasons.append("real_ros_topic_mismatch")
    if evidence and scene_package_id is not None and evidence.get("scene_package_id") != scene_package_id:
        reasons.append("real_ros_scene_package_mismatch")
    source_log = evidence.get("source_log") if isinstance(evidence.get("source_log"), dict) else {}
    message_count = source_log.get("message_count")
    if evidence and (
        not isinstance(source_log.get("sha256"), str)
        or len(source_log["sha256"]) != 64
        or not isinstance(message_count, int)
        or isinstance(message_count, bool)
        or message_count < 1
    ):
        reasons.append("real_ros_source_log_evidence_invalid")
    return _stage(
        "real_ros2_dynamic_tracking",
        "verified" if not reasons else "incomplete",
        "実ROS 2動的追跡を確認" if not reasons else "実ROS 2受入は未完了",
        report_path=str(path) if path else None,
        reasons=reasons,
    )


def _model_inventory(root: Path) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    for model in REVIEWED_VLMS.values():
        directory = reviewed_vlm_directory(root, model.model_id)
        size = _du_bytes(directory)
        inventory.append(
            {
                **asdict(model),
                "local_path": str(directory),
                "installed": (directory / "config.json").is_file(),
                "installed_bytes": size.get("bytes"),
            }
        )
    return inventory


def _gpu_inventory() -> dict[str, Any]:
    command = [
        "nvidia-smi",
        "--query-gpu=name,memory.total,driver_version",
        "--format=csv,noheader,nounits",
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as error:
        return {"available": False, "error": str(error)}
    return {"available": True, "command": command, "devices": result.stdout.strip().splitlines()}


def _assess_capacity_policy(
    policy_path: Path,
    inventory: dict[str, Any],
    model: dict[str, Any] | None,
    performance: dict[str, Any] | None,
) -> dict[str, Any]:
    """Compare measured capacity evidence with an explicit, versioned policy."""

    policy, error = _read_json(policy_path)
    reasons: list[str] = []
    evaluations: list[dict[str, Any]] = []
    if error:
        reasons.append(error)
    if policy.get("schema_version") != "phase9-capacity-policy-v1":
        reasons.append("capacity_policy_schema_invalid")
    if policy.get("status") != "enforced":
        reasons.append("capacity_policy_not_enforced")

    def maximum(label: str, measured: Any, limit: Any) -> None:
        valid = isinstance(measured, (int, float)) and not isinstance(measured, bool)
        bounded = isinstance(limit, (int, float)) and not isinstance(limit, bool) and limit > 0
        passed = valid and bounded and measured <= limit
        evaluations.append({"metric": label, "measured": measured, "limit": limit, "passed": passed})
        if not passed:
            reasons.append(f"capacity_limit_exceeded_or_unmeasured:{label}")

    def minimum(label: str, measured: Any, limit: Any) -> None:
        valid = isinstance(measured, (int, float)) and not isinstance(measured, bool)
        bounded = isinstance(limit, (int, float)) and not isinstance(limit, bool) and limit > 0
        passed = valid and bounded and measured >= limit
        evaluations.append({"metric": label, "measured": measured, "limit": limit, "passed": passed})
        if not passed:
            reasons.append(f"capacity_minimum_not_met_or_unmeasured:{label}")

    disk_limits = policy.get("disk_limits_bytes") if isinstance(policy.get("disk_limits_bytes"), dict) else {}
    for name in (
        "selected_workspace",
        "all_project_workspaces",
        "scene_package",
        "metric_reconstruction",
        "site_records",
    ):
        evidence = inventory.get(name) if isinstance(inventory.get(name), dict) else {}
        maximum(f"disk.{name}.bytes", evidence.get("bytes"), disk_limits.get(name))
    repository_disk = inventory.get("repository_disk") if isinstance(inventory.get("repository_disk"), dict) else {}
    minimum(
        "disk.repository.free_bytes",
        repository_disk.get("free"),
        policy.get("minimum_repository_free_bytes"),
    )

    model_id = model.get("id") if isinstance(model, dict) else None
    all_vlm_limits = policy.get("vlm_limits") if isinstance(policy.get("vlm_limits"), dict) else {}
    vlm_limits = all_vlm_limits.get(model_id) if isinstance(model_id, str) else None
    if not isinstance(vlm_limits, dict):
        reasons.append(f"vlm_capacity_policy_missing:{model_id or 'unknown'}")
    else:
        measured = performance if isinstance(performance, dict) else {}
        cuda = measured.get("cuda") if isinstance(measured.get("cuda"), dict) else {}
        maximum("vlm.model_load_seconds", measured.get("model_load_elapsed_sec"), vlm_limits.get("maximum_model_load_seconds"))
        maximum("vlm.seconds_per_output", measured.get("seconds_per_output"), vlm_limits.get("maximum_seconds_per_output"))
        maximum("vlm.peak_reserved_bytes", cuda.get("peak_reserved_bytes"), vlm_limits.get("maximum_peak_reserved_bytes"))
        minimum("vlm.device_total_bytes", cuda.get("device_total_bytes"), vlm_limits.get("minimum_device_total_bytes"))

    return _stage(
        "capacity_and_vlm_limits",
        "verified" if not reasons else "incomplete",
        "Measured disk and VLM resources are within the enforced internal policy"
        if not reasons
        else "Capacity evidence is missing or outside the enforced internal policy",
        policy_path=str(policy_path),
        policy_id=policy.get("policy_id"),
        policy_scope=policy.get("scope"),
        inventory=inventory,
        vlm_model=model,
        vlm_performance=performance,
        evaluations=evaluations,
        reasons=sorted(set(reasons)),
    )


def _assess_viewer_formats(
    report_path: Path | None,
    subject_formats: list[str],
) -> dict[str, Any]:
    report, error = _read_json(report_path)
    reasons: list[str] = []
    if error:
        reasons.append(error)
    if report and report.get("status") != "verified":
        reasons.append("gaussian_format_acceptance_not_verified")
    report_formats = report.get("accepted_formats") if isinstance(report.get("accepted_formats"), list) else []
    accepted_formats = sorted({*subject_formats, *(str(item) for item in report_formats)})
    missing_formats = sorted({"spz", "ply", "rad"} - set(accepted_formats))
    if missing_formats:
        reasons.append("gaussian_format_evidence_missing")
    return _stage(
        "viewer_formats",
        "verified" if not reasons else "incomplete",
        "Representative SPZ, PLY and RAD assets passed the import validation contract"
        if not reasons
        else "Representative Gaussian format evidence is incomplete",
        report_path=str(report_path) if report_path else None,
        subject_formats=subject_formats,
        accepted_formats=accepted_formats,
        missing_formats=missing_formats,
        assets=report.get("assets", []),
        reasons=sorted(set(reasons)),
    )


def collect(
    root: Path,
    *,
    scene_package_id: str,
    vlm_acceptance_report: Path | None,
    real_ros_report: Path | None,
    performance_iterations: int,
    run_automated: bool,
    capacity_policy: Path,
    viewer_format_report: Path | None,
    public_deployment_report: Path | None,
) -> dict[str, Any]:
    layout = SceneRepositoryLayout.from_root(root)
    service = PreviewService(root)
    packages = service.scene_packages()
    package = next((item for item in packages if item.get("scene_package_id") == scene_package_id), None)
    if package is None:
        raise ValueError(f"scene_package_not_found:{scene_package_id}")
    package_root = layout.resolve_uri(str(package.get("package_uri", "")))
    scene_manifest, scene_manifest_error = _read_json(package_root / "scene_manifest.json")

    stages: list[dict[str, Any]] = []
    package_reasons = []
    if package.get("managed") is not True or package.get("status") != "verified":
        package_reasons.append("scene_package_not_verified")
    if scene_manifest_error:
        package_reasons.append(scene_manifest_error)
    stages.append(
        _stage(
            "scene_package",
            "verified" if not package_reasons else "failed",
            "管理対象の検証済みScene Package" if not package_reasons else "Scene Package検証失敗",
            scene_package_id=scene_package_id,
            package_uri=package.get("package_uri"),
            schema_version=package.get("schema_version"),
            validation=package.get("validation"),
            reasons=package_reasons,
        )
    )

    projects = service.projects()
    project = _matching_project(projects, package)
    jobs = service.jobs()
    job = next((item for item in jobs if project and item.get("job_id") == project.get("job_id")), None)
    mrp_root = layout.metric_reconstructions / str(package.get("scene_id")) / str(package.get("reconstruction_id"))
    mrp_manifest, mrp_error = _read_json(mrp_root / "manifest.json")
    chain_reasons: list[str] = []
    if project is None:
        chain_reasons.append("matching_project_missing")
    if job is None or job.get("status") != "verified":
        chain_reasons.append("verified_pipeline_job_missing")
    if mrp_error or mrp_manifest.get("package_kind") != "metric_reconstruction":
        chain_reasons.append("metric_reconstruction_manifest_invalid")
    if mrp_manifest.get("scene_id") != package.get("scene_id"):
        chain_reasons.append("metric_reconstruction_scene_mismatch")
    if mrp_manifest.get("reconstruction_id") != package.get("reconstruction_id"):
        chain_reasons.append("metric_reconstruction_id_mismatch")
    stages.append(
        _stage(
            "source_colmap_mrp",
            "verified" if not chain_reasons else "incomplete",
            "実データpipeline jobからMRPまでidentityを追跡" if not chain_reasons else "sourceからMRPまでの証拠が不足",
            project=project,
            job={key: job.get(key) for key in ("job_id", "status", "started_at", "completed_at", "profile", "mrp_uri")} if job else None,
            mrp_manifest=str(mrp_root / "manifest.json"),
            input_video_hash=(mrp_manifest.get("source") or {}).get("input_video_hash"),
            reasons=chain_reasons,
        )
    )

    gaussian_metadata, gaussian_error = _read_json(package_root / "assets/gaussian_asset.json")
    studio_reasons: list[str] = []
    if (scene_manifest.get("provenance") or {}).get("external_stage") != "LichtFeld Studio":
        studio_reasons.append("lichtfeld_stage_provenance_missing")
    if gaussian_error or gaussian_metadata.get("status") != "verified":
        studio_reasons.append("gaussian_asset_not_verified")
    if not (mrp_root / "lfs_input/manifest.json").is_file():
        studio_reasons.append("lfs_input_manifest_missing")
    stages.append(
        _stage(
            "lichtfeld_to_scene_package",
            "verified" if not studio_reasons else "incomplete",
            "LichtFeld入力境界と検証済みSPZの関連を確認" if not studio_reasons else "LichtFeld境界の証拠が不足",
            lfs_manifest=str(mrp_root / "lfs_input/manifest.json"),
            gaussian_asset=gaussian_metadata,
            reasons=studio_reasons,
        )
    )

    compatibility = package.get("application_compatibility") or {}
    viewer_reasons = [
        f"{app_id}_not_compatible"
        for app_id in ("viewer", "simple-viewer", "frame-analysis", "site-insights")
        if not isinstance(compatibility.get(app_id), dict) or compatibility[app_id].get("compatible") is not True
    ]
    if not (package_root / "config/world_alignment.json").is_file():
        viewer_reasons.append("world_alignment_missing")
    stages.append(
        _stage(
            "viewer_application_contract",
            "verified" if not viewer_reasons else "failed",
            "Viewer・AI解析・検索のPackage互換性を確認" if not viewer_reasons else "アプリ互換性が不十分",
            compatibility=compatibility,
            viewer_url=package.get("viewer_url"),
            reasons=viewer_reasons,
        )
    )
    vlm_stage = _assess_vlm_evidence(vlm_acceptance_report, scene_package_id)
    stages.append(vlm_stage)
    stages.append(_assess_ros_evidence(real_ros_report, scene_package_id))

    asset_formats = sorted(
        {
            str(item.get("source_format"))
            for item in ((scene_manifest.get("assets") or {}).get("splats") or [])
            if isinstance(item, dict) and item.get("source_format")
        }
    )
    stages.append(_assess_viewer_formats(viewer_format_report, asset_formats))

    capabilities = scene_manifest.get("capabilities") if isinstance(scene_manifest.get("capabilities"), dict) else {}
    required_capabilities = ("measurement", "surface_placement", "fov", "occlusion")
    missing_capabilities = [name for name in required_capabilities if capabilities.get(name) is not True]
    stages.append(
        _stage(
            "viewer_analysis_capabilities",
            "verified" if not missing_capabilities else "incomplete",
            "Viewer解析機能の実Package受入" if not missing_capabilities else "実Packageで未受入のViewer解析機能あり",
            capabilities=capabilities,
            missing_capabilities=missing_capabilities,
        )
    )

    performance = measure(root, iterations=performance_iterations, scene_package_id=scene_package_id)
    measurements = performance["measurements"]
    warm_under_limit = all(
        measurements[name]["warm"]["median_ms"] < 10.0
        for name in ("scene_packages", "app_package_preflight")
    )
    stages.append(
        _stage(
            "scene_package_read_performance",
            "verified" if warm_under_limit else "failed",
            "warm read pathは10 ms未満" if warm_under_limit else "warm read pathが応答尺度を超過",
            response_measure="warm median < 10 ms",
            report=performance,
        )
    )

    selected_workspace = layout.resolve_uri(str(project.get("workspace_uri"))) if project else layout.projects / "missing"
    disk = shutil.disk_usage(layout.repository)
    capacity = {
        "selected_workspace": _du_bytes(selected_workspace),
        "all_project_workspaces": _du_bytes(layout.projects),
        "scene_package": _du_bytes(package_root),
        "metric_reconstruction": _du_bytes(mrp_root),
        "site_records": _du_bytes(layout.site_records),
        "repository_disk": {"total": disk.total, "used": disk.used, "free": disk.free},
        "gpu": _gpu_inventory(),
        "reviewed_models": _model_inventory(root),
    }
    stages.append(
        _assess_capacity_policy(
            capacity_policy,
            capacity,
            vlm_stage["evidence"].get("model"),
            vlm_stage["evidence"].get("performance"),
        )
    )

    checks = _automated_checks(root) if run_automated else []
    check_status = "verified" if checks and all(item["status"] == "verified" for item in checks) else ("failed" if checks else "incomplete")
    stages.append(
        _stage(
            "automated_regression",
            check_status,
            "自動回帰・buildを実行" if checks else "--run-automatedで回帰・buildを実行する必要あり",
            checks=checks,
            reasons=[] if checks else ["automated_checks_not_run"],
        )
    )

    license_exists = (root / "LICENSE").is_file() and (root / "LICENSE").stat().st_size > 0
    public_reasons = []
    if not license_exists:
        public_reasons.append("repository_license_missing")
    if not (root / ".github/CODEOWNERS").is_file():
        public_reasons.append("codeowners_not_configured")
    public_reasons.extend(public_deployment_failures(public_deployment_report))

    statuses = {stage["id"]: stage["status"] for stage in stages}
    overall = "failed" if "failed" in statuses.values() else ("verified" if all(value in {"verified", "not_applicable"} for value in statuses.values()) else "incomplete")
    git_status = _git(root, "status", "--porcelain")
    return {
        "schema_version": "phase9-acceptance-report-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": overall,
        "scope": "representative-real-data-end-to-end",
        "subject": {
            "scene_package_id": scene_package_id,
            "project_id": package.get("project_id"),
            "scene_id": package.get("scene_id"),
            "reconstruction_id": package.get("reconstruction_id"),
        },
        "environment": {
            "repository_root": str(root),
            "git_commit": _git(root, "rev-parse", "HEAD"),
            "git_worktree_clean": git_status == "" if git_status is not None else None,
            "git_status": git_status.splitlines() if git_status else [],
            "python": sys.version.split()[0],
            "platform": platform.platform(),
        },
        "stages": stages,
        "summary": {
            "verified": sorted(key for key, value in statuses.items() if value == "verified"),
            "incomplete": sorted(key for key, value in statuses.items() if value == "incomplete"),
            "failed": sorted(key for key, value in statuses.items() if value == "failed"),
        },
        "public_release_gate": {
            "status": "blocked" if public_reasons else "verified",
            "reasons": public_reasons,
            "note": "内部・単一operator受入と公開サービスreleaseを混同しない",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--scene-package-id", required=True)
    parser.add_argument("--vlm-acceptance-report", type=Path)
    parser.add_argument("--real-ros-report", type=Path)
    parser.add_argument("--viewer-format-report", type=Path)
    parser.add_argument("--public-deployment-report", type=Path)
    parser.add_argument("--performance-iterations", type=int, default=10)
    parser.add_argument("--run-automated", action="store_true")
    parser.add_argument(
        "--capacity-policy",
        type=Path,
        default=ROOT / "generation_pipeline/config/acceptance/phase9_limits.json",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.performance_iterations <= 1000:
        parser.error("--performance-iterations must be between 1 and 1000")
    root = args.repo_root.resolve()
    output = args.output
    if output is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = SceneRepositoryLayout.from_root(root).reports / "acceptance" / f"phase9-{stamp}.json"
    elif not output.is_absolute():
        output = root / output
    report = collect(
        root,
        scene_package_id=args.scene_package_id,
        vlm_acceptance_report=args.vlm_acceptance_report,
        real_ros_report=args.real_ros_report,
        performance_iterations=args.performance_iterations,
        run_automated=args.run_automated,
        capacity_policy=args.capacity_policy.resolve(),
        viewer_format_report=args.viewer_format_report.resolve() if args.viewer_format_report else None,
        public_deployment_report=args.public_deployment_report.resolve() if args.public_deployment_report else None,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    print(json.dumps({"status": report["status"], "summary": report["summary"], "report": str(output)}, ensure_ascii=False))
    return 0 if report["status"] == "verified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
