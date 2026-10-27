#!/usr/bin/env python3
"""Validate representative SPZ, PLY and RAD assets without importing them."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from generation_pipeline.tools.application_services.scene_import import SceneImportService  # noqa: E402


REQUIRED_FORMATS = frozenset({"spz", "ply", "rad"})


def verify(paths: list[Path]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for path in paths:
        try:
            evidence = SceneImportService._validate_gaussian_asset(path.resolve())
            results.append({"path": str(path.resolve()), **evidence})
        except (OSError, ValueError) as error:
            results.append({"path": str(path.resolve()), "status": "failed", "error": str(error)})
    formats = {str(item.get("format")) for item in results if item.get("status") == "verified"}
    missing = sorted(REQUIRED_FORMATS - formats)
    failed = [str(item["path"]) for item in results if item.get("status") != "verified"]
    return {
        "schema_version": "gaussian-format-acceptance-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "verified" if not missing and not failed else "incomplete",
        "accepted_formats": sorted(formats),
        "missing_formats": missing,
        "failed_assets": failed,
        "assets": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("assets", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = verify(args.assets)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    print(json.dumps({"status": report["status"], "report": str(output)}, ensure_ascii=False))
    return 0 if report["status"] == "verified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
