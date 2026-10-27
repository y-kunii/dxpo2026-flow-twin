#!/usr/bin/env python3
"""Run one real, offline GPU inference against the reviewed local VLM."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.control_api.frame_analysis import TransformersLocalVlmAnalyzer


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("--model", type=Path, default=Path(".external/models/local-vlm"))
    args = parser.parse_args()
    if not args.image.is_file():
        raise SystemExit(f"input image is missing: {args.image}")
    result = TransformersLocalVlmAnalyzer(str(args.model)).analyze(
        Image.open(args.image), "spatial-frame-v1"
    )
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
