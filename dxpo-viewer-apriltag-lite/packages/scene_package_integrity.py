"""Cross-process integrity helpers for Scene Package registry operations."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
from pathlib import Path
import re
from typing import Iterator

from packages.scene_repository import SceneRepositoryLayout


SCENE_PACKAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@contextmanager
def scene_package_reference_lock(root: Path, scene_package_id: str) -> Iterator[None]:
    """Serialize package removal with Field Memory SceneVersion creation.

    The filesystem and SQLite registry cannot share a foreign key, so both
    writers use this advisory lock around their final existence/reference
    checks and mutation.
    """

    if SCENE_PACKAGE_ID_RE.fullmatch(scene_package_id) is None:
        raise ValueError("invalid_scene_package_id")
    lock_root = SceneRepositoryLayout.from_root(root).locks
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / f"scene-package-reference-{scene_package_id}.lock"
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
