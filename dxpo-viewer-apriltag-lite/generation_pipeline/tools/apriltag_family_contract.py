"""Supported AprilTag families for App A metric reconstruction.

The list intentionally matches the families exposed by the pinned
``pupil-apriltags==1.0.4.post11`` detector.  Keep UI discovery, API input
validation, and pipeline execution on this one contract so a family can never
be selectable but unusable at detection time.
"""

from __future__ import annotations

from typing import Any


DEFAULT_APRILTAG_FAMILY = "tag25h9"

APRILTAG_FAMILY_OPTIONS: tuple[tuple[str, str], ...] = (
    ("tag16h5", "tag16h5"),
    ("tag25h9", "tag25h9（既定）"),
    ("tag36h11", "tag36h11"),
    ("tagCircle21h7", "tagCircle21h7（円形レイアウト）"),
    ("tagCircle49h12", "tagCircle49h12（円形レイアウト）"),
    ("tagCustom48h12", "tagCustom48h12（中央穴レイアウト）"),
    ("tagStandard41h12", "tagStandard41h12（AprilTag公式推奨）"),
    ("tagStandard52h13", "tagStandard52h13（大規模ID向け）"),
)

SUPPORTED_APRILTAG_FAMILIES = frozenset(value for value, _label in APRILTAG_FAMILY_OPTIONS)


def require_supported_apriltag_family(value: Any) -> str:
    """Return a supported family or fail closed with an actionable reason."""
    if not isinstance(value, str) or value not in SUPPORTED_APRILTAG_FAMILIES:
        supported = ",".join(option for option, _label in APRILTAG_FAMILY_OPTIONS)
        raise ValueError(f"tag_family_unsupported:{value!s};supported={supported}")
    return value


def apriltag_family_catalog() -> dict[str, Any]:
    """Return the JSON-safe family catalog consumed by Application Preview."""
    return {
        "default_family": DEFAULT_APRILTAG_FAMILY,
        "families": [
            {"value": value, "label": label}
            for value, label in APRILTAG_FAMILY_OPTIONS
        ],
    }
