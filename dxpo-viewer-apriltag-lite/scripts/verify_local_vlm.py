#!/usr/bin/env python3
"""Fail closed unless the reviewed local VLM snapshot is complete."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.vlm_model_catalog import DEFAULT_VLM, reviewed_vlm


def verify(path: Path, model_id: str | None = None) -> dict[str, object]:
    root = path.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"local_vlm_not_found:{root}")
    provenance_path = root / "SPATIAL_MODEL_PROVENANCE.json"
    if not provenance_path.is_file():
        raise ValueError("local_vlm_provenance_missing")
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    model = reviewed_vlm(model_id or str(provenance.get("model_id", DEFAULT_VLM.model_id)))
    expected = {
        "model_id": model.model_id,
        "revision": model.revision,
        "license": model.license_id,
        "parameter_scale": model.parameter_scale,
    }
    for key, value in expected.items():
        if provenance.get(key) != value:
            raise ValueError(f"local_vlm_provenance_mismatch:{key}")
    config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    model_type = config.get("model_type")
    if model_type != "qwen3_vl":
        raise ValueError(f"local_vlm_model_type_invalid:{model_type}")
    shards = sorted(root.glob("*.safetensors"))
    if not shards:
        raise ValueError("local_vlm_weight_shards_missing")
    total_bytes = sum(path.stat().st_size for path in shards)
    minimum_weight_bytes = int(model.approximate_download_gib * 1024**3 * 0.8)
    if total_bytes < minimum_weight_bytes:
        raise ValueError(f"local_vlm_weights_incomplete:{total_bytes}")
    if any(path.is_symlink() for path in root.rglob("*")):
        raise ValueError("local_vlm_symlink_not_allowed")
    return {
        **expected,
        "path": str(root),
        "weight_shards": len(shards),
        "weight_bytes": total_bytes,
        "status": "verified",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=Path(".external/models/local-vlm"))
    parser.add_argument("--model-id", help="Reviewed model ID; defaults to provenance model_id")
    args = parser.parse_args()
    print(json.dumps(verify(args.path, args.model_id), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
