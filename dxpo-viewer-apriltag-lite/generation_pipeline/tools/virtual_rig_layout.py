"""Dependency-light virtual camera layout definitions.

The renderer and its tests share these matrices.  They map camera rays to
equirectangular panorama directions using the renderer's existing convention.
"""

from __future__ import annotations

import math

import numpy as np


RIG_LAYOUT_COVERAGE_12 = "coverage_12"
RIG_LAYOUT_CUBE_6 = "cube_6"
CUBE_6_CAMERA_LABELS = ["front", "left", "back", "right", "bottom", "top"]


def _rotation_x(degrees: float) -> np.ndarray:
    radians = math.radians(degrees)
    cosine, sine = math.cos(radians), math.sin(radians)
    return np.asarray([[1.0, 0.0, 0.0], [0.0, cosine, -sine], [0.0, sine, cosine]])


def _rotation_y(degrees: float) -> np.ndarray:
    radians = math.radians(degrees)
    cosine, sine = math.cos(radians), math.sin(radians)
    return np.asarray([[cosine, 0.0, sine], [0.0, 1.0, 0.0], [-sine, 0.0, cosine]])


def cube6_rotations() -> list[np.ndarray]:
    """Return exact front, left, back, right, bottom and top cube faces."""
    return [
        np.identity(3),
        _rotation_y(-90.0),
        _rotation_y(-180.0),
        _rotation_y(-270.0),
        _rotation_x(90.0),
        _rotation_x(-90.0),
    ]
