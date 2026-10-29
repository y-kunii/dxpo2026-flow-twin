"""Canonical locations for MRP and Scene Package contracts.

Schemas remain in ``scene_spec`` to preserve all published v1.0/v1.1 paths.
New services depend on this facade rather than inventing another schema copy.
"""

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCENE_SPEC_ROOT = REPOSITORY_ROOT / "scene_spec"
SCHEMA_ROOT = SCENE_SPEC_ROOT / "schemas"

__all__ = ["SCENE_SPEC_ROOT", "SCHEMA_ROOT"]
