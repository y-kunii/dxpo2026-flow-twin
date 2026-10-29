"""Retirement guard for the mutable root Scene Package workflow.

The historical ``data/scene_packages/<scene_id>`` URI is resolved into
``data/scene_repository/packages/scenes/<scene_id>`` only for manifest
compatibility. The old mutable root package and
``web_viewer/app/public/scene_package`` v1.0 copy are not sources of truth and
must never be updated. New immutable Packages are built from a verified MRP
plus SPZ and published only to their package-ID runtime directory.
"""

from __future__ import annotations

from pathlib import Path


class LegacyRootScenePackageRetiredError(RuntimeError):
    """Raised before a caller can mutate a historical root Scene Package."""


def copy_root_scene_package_payload(source: Path, destination: Path) -> list[str]:
    """Refuse the retired root-payload copy operation without touching either path."""
    del source, destination
    raise LegacyRootScenePackageRetiredError(
        "legacy_root_scene_package_write_retired:use_verified_mrp_and_spz_import"
    )


def replace_root_scene_package_payload(source: Path, target: Path) -> list[str]:
    """Refuse the retired mutable-root publish operation without touching either path."""
    del source, target
    raise LegacyRootScenePackageRetiredError(
        "legacy_root_scene_package_write_retired:use_verified_mrp_and_spz_import"
    )
