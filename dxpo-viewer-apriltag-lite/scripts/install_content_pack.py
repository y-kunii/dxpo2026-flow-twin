#!/usr/bin/env python3
"""Install one verified Content Pack beside a split Runtime Bundle."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.distribution_contracts.artifacts import install_scene_content_pack


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("content_pack", type=Path)
    args = parser.parse_args()
    result = install_scene_content_pack(args.content_pack.expanduser().resolve(), ROOT)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
