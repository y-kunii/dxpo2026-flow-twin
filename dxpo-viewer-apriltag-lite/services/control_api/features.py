"""Fail-closed feature boundary for composed and standalone applications."""

from __future__ import annotations

from collections.abc import Iterable


KNOWN_FEATURES = frozenset({
    "suite",
    "pipeline",
    "apriltag-check",
    "spz-import",
    "scene-manager",
    "scene-reader",
    "project-manager",
    "package-builder",
    "field-memory",
    "spatial-search",
    "frame-analysis",
    "evidence-ai",
    "change-review",
})


class ApplicationFeatureDisabled(PermissionError):
    """The selected standalone app does not expose this API capability."""


def parse_enabled_features(value: str | Iterable[str] | None) -> frozenset[str]:
    if value is None:
        return frozenset({"suite"})
    raw = value.split(",") if isinstance(value, str) else list(value)
    features = frozenset(str(item).strip() for item in raw if str(item).strip())
    if not features:
        raise ValueError("application_feature_required")
    unknown = sorted(features - KNOWN_FEATURES)
    if unknown:
        raise ValueError(f"application_feature_unknown:{','.join(unknown)}")
    if "suite" in features and len(features) != 1:
        raise ValueError("suite_feature_must_be_exclusive")
    return features


def require_feature(enabled: frozenset[str], *accepted: str) -> None:
    if "suite" in enabled or enabled.intersection(accepted):
        return
    required = ",".join(accepted)
    raise ApplicationFeatureDisabled(f"api_feature_not_enabled:requires={required}")
