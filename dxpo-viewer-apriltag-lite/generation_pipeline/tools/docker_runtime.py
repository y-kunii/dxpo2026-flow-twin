"""Shared immutable Docker runtime settings for App A child containers."""

from __future__ import annotations

import os
from pathlib import Path


PINNED_COLMAP_IMAGE = "colmap/colmap@sha256:b809882552887b6471094dcadd2f2eb01656b010663564c43a5e7f04c0a08f2f"


def docker_workspace_arguments(repo_root: Path) -> list[str]:
    """Map source read-only and only the owned data/runtime roots writable.

    A host-launched runner can bind ``repo_root`` directly.  The Compose API
    runs inside a container, so its startup wrapper discovers the host source
    of the existing ``/workspace`` bind and exposes it through the environment.
    """
    source = os.environ.get("SPATIAL_DOCKER_WORKSPACE_SOURCE", str(repo_root.resolve()))
    if not source or any(character in source for character in ("\n", "\r", "\0")):
        raise RuntimeError("docker_workspace_source_invalid")
    return [
        "-v", f"{source}:/workspace:ro",
        "-v", f"{source}/data:/workspace/data",
        "-v", f"{source}/var/spatial:/workspace/var/spatial",
        "-w", "/workspace",
    ]
