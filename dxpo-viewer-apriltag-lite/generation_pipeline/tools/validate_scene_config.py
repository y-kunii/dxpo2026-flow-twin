from __future__ import annotations

import argparse
import json
from pathlib import Path

if __package__:
    from .scene_config import SceneConfigError, load_scene_config
else:
    from scene_config import SceneConfigError, load_scene_config


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a repository-relative scene configuration.")
    parser.add_argument("--scene-config", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--check-paths", action="store_true")
    args = parser.parse_args()
    try:
        config = load_scene_config(
            args.scene_config.resolve(), repo_root=args.repo_root.resolve(), check_paths=args.check_paths
        )
    except SceneConfigError as error:
        print(json.dumps({"status": "failed", "failure_reasons": [str(error)]}, indent=2))
        return 2
    print(json.dumps({"status": "verified", "scene_id": config["scene_id"], "schema_version": config["schema_version"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
