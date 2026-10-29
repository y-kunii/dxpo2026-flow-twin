"""Backward-compatible import path for application distribution contracts."""

from packages.distribution_contracts.catalog import (
    BACKEND_COMPONENTS,
    PACKAGE_ID_RE,
    UI_MODULES,
    normalize_package_recipe,
    package_catalog,
)

__all__ = [
    "BACKEND_COMPONENTS",
    "PACKAGE_ID_RE",
    "UI_MODULES",
    "normalize_package_recipe",
    "package_catalog",
]
