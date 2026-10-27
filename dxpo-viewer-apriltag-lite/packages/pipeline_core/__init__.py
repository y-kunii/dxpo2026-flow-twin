"""Framework-independent pipeline ownership boundary.

The existing ``generation_pipeline`` package remains the compatibility source
path while its stages are migrated incrementally into this boundary.
"""

from pathlib import Path

from .project_identity import PROJECT_NAME_RE, project_name_from_workspace_uri
from .storage_lifecycle import RetentionPolicy, StorageLifecycle


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
LEGACY_PIPELINE_ROOT = REPOSITORY_ROOT / "generation_pipeline"

__all__ = [
    "LEGACY_PIPELINE_ROOT",
    "PROJECT_NAME_RE",
    "RetentionPolicy",
    "StorageLifecycle",
    "project_name_from_workspace_uri",
]
