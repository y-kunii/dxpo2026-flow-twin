#!/usr/bin/env python3
"""Process queued registered-frame analysis runs with a local VLM.

The worker is intentionally separate from the control API.  It reads immutable
MRP/COLMAP/LichtFeld inputs and writes only derived data under
``data/scene_repository/registries/site_records``.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
from generation_pipeline.tools.application_preview_api import PreviewService
from services.control_api.field_memory import FieldMemoryService  # noqa: E402
from services.control_api.evidence_ai import EvidenceAiService  # noqa: E402
from services.control_api.frame_analysis import (  # noqa: E402
    FrameAnalysisCancelled,
    FrameAnalysisService,
    RegisteredFrameAnalysisEngine,
    TransformersLocalVlmAnalyzer,
)
from services.vlm_model_catalog import DEFAULT_VLM, reviewed_vlm, reviewed_vlm_directory  # noqa: E402
from services.control_api.spatial_search import SpatialSearchService  # noqa: E402


def acquire_worker_lock(repo_root: Path):
    """Keep one local-AI queue consumer across integrated and standalone apps."""

    lock_path = repo_root / "var/spatial/locks/local-ai-worker.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    stream = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"waiting for active local AI worker: {lock_path}", flush=True)
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
    stream.seek(0)
    stream.truncate()
    stream.write(f"pid={os.getpid()}\n")
    stream.flush()
    print(f"acquired local AI worker lock: {lock_path}", flush=True)
    return stream


def _model_path(run: dict[str, Any], repo_root: Path, override: str | None) -> str:
    model = reviewed_vlm(str(run["model_id"]))
    if str(run["model_revision"]) != model.revision:
        raise ValueError(
            f"frame_analysis_model_revision_not_reviewed:{run['model_id']}@{run['model_revision']}"
        )
    # The legacy override names the historical default 8B directory.  Applying
    # it to every reviewed model would make a 2B/4B run silently open 8B
    # weights, so non-default models always resolve through the shared store.
    if override and model.model_id == DEFAULT_VLM.model_id:
        path = Path(override).expanduser()
        if not path.is_absolute():
            path = repo_root / path
        return str(path.resolve())
    return str(reviewed_vlm_directory(repo_root, model.model_id))


def _analyzer_for_model(
    model_path: str,
    analyzers: dict[str, TransformersLocalVlmAnalyzer],
) -> TransformersLocalVlmAnalyzer:
    analyzer = analyzers.get(model_path)
    if analyzer is not None:
        return analyzer
    # Keep at most one model resident.  Otherwise selecting a second model can
    # retain both sets of weights and cause an avoidable CUDA OOM.
    for loaded in analyzers.values():
        loaded.release()
    analyzers.clear()
    analyzer = TransformersLocalVlmAnalyzer(model_path)
    analyzers[model_path] = analyzer
    return analyzer


def model_artifact_evidence(model_path: Path) -> dict[str, Any]:
    """Hash a local model tree without recording host-specific absolute paths."""
    root = model_path.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"frame_analysis_local_model_not_found:{root}")
    files = sorted(path for path in root.rglob("*") if path.is_file())
    if not files:
        raise ValueError("frame_analysis_local_model_empty")
    tree = hashlib.sha256()
    total_bytes = 0
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(chunk)
                total_bytes += len(chunk)
        tree.update(len(relative).to_bytes(4, "big"))
        tree.update(relative)
        tree.update(digest.digest())
    return {
        "algorithm": "sha256-tree-v1",
        "tree_sha256": tree.hexdigest(),
        "file_count": len(files),
        "total_bytes": total_bytes,
    }


def process_one(
    service: FrameAnalysisService,
    repo_root: Path,
    model_override: str | None,
    analyzers: dict[str, TransformersLocalVlmAnalyzer],
) -> bool:
    run = service.claim_next_run()
    if run is None:
        return False
    try:
        model_path = Path(_model_path(run, repo_root, model_override))
        analyzer = _analyzer_for_model(str(model_path), analyzers)
        run = service.record_model_provenance(
            run["analysis_run_id"], model_artifact_evidence(model_path)
        )
        RegisteredFrameAnalysisEngine(repo_root, service, analyzer).process(run)
        print(f"completed {run['analysis_run_id']}", flush=True)
    except FrameAnalysisCancelled:
        # The control API already recorded a terminal cancellation state.  Do
        # not turn it into a second generic failure or alter its reason.
        print(f"cancelled {run['analysis_run_id']}", flush=True)
    except Exception as error:
        try:
            if service.cancellation_requested(run["analysis_run_id"]):
                print(f"cancelled {run['analysis_run_id']}", flush=True)
                return True
            service.fail_run(run["analysis_run_id"], f"{type(error).__name__}: {error}")
        finally:
            if not service.cancellation_requested(run["analysis_run_id"]):
                print(f"failed {run['analysis_run_id']}: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
    return True


def process_one_inquiry(
    service: EvidenceAiService,
    repo_root: Path,
    model_override: str | None,
    analyzers: dict[str, TransformersLocalVlmAnalyzer],
) -> bool:
    inquiry = service.claim_next()
    if inquiry is None:
        return False
    try:
        model = reviewed_vlm(str(inquiry["model_id"]))
        if inquiry["model_revision"] != model.revision:
            raise ValueError("evidence_ai_model_revision_not_reviewed")
        model_path = _model_path(inquiry, repo_root, model_override)
        analyzer = _analyzer_for_model(model_path, analyzers)
        output = analyzer.answer_evidence(inquiry["question"], inquiry["evidence_snapshot"])
        service.complete(inquiry["inquiry_id"], output)
        print(f"completed evidence {inquiry['inquiry_id']}", flush=True)
    except Exception as error:
        try:
            service.fail(inquiry["inquiry_id"], f"{type(error).__name__}: {error}")
        finally:
            print(
                f"failed evidence {inquiry['inquiry_id']}: {type(error).__name__}: {error}",
                file=sys.stderr, flush=True,
            )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--model-path", help="Local model directory; network model IDs are not accepted")
    parser.add_argument("--once", action="store_true", help="Process at most one queued run")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    worker_lock = acquire_worker_lock(repo_root)
    preview = PreviewService(repo_root)
    registry = FieldMemoryService(repo_root, scene_packages_provider=preview.scene_packages)
    service = FrameAnalysisService(registry)
    evidence = EvidenceAiService(registry, SpatialSearchService(registry))
    analyzers: dict[str, TransformersLocalVlmAnalyzer] = {}
    while True:
        # Keep the advisory-lock stream alive for the lifetime of the worker.
        assert not worker_lock.closed
        worked = process_one(service, repo_root, args.model_path, analyzers)
        if not worked:
            worked = process_one_inquiry(evidence, repo_root, args.model_path, analyzers)
        if args.once:
            return 0
        if not worked:
            time.sleep(max(0.1, args.poll_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
