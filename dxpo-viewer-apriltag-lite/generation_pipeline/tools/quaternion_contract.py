"""Shared strict quaternion validation for persisted coordinate contracts.

External artifacts use the ROS/Scene Package component order ``x, y, z, w``.
They must already be unit quaternions: callers may use normalized values that
they computed locally, but must never silently repair a persisted input.
"""

from __future__ import annotations

import math


QUATERNION_UNIT_NORM_TOLERANCE = 1e-6


def quaternion_rejection_reason(value: object) -> str | None:
    """Return the contract rejection reason for an external xyzw quaternion."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return "shape_invalid"
    if any(isinstance(component, bool) or not isinstance(component, (int, float)) for component in value):
        return "component_not_numeric"
    if not all(math.isfinite(float(component)) for component in value):
        return "component_non_finite"
    norm = math.sqrt(sum(float(component) ** 2 for component in value))
    if norm <= 1e-12:
        return "zero_quaternion"
    if abs(norm - 1.0) > QUATERNION_UNIT_NORM_TOLERANCE:
        return "quaternion_not_normalized"
    return None


def require_unit_quaternion_xyzw(value: object, field: str) -> tuple[float, float, float, float]:
    """Validate and return an external unit quaternion without normalizing it.

    ``field`` is part of the error so a failed MRP or Scene Package validation
    identifies the exact persisted input that needs correction.
    """
    reason = quaternion_rejection_reason(value)
    if reason is not None:
        raise ValueError(f"{field}:{reason}")
    assert isinstance(value, (list, tuple))
    return tuple(float(component) for component in value)  # type: ignore[return-value]
