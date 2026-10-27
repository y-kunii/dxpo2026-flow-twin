"""Dedicated worker for rebuildable field-memory image indexes."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from services.control_api.field_memory import FieldMemoryService
from services.control_api.image_features import ImageFeatureIndexService


class FeatureIndexWorker:
    def __init__(self, repo_root: Path, *, require_faiss: bool = True) -> None:
        self.index = ImageFeatureIndexService(FieldMemoryService(repo_root))
        self.require_faiss = require_faiss

    def run_once(self) -> bool:
        job = self.index.claim_next()
        if job is None:
            return False
        try:
            count = self.index.rebuild(job["site_id"], require_faiss=self.require_faiss)
        except BaseException as error:
            self.index.fail(job["job_id"], error)
        else:
            self.index.complete(job["job_id"], count)
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--allow-numpy-test-fallback", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    worker = FeatureIndexWorker(args.repo_root.resolve(), require_faiss=not args.allow_numpy_test_fallback)
    if args.once:
        worker.run_once()
        return 0
    while True:
        if not worker.run_once():
            time.sleep(max(args.poll_seconds, 0.1))


if __name__ == "__main__":
    raise SystemExit(main())
