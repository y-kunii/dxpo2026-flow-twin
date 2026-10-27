#!/usr/bin/env python3
"""Publish a verified Proxy as a new immutable Scene Package edition."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.proxy_scene_package_edition import create_proxy_scene_package_edition  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create and publish a new proxy-enabled immutable Scene Package edition."
    )
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--source-scene-package-id", required=True)
    parser.add_argument(
        "--proxy-directory",
        type=Path,
        required=True,
        help="Directory containing verified proxy.glb, proxy_metadata.json, and proxy_quality.json.",
    )
    args = parser.parse_args()
    result = create_proxy_scene_package_edition(
        args.repo_root,
        args.source_scene_package_id,
        args.proxy_directory,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
