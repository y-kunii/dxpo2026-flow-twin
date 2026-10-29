#!/usr/bin/env python3
"""Fail when tracked files or root-local artifacts violate repository layout."""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]

ALLOWED_TRACKED_ROOT_DIRECTORIES = frozenset(
    {
        ".github",
        "apps",
        "commercial_license",
        "data",
        "docker",
        "docs",
        "examples",
        "generation_pipeline",
        "infra",
        "packages",
        "sample_scene_package",
        "scene_spec",
        "scripts",
        "services",
        "tests",
        "web_viewer",
        "var",
    }
)
ALLOWED_TRACKED_ROOT_FILES = frozenset(
    {
        ".dockerignore",
        ".coveragerc",
        ".gitignore",
        ".pre-commit-config.yaml",
        "README.md",
        "SETUP.md",
        "SECURITY.md",
        "THIRD_PARTY_NOTICES.md",
        "LICENSE",
        "docker-compose.yml",
        "compose.yaml",
        "mypy.ini",
        "pytest.ini",
        "ruff.toml",
    }
)
ALLOWED_LOCAL_ROOT_DIRECTORIES = ALLOWED_TRACKED_ROOT_DIRECTORIES | frozenset(
    {".external", ".git", ".idea", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", ".vscode"}
)
ALLOWED_LOCAL_ROOT_FILES = ALLOWED_TRACKED_ROOT_FILES | frozenset({".coverage", ".DS_Store", ".env"})
LARGE_ARTIFACT_SUFFIXES = frozenset(
    {".glb", ".gltf", ".ksplat", ".ply", ".rad", ".sog", ".sogs", ".spz", ".zip"}
)
ALLOWED_TRACKED_DATA_PATHS = frozenset(
    {
        "data/scene_repository/README.md",
        "var/spatial/.gitkeep",
    }
)
FORBIDDEN_TRACKED_PREFIXES = (
    PurePosixPath("web_viewer/app/.vite"),
    PurePosixPath("web_viewer/app/dist"),
    PurePosixPath("web_viewer/app/node_modules"),
    PurePosixPath("web_viewer/app/public/scene_package/runtime"),
)


def _normalized_paths(paths: Iterable[str]) -> tuple[PurePosixPath, ...]:
    return tuple(PurePosixPath(value.replace("\\", "/")) for value in paths if value)


def collect_violations(
    tracked_paths: Iterable[str],
    root_entry_names: Iterable[str],
) -> list[str]:
    """Return deterministic repository-layout violations."""

    violations: list[str] = []
    normalized = _normalized_paths(tracked_paths)

    for path in normalized:
        root_name = path.parts[0]
        if len(path.parts) == 1:
            if root_name not in ALLOWED_TRACKED_ROOT_FILES:
                violations.append(f"unexpected tracked root file: {path}")
        elif root_name not in ALLOWED_TRACKED_ROOT_DIRECTORIES:
            violations.append(f"unexpected tracked root directory: {root_name}")

        if ".external" in path.parts:
            violations.append(f"local external adapter content must not be tracked: {path}")
        if "__pycache__" in path.parts or any(
            path == prefix or prefix in path.parents for prefix in FORBIDDEN_TRACKED_PREFIXES
        ):
            violations.append(f"generated directory content must not be tracked: {path}")
        if path.suffix.lower() in LARGE_ARTIFACT_SUFFIXES:
            violations.append(f"large/generated artifact must not be tracked: {path}")
        if root_name == "data" and str(path) not in ALLOWED_TRACKED_DATA_PATHS:
            violations.append(f"data artifact must not be tracked: {path}")

    for name in sorted(set(root_entry_names)):
        lower_name = name.lower()
        if name in ALLOWED_LOCAL_ROOT_DIRECTORIES or name in ALLOWED_LOCAL_ROOT_FILES:
            continue
        if lower_name.startswith("counterapp"):
            violations.append(
                f"external CounterApp copy is misplaced at repository root: {name}; "
                "place it under .external/ or outside the repository"
            )
        elif Path(name).suffix.lower() in LARGE_ARTIFACT_SUFFIXES:
            violations.append(
                f"local archive/artifact is misplaced at repository root: {name}; "
                "place it under .external/, data/scene_repository/deliverables/, or outside the repository"
            )
        else:
            violations.append(f"unexpected local entry at repository root: {name}")

    return sorted(set(violations))


def _tracked_paths(repo_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    )
    # A layout migration can remove tracked placeholders before the operator
    # stages the deletion. Validate the filesystem that will be committed,
    # not deleted index entries that no longer exist in the worktree.
    return [
        value
        for value in result.stdout.splitlines()
        if (repo_root / value).exists()
    ]


def main() -> int:
    violations = collect_violations(
        _tracked_paths(REPO_ROOT),
        (entry.name for entry in REPO_ROOT.iterdir()),
    )
    if violations:
        print("Repository structure: FAILED")
        for violation in violations:
            print(f"- {violation}")
        return 1

    print("Repository structure: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
