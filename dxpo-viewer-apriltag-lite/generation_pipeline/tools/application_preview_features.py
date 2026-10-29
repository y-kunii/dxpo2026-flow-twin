"""Compatibility import for the control-plane feature contract.

New code imports ``services.control_api.features``.  This stable path remains
while generation-pipeline callers migrate without a flag day.
"""

from services.control_api.features import (  # noqa: F401
    ApplicationFeatureDisabled,
    KNOWN_FEATURES,
    parse_enabled_features,
    require_feature,
)

__all__ = [
    "ApplicationFeatureDisabled",
    "KNOWN_FEATURES",
    "parse_enabled_features",
    "require_feature",
]
