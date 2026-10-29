#!/usr/bin/env python3
"""Run the real registered-frame VLM path and write an auditable report.

This acceptance command reads an immutable verified Scene Package and its MRP,
stores only derived records in the Scene Repository site-records registry, activates the completed
analysis run, and proves that a search hit resolves to the recorded Viewer pose.
It never updates Viewer runtime, Scene Packages, MRP, or SPZ artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
import sys
from time import perf_counter
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "generation_pipeline" / "tools"
for import_root in (ROOT, TOOLS):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from application_preview_api import PreviewService  # noqa: E402
from packages.scene_repository import SceneRepositoryLayout  # noqa: E402
from services.control_api.field_memory import FieldMemoryService  # noqa: E402
from services.control_api.frame_analysis import (  # noqa: E402
    FrameAnalysisService,
    RegisteredFrameAnalysisEngine,
    TransformersLocalVlmAnalyzer,
)
from services.control_api.spatial_search import SpatialSearchService  # noqa: E402
from services.frame_vlm_worker import model_artifact_evidence  # noqa: E402
from services.vlm_model_catalog import DEFAULT_VLM, REVIEWED_VLMS  # noqa: E402


def _tree_sha256(root: Path) -> str:
    """Return a stable content/relative-path digest for an immutable tree."""
    digest = hashlib.sha256()
    if not root.exists():
        digest.update(b"missing")
        return digest.hexdigest()
    paths = [root] if root.is_file() else sorted(path for path in root.rglob("*") if path.is_file())
    for path in paths:
        relative = path.name if root.is_file() else path.relative_to(root).as_posix()
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _existing_by_id(rows: list[dict[str, Any]], field: str, identifier: str) -> dict[str, Any] | None:
    return next((row for row in rows if row.get(field) == identifier), None)


def _ensure_site(registry: FieldMemoryService, site_id: str, name: str) -> dict[str, Any]:
    existing = _existing_by_id(registry.sites(), "site_id", site_id)
    if existing is not None:
        return existing
    return registry.create_site({"site_id": site_id, "name": name, "description": "実データVLM受入"})


def _ensure_scene_version(
    registry: FieldMemoryService,
    site_id: str,
    scene_version_id: str,
    scene_package_id: str,
) -> dict[str, Any]:
    existing = _existing_by_id(registry.scene_versions(site_id), "scene_version_id", scene_version_id)
    if existing is not None:
        if existing["scene_package_id"] != scene_package_id:
            raise ValueError("acceptance_scene_version_package_mismatch")
        return existing
    return registry.create_scene_version(
        site_id,
        {
            "scene_version_id": scene_version_id,
            "scene_package_id": scene_package_id,
            "label": f"VLM acceptance: {scene_package_id}",
        },
    )


def _package_inputs(repo_root: Path, registry: FieldMemoryService, package_id: str) -> dict[str, Path]:
    package = registry._verified_scene_packages().get(package_id)
    if package is None:
        raise ValueError(f"acceptance_scene_package_not_verified:{package_id}")
    scene_id = package.get("scene_id")
    reconstruction_id = package.get("reconstruction_id")
    if not isinstance(scene_id, str) or not isinstance(reconstruction_id, str):
        raise ValueError("acceptance_scene_package_identity_missing")
    layout = SceneRepositoryLayout.from_root(repo_root)
    package_root = layout.scene_packages / package_id
    mrp_root = layout.metric_reconstructions / scene_id / reconstruction_id
    if not package_root.is_dir() or not mrp_root.is_dir():
        raise FileNotFoundError("acceptance_immutable_input_missing")
    return {
        "scene_package": package_root,
        "mrp": mrp_root,
        "viewer_runtime": repo_root / "web_viewer" / "app" / "public" / "scene_package",
    }


def _validate_viewer_record(frame: dict[str, Any], package_id: str) -> None:
    if frame.get("scene_package_id") != package_id or not frame.get("is_active"):
        raise ValueError("acceptance_viewer_record_identity_invalid")
    position = frame["pose"]["position"]
    rotation = frame["pose"]["rotation"]
    values = [position[axis] for axis in "xyz"] + [rotation[axis] for axis in "xyzw"]
    if not all(isinstance(value, (int, float)) and math.isfinite(float(value)) for value in values):
        raise ValueError("acceptance_viewer_pose_non_finite")
    norm = math.sqrt(sum(float(rotation[axis]) ** 2 for axis in "xyzw"))
    if abs(norm - 1.0) > 1e-6:
        raise ValueError("acceptance_viewer_pose_quaternion_not_unit")
    expected = f"/?scene_package={package_id}&frame_analysis={frame['frame_analysis_id']}"
    if frame.get("viewer_url") != expected:
        raise ValueError("acceptance_viewer_url_invalid")


def _start_cuda_measurement() -> tuple[Any | None, dict[str, Any]]:
    """Reset PyTorch peak counters before model load when CUDA is available."""

    try:
        import torch
    except (ImportError, OSError) as error:
        return None, {"available": False, "reason": f"torch_unavailable:{error}"}
    if not torch.cuda.is_available():
        return None, {"available": False, "reason": "cuda_not_available"}
    torch.cuda.synchronize()
    torch.cuda.reset_peak_memory_stats()
    device = torch.cuda.current_device()
    properties = torch.cuda.get_device_properties(device)
    return torch, {
        "available": True,
        "device_index": device,
        "device_name": properties.name,
        "device_total_bytes": properties.total_memory,
    }


def _finish_cuda_measurement(torch_module: Any | None, evidence: dict[str, Any]) -> dict[str, Any]:
    if torch_module is None:
        return evidence
    torch_module.cuda.synchronize()
    evidence.update(
        {
            "peak_allocated_bytes": torch_module.cuda.max_memory_allocated(),
            "peak_reserved_bytes": torch_module.cuda.max_memory_reserved(),
        }
    )
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--scene-package-id", required=True)
    parser.add_argument("--site-id", default="site_vlm_acceptance_20260804")
    parser.add_argument("--scene-version-id", default="scene_vlm_acceptance_20260804")
    parser.add_argument("--analysis-run-id", required=True)
    parser.add_argument(
        "--model-id",
        choices=tuple(REVIEWED_VLMS),
        default=DEFAULT_VLM.model_id,
        help="Reviewed model identity. The local snapshot provenance must match it exactly.",
    )
    parser.add_argument("--model-path", type=Path, default=Path(".external/models/local-vlm"))
    parser.add_argument(
        "--interval-sec",
        type=float,
        default=1_000_000.0,
        help="Acceptance defaults to one source time point across all registered virtual cameras.",
    )
    parser.add_argument("--similarity-threshold", type=float, default=0.95)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    selected_model = REVIEWED_VLMS[args.model_id]

    repo_root = args.repo_root.resolve()
    model_path = args.model_path
    if not model_path.is_absolute():
        model_path = repo_root / model_path
    model_path = model_path.resolve()
    preview = PreviewService(repo_root)
    registry = FieldMemoryService(repo_root, scene_packages_provider=preview.scene_packages)
    analysis = FrameAnalysisService(registry)
    search = SpatialSearchService(registry)
    immutable_inputs = _package_inputs(repo_root, registry, args.scene_package_id)
    before = {name: _tree_sha256(path) for name, path in immutable_inputs.items()}

    _ensure_site(registry, args.site_id, "20260804 C room VLM acceptance")
    _ensure_scene_version(
        registry,
        args.site_id,
        args.scene_version_id,
        args.scene_package_id,
    )
    existing_run = _existing_by_id(analysis.runs(), "analysis_run_id", args.analysis_run_id)
    if existing_run is not None:
        raise ValueError(f"acceptance_analysis_run_id_already_exists:{args.analysis_run_id}")
    busy_runs = [
        run
        for run in analysis.runs()
        if run.get("status") in {"queued", "running"}
    ]
    if busy_runs:
        busy_ids = ",".join(str(run.get("analysis_run_id", "unknown")) for run in busy_runs)
        raise RuntimeError(f"acceptance_other_run_in_progress:{busy_ids}")
    analysis.create_run(
        {
            "analysis_run_id": args.analysis_run_id,
            "scene_version_id": args.scene_version_id,
            "model_id": selected_model.model_id,
            "model_revision": selected_model.revision,
            "prompt_version": "spatial-frame-v1",
            "interval_sec": args.interval_sec,
            "similarity_threshold": args.similarity_threshold,
            "parameters": {
                "acceptance_scope": "one-source-time-all-registered-cameras",
                "neutral_background_rgb": [127, 127, 127],
            },
        }
    )
    run = analysis.claim_next_run()
    if run is None or run["analysis_run_id"] != args.analysis_run_id:
        raise RuntimeError("acceptance_run_claim_order_invalid")

    acceptance_started = perf_counter()
    model_load_elapsed_sec: float | None = None
    analysis_elapsed_sec: float | None = None
    cuda_module, cuda_evidence = _start_cuda_measurement()
    try:
        run = analysis.record_model_provenance(
            args.analysis_run_id,
            model_artifact_evidence(model_path),
        )
        analyzer = TransformersLocalVlmAnalyzer(str(model_path))
        model_load_started = perf_counter()
        analyzer.load()
        model_load_elapsed_sec = perf_counter() - model_load_started
        analysis_started = perf_counter()
        completed = RegisteredFrameAnalysisEngine(repo_root, analysis, analyzer).process(run)
        analysis_elapsed_sec = perf_counter() - analysis_started
    except Exception as error:
        try:
            analysis.fail_run(args.analysis_run_id, f"{type(error).__name__}: {error}")
        except ValueError:
            pass
        raise

    if completed["status"] != "completed" or completed["output_count"] < 1:
        raise RuntimeError("acceptance_analysis_not_completed")
    activation = analysis.activate(args.scene_version_id, args.analysis_run_id)
    completed = analysis.run(args.analysis_run_id)
    first = analysis.frame(completed["frames"][0]["frame_analysis_id"])
    _validate_viewer_record(first, args.scene_package_id)

    listing = search.search(
        {
            "site_id": args.site_id,
            "query": "",
            "record_types": ["frame_analysis"],
            "limit": 100,
        }
    )
    listed_ids = {item["record_id"] for item in listing["results"]}
    if first["frame_analysis_id"] not in listed_ids:
        raise RuntimeError("acceptance_active_frame_missing_from_search")
    query_term = next(iter(first["keywords"]), first["image_name"])
    keyword_result = search.search(
        {
            "site_id": args.site_id,
            "query": query_term,
            "record_types": ["frame_analysis"],
            "limit": 100,
        }
    )
    if first["frame_analysis_id"] not in {item["record_id"] for item in keyword_result["results"]}:
        raise RuntimeError("acceptance_frame_missing_from_keyword_search")

    after = {name: _tree_sha256(path) for name, path in immutable_inputs.items()}
    if before != after:
        raise RuntimeError("acceptance_immutable_input_changed")
    report = {
        "schema_version": "registered-frame-vlm-acceptance-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "passed",
        "site_id": args.site_id,
        "scene_version_id": args.scene_version_id,
        "scene_package_id": args.scene_package_id,
        "analysis_run_id": args.analysis_run_id,
        "model": {
            "id": selected_model.model_id,
            "revision": selected_model.revision,
            "parameter_scale": selected_model.parameter_scale,
        },
        "selection": {
            "interval_sec": completed["interval_sec"],
            "similarity_threshold": completed["similarity_threshold"],
            "source_evidence": completed["parameters"]["source_evidence"],
        },
        "output_count": completed["output_count"],
        "activation": activation,
        "search": {
            "listing_count": listing["count"],
            "query": query_term,
            "query_count": keyword_result["count"],
            "resolved_frame_analysis_id": first["frame_analysis_id"],
        },
        "viewer": {
            "url": first["viewer_url"],
            "pose": first["pose"],
            "projection": first["projection"],
        },
        "first_result": {
            "image_name": first["image_name"],
            "summary": first["summary"],
            "keywords": first["keywords"],
            "image_content_sha256": first["image_content_sha256"],
            "thumbnail_content_sha256": first["thumbnail_content_sha256"],
        },
        "immutable_input_tree_sha256_before": before,
        "immutable_input_tree_sha256_after": after,
        "immutable_inputs_unchanged": before == after,
        "performance": {
            "acceptance_elapsed_sec": round(perf_counter() - acceptance_started, 3),
            "model_load_elapsed_sec": round(model_load_elapsed_sec, 3) if model_load_elapsed_sec is not None else None,
            "analysis_elapsed_sec": round(analysis_elapsed_sec, 3) if analysis_elapsed_sec is not None else None,
            "output_count": completed["output_count"],
            "seconds_per_output": round(analysis_elapsed_sec / completed["output_count"], 3)
            if analysis_elapsed_sec is not None and completed["output_count"]
            else None,
            "cuda": _finish_cuda_measurement(cuda_module, cuda_evidence),
        },
    }
    report_path = args.report or (
        SceneRepositoryLayout.from_root(repo_root).site_records
        / "acceptance" / f"{args.analysis_run_id}.json"
    )
    if not report_path.is_absolute():
        report_path = repo_root / report_path
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "status": "passed",
        "analysis_run_id": args.analysis_run_id,
        "output_count": completed["output_count"],
        "search_query": query_term,
        "viewer_url": first["viewer_url"],
        "report": str(report_path),
    }, ensure_ascii=False, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
