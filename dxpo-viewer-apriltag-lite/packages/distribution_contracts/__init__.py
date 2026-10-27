"""Public distribution recipe contract."""

from .catalog import (
    BACKEND_COMPONENTS,
    PACKAGE_ID_RE,
    UI_MODULES,
    VIEWER_VARIANTS,
    normalize_package_recipe,
    package_catalog,
)
from .products import (
    PRODUCT_RELEASE_LIFECYCLE,
    load_product_profile,
    product_profiles,
    resolve_product_recipe,
)
from .release import (
    application_runtime_contract,
    build_provenance,
    require_formal_build_source,
)
from .artifacts import (
    create_scene_content_pack,
    install_scene_content_pack,
    read_content_pack_manifest,
    sha256_file,
)
from .lifecycle import (
    apply_retention_plan,
    acceptance_records,
    artifact_inventory,
    record_acceptance,
    release_records,
    retention_preview,
    transition_release,
)

__all__ = [
    "BACKEND_COMPONENTS",
    "PACKAGE_ID_RE",
    "UI_MODULES",
    "VIEWER_VARIANTS",
    "normalize_package_recipe",
    "package_catalog",
    "PRODUCT_RELEASE_LIFECYCLE",
    "application_runtime_contract",
    "build_provenance",
    "load_product_profile",
    "product_profiles",
    "require_formal_build_source",
    "resolve_product_recipe",
    "create_scene_content_pack",
    "install_scene_content_pack",
    "read_content_pack_manifest",
    "sha256_file",
    "acceptance_records",
    "apply_retention_plan",
    "artifact_inventory",
    "record_acceptance",
    "release_records",
    "retention_preview",
    "transition_release",
]
