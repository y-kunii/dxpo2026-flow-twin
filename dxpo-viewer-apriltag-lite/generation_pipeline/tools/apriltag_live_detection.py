"""In-memory AprilTag detection and camera-relative 6DoF pose estimation.

Frames are decoded, processed, and discarded.  No camera image or detection
result is written to the repository.  Only the currently selected family's
detector is retained because some tag dictionaries have a large native-memory
footprint in the pinned pupil-apriltags binding.
"""

from __future__ import annotations

import gc
import io
import math
import threading
import time
from copy import deepcopy
from collections.abc import Callable
from typing import Any

import numpy as np
from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError

try:
    import cv2
except ImportError:  # pragma: no cover - generation image verification requires it
    cv2 = None

if __package__:
    from .apriltag_family_contract import require_supported_apriltag_family
else:
    from apriltag_family_contract import require_supported_apriltag_family


MAX_LIVE_FRAME_BYTES = 8 * 1024 * 1024
MAX_LIVE_FRAME_PIXELS = 4096 * 4096
MAX_OPTICAL_FLOW_MISSES = 30
MAX_OPTICAL_FLOW_AGE_SECONDS = 2.5
MAX_OPTICAL_FLOW_FB_ERROR_PX = 3.5
MIN_OPTICAL_FLOW_QUAD_AREA_PX = 64.0
MAX_OPTICAL_FLOW_FEATURES = 64
MIN_OPTICAL_FLOW_FEATURES = 4
MIN_HOMOGRAPHY_INLIERS = 4
MIN_HOMOGRAPHY_INLIER_RATIO = 0.5
HOMOGRAPHY_RANSAC_THRESHOLD_PX = 3.0
MIN_TEMPLATE_SEED_SCORE = 0.15
MAX_DETECTION_HAMMING = 1
MIN_DETECTION_DECISION_MARGIN = 10.0


def _finite_float(value: Any, label: str, *, positive: bool = False) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label}_invalid") from error
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError(f"{label}_invalid")
    return result


def _distortion_coefficients(value: Any) -> tuple[float, ...]:
    if value in (None, "", []):
        return ()
    items = value.split(",") if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)) or len(items) not in {4, 5, 8}:
        raise ValueError("camera_distortion_coefficients_invalid")
    return tuple(_finite_float(item, f"camera_distortion_coefficients_{index}") for index, item in enumerate(items))


def _distort_record_pixels(
    record: dict[str, Any],
    camera_params: tuple[float, float, float, float],
    distortion: tuple[float, ...],
) -> dict[str, Any]:
    """Map undistorted detector coordinates back to the captured image."""
    if not distortion:
        return record
    fx, fy, cx, cy = camera_params
    values = list(distortion) + [0.0] * (8 - len(distortion))
    k1, k2, p1, p2, k3, k4, k5, k6 = values

    def point(pixel: Any) -> list[float]:
        u, v = float(pixel[0]), float(pixel[1])
        x, y = (u - cx) / fx, (v - cy) / fy
        r2 = x * x + y * y
        r4, r6 = r2 * r2, r2 * r2 * r2
        denominator = 1.0 + k4 * r2 + k5 * r4 + k6 * r6
        if abs(denominator) < 1e-12:
            raise ValueError("camera_distortion_projection_invalid")
        radial = (1.0 + k1 * r2 + k2 * r4 + k3 * r6) / denominator
        xd = x * radial + 2.0 * p1 * x * y + p2 * (r2 + 2.0 * x * x)
        yd = y * radial + p1 * (r2 + 2.0 * y * y) + 2.0 * p2 * x * y
        return [fx * xd + cx, fy * yd + cy]

    output = deepcopy(record)
    corners = [point(item) for item in record["corners_px"]]
    output["corners_px"] = corners
    output["center_px"] = point(record["center_px"])
    xs, ys = [item[0] for item in corners], [item[1] for item in corners]
    output["bbox_px"] = {"x": min(xs), "y": min(ys), "width": max(xs) - min(xs), "height": max(ys) - min(ys)}
    return output


def _rotation_matrix_to_quaternion_xyzw(matrix: np.ndarray) -> list[float]:
    """Convert a finite proper 3x3 rotation matrix to normalized XYZW."""
    if matrix.shape != (3, 3) or not np.all(np.isfinite(matrix)):
        raise ValueError("apriltag_pose_rotation_invalid")
    trace = float(np.trace(matrix))
    if trace > 0:
        scale = math.sqrt(trace + 1.0) * 2.0
        w = 0.25 * scale
        x = (matrix[2, 1] - matrix[1, 2]) / scale
        y = (matrix[0, 2] - matrix[2, 0]) / scale
        z = (matrix[1, 0] - matrix[0, 1]) / scale
    elif matrix[0, 0] > matrix[1, 1] and matrix[0, 0] > matrix[2, 2]:
        scale = math.sqrt(max(0.0, 1.0 + matrix[0, 0] - matrix[1, 1] - matrix[2, 2])) * 2.0
        x = 0.25 * scale
        y = (matrix[0, 1] + matrix[1, 0]) / scale
        z = (matrix[0, 2] + matrix[2, 0]) / scale
        w = (matrix[2, 1] - matrix[1, 2]) / scale
    elif matrix[1, 1] > matrix[2, 2]:
        scale = math.sqrt(max(0.0, 1.0 + matrix[1, 1] - matrix[0, 0] - matrix[2, 2])) * 2.0
        x = (matrix[0, 1] + matrix[1, 0]) / scale
        y = 0.25 * scale
        z = (matrix[1, 2] + matrix[2, 1]) / scale
        w = (matrix[0, 2] - matrix[2, 0]) / scale
    else:
        scale = math.sqrt(max(0.0, 1.0 + matrix[2, 2] - matrix[0, 0] - matrix[1, 1])) * 2.0
        x = (matrix[0, 2] + matrix[2, 0]) / scale
        y = (matrix[1, 2] + matrix[2, 1]) / scale
        z = 0.25 * scale
        w = (matrix[1, 0] - matrix[0, 1]) / scale
    quaternion = np.asarray([x, y, z, w], dtype=float)
    norm = float(np.linalg.norm(quaternion))
    if not math.isfinite(norm) or norm <= 1e-12:
        raise ValueError("apriltag_pose_rotation_invalid")
    return [float(value) for value in quaternion / norm]


def _rotation_matrix_to_rpy_degrees(matrix: np.ndarray) -> list[float]:
    """Return fixed-axis roll(X), pitch(Y), yaw(Z) angles in degrees."""
    pitch = math.asin(max(-1.0, min(1.0, -float(matrix[2, 0]))))
    if abs(math.cos(pitch)) > 1e-8:
        roll = math.atan2(float(matrix[2, 1]), float(matrix[2, 2]))
        yaw = math.atan2(float(matrix[1, 0]), float(matrix[0, 0]))
    else:
        roll = math.atan2(-float(matrix[1, 2]), float(matrix[1, 1]))
        yaw = 0.0
    return [math.degrees(value) for value in (roll, pitch, yaw)]


class AprilTagLiveDetector:
    """Thread-safe cache and processing boundary for browser camera frames."""

    def __init__(self, detector_factory: Callable[[str], Any] | None = None) -> None:
        self._detector_factory = detector_factory
        self._active_family: str | None = None
        self._active_detector: Any = None
        self._previous_gray: np.ndarray | None = None
        self._tracks: dict[str, dict[str, Any]] = {}
        self._tracking_signature: tuple[Any, ...] | None = None
        self._frame_sequence = 0
        self._lock = threading.Lock()

    def _new_detector(self, family: str) -> Any:
        if self._detector_factory is not None:
            return self._detector_factory(family)
        try:
            from pupil_apriltags import Detector
        except ImportError as error:
            raise RuntimeError("pupil_apriltags_not_installed") from error
        return Detector(
            families=family,
            nthreads=2,
            quad_decimate=1.0,
            quad_sigma=0.0,
            refine_edges=1,
            decode_sharpening=0.5,
            debug=0,
        )

    def _reset_tracking(self) -> None:
        self._previous_gray = None
        self._tracks.clear()
        self._tracking_signature = None

    def _detector(self, family: str) -> Any:
        if self._active_family != family or self._active_detector is None:
            # Release the previous family's native lookup tables before
            # constructing the next one. Keeping all supported families alive
            # can consume several GiB with the pinned detector implementation.
            self._active_detector = None
            self._active_family = None
            self._reset_tracking()
            gc.collect()
            self._active_detector = self._new_detector(family)
            self._active_family = family
        return self._active_detector

    @staticmethod
    def _decode_frame(frame: bytes) -> np.ndarray:
        if not frame or len(frame) > MAX_LIVE_FRAME_BYTES:
            raise ValueError("apriltag_live_frame_size_invalid")
        try:
            with Image.open(io.BytesIO(frame)) as image:
                if image.format not in {"JPEG", "PNG", "WEBP"}:
                    raise ValueError("apriltag_live_frame_format_invalid")
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > MAX_LIVE_FRAME_PIXELS:
                    raise ValueError("apriltag_live_frame_dimensions_invalid")
                gray = np.asarray(image.convert("L"), dtype=np.uint8)
        except (UnidentifiedImageError, OSError) as error:
            raise ValueError("apriltag_live_frame_decode_failed") from error
        return gray

    @staticmethod
    def _motion_enhanced_frame(gray: np.ndarray) -> np.ndarray:
        """Increase cell/edge separation for a second, miss-only decode pass."""
        image = Image.fromarray(gray, mode="L")
        contrasted = ImageOps.autocontrast(image, cutoff=1)
        sharpened = contrasted.filter(ImageFilter.UnsharpMask(radius=1.0, percent=180, threshold=2))
        return np.asarray(sharpened, dtype=np.uint8)

    @staticmethod
    def _pose(detection: Any) -> dict[str, Any] | None:
        if detection.pose_R is None or detection.pose_t is None:
            return None
        rotation = np.asarray(detection.pose_R, dtype=float)
        translation = np.asarray(detection.pose_t, dtype=float).reshape(-1)
        if translation.shape != (3,) or not np.all(np.isfinite(translation)):
            raise ValueError("apriltag_pose_translation_invalid")
        quaternion = _rotation_matrix_to_quaternion_xyzw(rotation)
        error_array = np.asarray(detection.pose_err, dtype=float).reshape(-1)
        pose_error = float(error_array[0]) if error_array.size and math.isfinite(float(error_array[0])) else None
        return {
            "frame": "camera_optical",
            "translation_m": [float(value) for value in translation],
            "rotation_quaternion_xyzw": quaternion,
            "rotation_rpy_deg": _rotation_matrix_to_rpy_degrees(rotation),
            "rotation_matrix_row_major": [float(value) for value in rotation.reshape(-1)],
            "pose_error": pose_error,
        }

    @classmethod
    def _record(
        cls,
        detection: Any,
        requested_family: str,
        *,
        tracking_state: str = "detected",
    ) -> dict[str, Any]:
        raw_family = detection.tag_family.decode("utf-8") if isinstance(detection.tag_family, bytes) else str(detection.tag_family)
        family = require_supported_apriltag_family(raw_family or requested_family)
        corners = np.asarray(detection.corners, dtype=float)
        center = np.asarray(detection.center, dtype=float).reshape(-1)
        if corners.shape != (4, 2) or center.shape != (2,) or not np.all(np.isfinite(corners)) or not np.all(np.isfinite(center)):
            raise ValueError("apriltag_detection_geometry_invalid")
        minimum = corners.min(axis=0)
        maximum = corners.max(axis=0)
        return {
            "family": family,
            "tag_id": int(detection.tag_id),
            "hamming": int(detection.hamming),
            "decision_margin": float(detection.decision_margin),
            "center_px": [float(value) for value in center],
            "corners_px": [[float(value) for value in point] for point in corners],
            "bbox_px": {
                "x": float(minimum[0]),
                "y": float(minimum[1]),
                "width": float(maximum[0] - minimum[0]),
                "height": float(maximum[1] - minimum[1]),
            },
            "pose_camera_tag": cls._pose(detection),
            "tracking_state": tracking_state,
            "tracking_age_frames": 0,
        }

    @classmethod
    def _records_from_detections(
        cls,
        detections: list[Any],
        requested_family: str,
        *,
        source: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        records: list[dict[str, Any]] = []
        rejections: list[dict[str, Any]] = []
        for detection in detections:
            try:
                hamming = int(detection.hamming)
                decision_margin = float(detection.decision_margin)
            except (AttributeError, TypeError, ValueError):
                hamming = -1
                decision_margin = math.nan
            reason: str | None = None
            if hamming < 0 or not math.isfinite(decision_margin):
                reason = "detection_quality_invalid"
            elif hamming > MAX_DETECTION_HAMMING:
                reason = "hamming_too_high"
            elif decision_margin < MIN_DETECTION_DECISION_MARGIN:
                reason = "decision_margin_too_low"
            if reason is not None:
                raw_family = getattr(detection, "tag_family", requested_family)
                family = raw_family.decode("utf-8", errors="replace") if isinstance(raw_family, bytes) else str(raw_family)
                try:
                    tag_id: int | None = int(detection.tag_id)
                except (AttributeError, TypeError, ValueError):
                    tag_id = None
                rejections.append({
                    "source": source,
                    "family": family,
                    "tag_id": tag_id,
                    "hamming": hamming,
                    "decision_margin": decision_margin if math.isfinite(decision_margin) else None,
                    "reason": reason,
                })
                continue
            tracking_state = "enhanced" if source == "enhanced" else "detected"
            records.append(cls._record(detection, requested_family, tracking_state=tracking_state))
        return records, rejections

    @staticmethod
    def _quad_area(corners: np.ndarray) -> float:
        x = corners[:, 0]
        y = corners[:, 1]
        return abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))) * 0.5

    @staticmethod
    def _signed_quad_area(corners: np.ndarray) -> float:
        x = corners[:, 0]
        y = corners[:, 1]
        return float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) * 0.5

    @staticmethod
    def _feature_points(gray: np.ndarray, corners: np.ndarray) -> np.ndarray:
        """Return trackable points restricted to the decoded tag face."""
        if cv2 is None:
            return np.asarray(corners, dtype=np.float32).reshape(-1, 2)
        polygon = np.rint(corners).astype(np.int32).reshape(-1, 1, 2)
        mask = np.zeros(gray.shape, dtype=np.uint8)
        cv2.fillConvexPoly(mask, polygon, 255)
        features = cv2.goodFeaturesToTrack(
            gray,
            maxCorners=MAX_OPTICAL_FLOW_FEATURES,
            qualityLevel=0.01,
            minDistance=3.0,
            mask=mask,
            blockSize=5,
            useHarrisDetector=False,
        )
        candidates: list[np.ndarray] = []
        if features is not None:
            candidates.extend(features.reshape(-1, 2))
        # The decoded corners preserve a four-point fallback for very small
        # tags while the interior features provide rotation/perspective
        # resilience for normal-sized tags.
        candidates.extend(np.asarray(corners, dtype=np.float32).reshape(-1, 2))
        selected: list[np.ndarray] = []
        for candidate in candidates:
            point = np.asarray(candidate, dtype=np.float32)
            if not np.all(np.isfinite(point)):
                continue
            if any(float(np.linalg.norm(point - existing)) < 2.0 for existing in selected):
                continue
            selected.append(point)
        return np.asarray(selected, dtype=np.float32).reshape(-1, 2)

    @staticmethod
    def _translation_seed(
        previous_gray: np.ndarray,
        gray: np.ndarray,
        corners: np.ndarray,
    ) -> tuple[np.ndarray, float | None]:
        """Estimate coarse motion so LK can converge after a large frame jump."""
        if cv2 is None:
            return np.zeros(2, dtype=np.float32), None
        minimum = np.floor(corners.min(axis=0)).astype(int) - 2
        maximum = np.ceil(corners.max(axis=0)).astype(int) + 3
        x0 = max(0, int(minimum[0]))
        y0 = max(0, int(minimum[1]))
        x1 = min(int(previous_gray.shape[1]), int(maximum[0]))
        y1 = min(int(previous_gray.shape[0]), int(maximum[1]))
        if x1 - x0 < 8 or y1 - y0 < 8:
            return np.zeros(2, dtype=np.float32), None
        template = previous_gray[y0:y1, x0:x1]
        margin = max(48, int(round(max(x1 - x0, y1 - y0) * 2.5)))
        search_x0 = max(0, x0 - margin)
        search_y0 = max(0, y0 - margin)
        search_x1 = min(int(gray.shape[1]), x1 + margin)
        search_y1 = min(int(gray.shape[0]), y1 + margin)
        search = gray[search_y0:search_y1, search_x0:search_x1]
        if search.shape[0] < template.shape[0] or search.shape[1] < template.shape[1]:
            return np.zeros(2, dtype=np.float32), None
        response = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
        _minimum_score, maximum_score, _minimum_location, maximum_location = cv2.minMaxLoc(response)
        score = float(maximum_score)
        if not math.isfinite(score) or score < MIN_TEMPLATE_SEED_SCORE:
            return np.zeros(2, dtype=np.float32), score if math.isfinite(score) else None
        matched_x = search_x0 + int(maximum_location[0])
        matched_y = search_y0 + int(maximum_location[1])
        return np.asarray([matched_x - x0, matched_y - y0], dtype=np.float32), score

    @classmethod
    def _valid_flow_geometry(
        cls,
        previous: np.ndarray,
        current: np.ndarray,
        *,
        width: int,
        height: int,
    ) -> bool:
        if current.shape != (4, 2) or not np.all(np.isfinite(current)):
            return False
        if np.any(current[:, 0] < -2) or np.any(current[:, 0] > width + 2):
            return False
        if np.any(current[:, 1] < -2) or np.any(current[:, 1] > height + 2):
            return False
        previous_area = cls._quad_area(previous)
        current_area = cls._quad_area(current)
        if previous_area < MIN_OPTICAL_FLOW_QUAD_AREA_PX or current_area < MIN_OPTICAL_FLOW_QUAD_AREA_PX:
            return False
        if cls._signed_quad_area(previous) * cls._signed_quad_area(current) <= 0:
            return False
        area_ratio = current_area / previous_area
        if not 0.45 <= area_ratio <= 2.2:
            return False
        edges = np.linalg.norm(current - np.roll(current, -1, axis=0), axis=1)
        if not np.all(edges >= 5.0):
            return False
        if cv2 is not None and not cv2.isContourConvex(np.rint(current).astype(np.int32)):
            return False
        return True

    @staticmethod
    def _propagate_pose(
        record: dict[str, Any],
        previous_corners: np.ndarray,
        current_corners: np.ndarray,
        camera_params: tuple[float, float, float, float],
    ) -> dict[str, Any] | None:
        pose = record.get("pose_camera_tag")
        if not isinstance(pose, dict):
            return None
        translation = np.asarray(pose.get("translation_m"), dtype=float).reshape(-1)
        if translation.shape != (3,) or not np.all(np.isfinite(translation)):
            return None
        previous_area = AprilTagLiveDetector._quad_area(previous_corners)
        current_area = AprilTagLiveDetector._quad_area(current_corners)
        if previous_area <= 0 or current_area <= 0:
            return None
        fx, fy, cx, cy = camera_params
        depth = float(translation[2]) * math.sqrt(previous_area / current_area)
        center = current_corners.mean(axis=0)
        propagated = deepcopy(pose)
        propagated["translation_m"] = [
            float((center[0] - cx) * depth / fx),
            float((center[1] - cy) * depth / fy),
            depth,
        ]
        propagated["pose_error"] = None
        propagated["estimated_from_image_tracking"] = True
        return propagated

    def _optical_flow_records(
        self,
        gray: np.ndarray,
        detected_keys: set[str],
        camera_params: tuple[float, float, float, float],
        now: float,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if cv2 is None or self._previous_gray is None or self._previous_gray.shape != gray.shape:
            return [], []
        recovered: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        expired: list[str] = []
        for key, track in self._tracks.items():
            misses = int(track["misses"])
            age_seconds = now - float(track["last_decoded_at"])
            diagnostic: dict[str, Any] = {
                "track_key": key,
                "previous_misses": misses,
                "decoded_age_ms": round(age_seconds * 1000.0, 3),
                "outcome": "pending",
                "failure_stage": None,
                "failure_reason": None,
            }

            def reject(
                stage: str,
                reason: str,
                *,
                item: dict[str, Any] = diagnostic,
                track_key: str = key,
            ) -> None:
                item["outcome"] = "rejected"
                item["failure_stage"] = stage
                item["failure_reason"] = reason
                diagnostics.append(item)
                expired.append(track_key)

            if key in detected_keys:
                diagnostic["outcome"] = "reanchored_by_decoder"
                diagnostics.append(diagnostic)
                continue
            if misses >= MAX_OPTICAL_FLOW_MISSES or age_seconds > MAX_OPTICAL_FLOW_AGE_SECONDS:
                reject("lifetime", "tracking_window_expired")
                continue
            previous_corners = np.asarray(track["corners"], dtype=np.float32).reshape(4, 2)
            feature_points = np.asarray(
                track.get("feature_points", previous_corners),
                dtype=np.float32,
            ).reshape(-1, 2)
            diagnostic["input_feature_count"] = int(feature_points.shape[0])
            if feature_points.shape[0] < MIN_OPTICAL_FLOW_FEATURES:
                reject("feature_selection", "insufficient_input_features")
                continue
            points = feature_points.reshape(-1, 1, 2)
            seed, seed_score = self._translation_seed(
                self._previous_gray,
                gray,
                previous_corners,
            )
            diagnostic["template_seed_px"] = [float(seed[0]), float(seed[1])]
            diagnostic["template_seed_score"] = seed_score
            initial_points = points + seed.reshape(1, 1, 2)
            current_points, status, _error = cv2.calcOpticalFlowPyrLK(
                self._previous_gray,
                gray,
                points,
                initial_points,
                winSize=(51, 51),
                maxLevel=5,
                criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 40, 0.01),
                minEigThreshold=1e-4,
                flags=cv2.OPTFLOW_USE_INITIAL_FLOW,
            )
            if current_points is None or status is None:
                reject("forward_lk", "forward_flow_unavailable")
                continue
            diagnostic["forward_valid_count"] = int(np.count_nonzero(status.reshape(-1) == 1))
            back_points, back_status, _back_error = cv2.calcOpticalFlowPyrLK(
                gray,
                self._previous_gray,
                current_points,
                points.copy(),
                winSize=(51, 51),
                maxLevel=5,
                criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 40, 0.01),
                minEigThreshold=1e-4,
                flags=cv2.OPTFLOW_USE_INITIAL_FLOW,
            )
            if back_points is None or back_status is None:
                reject("backward_lk", "backward_flow_unavailable")
                continue
            diagnostic["backward_valid_count"] = int(np.count_nonzero(back_status.reshape(-1) == 1))
            current_flat = current_points.reshape(-1, 2)
            back_flat = back_points.reshape(-1, 2)
            round_trip = np.linalg.norm(back_flat - feature_points, axis=1)
            valid = (
                (status.reshape(-1) == 1)
                & (back_status.reshape(-1) == 1)
                & np.all(np.isfinite(current_flat), axis=1)
                & np.all(np.isfinite(back_flat), axis=1)
                & (round_trip <= MAX_OPTICAL_FLOW_FB_ERROR_PX)
            )
            previous_good = feature_points[valid]
            current_good = current_flat[valid]
            finite_round_trip = round_trip[np.isfinite(round_trip)]
            diagnostic["forward_backward_valid_count"] = int(previous_good.shape[0])
            diagnostic["forward_backward_error_px"] = {
                "median": float(np.median(finite_round_trip)) if finite_round_trip.size else None,
                "max": float(np.max(finite_round_trip)) if finite_round_trip.size else None,
                "limit": MAX_OPTICAL_FLOW_FB_ERROR_PX,
            }
            if previous_good.shape[0] < MIN_OPTICAL_FLOW_FEATURES:
                reject("forward_backward_check", "insufficient_consistent_features")
                continue
            homography, inlier_mask = cv2.findHomography(
                previous_good,
                current_good,
                cv2.RANSAC,
                HOMOGRAPHY_RANSAC_THRESHOLD_PX,
            )
            if homography is None or inlier_mask is None or not np.all(np.isfinite(homography)):
                reject("homography", "homography_unavailable")
                continue
            inliers = inlier_mask.reshape(-1) == 1
            inlier_count = int(np.count_nonzero(inliers))
            inlier_ratio = inlier_count / float(previous_good.shape[0])
            diagnostic["homography_inlier_count"] = inlier_count
            diagnostic["homography_inlier_ratio"] = float(inlier_ratio)
            if inlier_count < MIN_HOMOGRAPHY_INLIERS or inlier_ratio < MIN_HOMOGRAPHY_INLIER_RATIO:
                reject("homography", "insufficient_homography_inliers")
                continue
            current_corners = cv2.perspectiveTransform(
                previous_corners.reshape(1, 4, 2),
                homography,
            ).reshape(4, 2).astype(float)
            # The planar model provides a robust coarse quadrilateral. Snap
            # each vertex back to the strong black/white tag corner when a
            # complete refinement window is available.
            if (
                np.all(current_corners[:, 0] >= 8)
                and np.all(current_corners[:, 0] < gray.shape[1] - 8)
                and np.all(current_corners[:, 1] >= 8)
                and np.all(current_corners[:, 1] < gray.shape[0] - 8)
            ):
                refined_corners = current_corners.astype(np.float32).reshape(-1, 1, 2)
                cv2.cornerSubPix(
                    gray,
                    refined_corners,
                    (7, 7),
                    (-1, -1),
                    (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 40, 0.01),
                )
                if np.all(np.isfinite(refined_corners)):
                    current_corners = refined_corners.reshape(4, 2).astype(float)
            previous_area = self._quad_area(previous_corners)
            current_area = self._quad_area(current_corners)
            center_displacement = current_corners.mean(axis=0) - previous_corners.mean(axis=0)
            diagnostic["center_displacement_px"] = [
                float(center_displacement[0]),
                float(center_displacement[1]),
            ]
            diagnostic["area_ratio"] = float(current_area / previous_area) if previous_area > 0 else None
            if not self._valid_flow_geometry(
                previous_corners,
                current_corners,
                width=int(gray.shape[1]),
                height=int(gray.shape[0]),
            ):
                reject("geometry", "tracked_quadrilateral_invalid")
                continue
            record = deepcopy(track["record"])
            minimum = current_corners.min(axis=0)
            maximum = current_corners.max(axis=0)
            center = current_corners.mean(axis=0)
            record["center_px"] = [float(value) for value in center]
            record["corners_px"] = [[float(value) for value in point] for point in current_corners]
            record["bbox_px"] = {
                "x": float(minimum[0]),
                "y": float(minimum[1]),
                "width": float(maximum[0] - minimum[0]),
                "height": float(maximum[1] - minimum[1]),
            }
            record["pose_camera_tag"] = self._propagate_pose(
                record,
                previous_corners,
                current_corners,
                camera_params,
            )
            record["tracking_state"] = "optical_flow"
            record["tracking_age_frames"] = misses + 1
            record["tracking_feature_count"] = int(previous_good.shape[0])
            record["tracking_inlier_count"] = inlier_count
            record["tracking_inlier_ratio"] = float(inlier_ratio)
            record["tracking_seed_score"] = seed_score
            track["record"] = record
            track["corners"] = current_corners
            refreshed_features = self._feature_points(gray, current_corners)
            if refreshed_features.shape[0] < MIN_OPTICAL_FLOW_FEATURES:
                refreshed_features = current_good[inliers].astype(np.float32)
            track["feature_points"] = refreshed_features
            track["misses"] = misses + 1
            recovered.append(record)
            diagnostic["outcome"] = "tracked"
            diagnostic["output_feature_count"] = int(refreshed_features.shape[0])
            diagnostics.append(diagnostic)
        for key in expired:
            self._tracks.pop(key, None)
        return recovered, diagnostics

    def detect(
        self,
        frame: bytes,
        *,
        family: str,
        tag_size_m: Any,
        fx: Any,
        fy: Any,
        cx: Any,
        cy: Any,
        distortion_coefficients: Any = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        gray = self._decode_frame(frame)
        decoded_at = time.perf_counter()
        size = _finite_float(tag_size_m, "tag_size_m", positive=True)
        camera_params = (
            _finite_float(fx, "camera_fx", positive=True),
            _finite_float(fy, "camera_fy", positive=True),
            _finite_float(cx, "camera_cx"),
            _finite_float(cy, "camera_cy"),
        )
        distortion = _distortion_coefficients(distortion_coefficients)
        if distortion:
            if cv2 is None:
                raise RuntimeError("opencv_required_for_camera_distortion_correction")
            matrix = np.asarray([
                [camera_params[0], 0.0, camera_params[2]],
                [0.0, camera_params[1], camera_params[3]],
                [0.0, 0.0, 1.0],
            ], dtype=np.float64)
            gray = cv2.undistort(gray, matrix, np.asarray(distortion, dtype=np.float64), None, matrix)
        validated_at = time.perf_counter()
        families = [require_supported_apriltag_family(family)]
        records: list[dict[str, Any]] = []
        tracking_diagnostics: list[dict[str, Any]] = []
        decoder_rejections: list[dict[str, Any]] = []
        direct_detection_count = 0
        enhanced_detection_count = 0
        timings_ms: dict[str, float] = {
            "frame_decode": round((decoded_at - started) * 1000.0, 3),
            "input_validation": round((validated_at - decoded_at) * 1000.0, 3),
            "direct_decode": 0.0,
            "optical_flow": 0.0,
            "enhanced_decode": 0.0,
        }
        # pupil-apriltags Detector instances are not documented as reentrant.
        # Serialize requests and retain only the currently selected family.
        lock_requested_at = time.perf_counter()
        with self._lock:
            timings_ms["lock_wait"] = round((time.perf_counter() - lock_requested_at) * 1000.0, 3)
            self._frame_sequence += 1
            frame_sequence = self._frame_sequence
            direct_started = time.perf_counter()
            for selected_family in families:
                detector = self._detector(selected_family)
                tracking_signature = (
                    selected_family,
                    size,
                    *camera_params,
                    *distortion,
                    int(gray.shape[1]),
                    int(gray.shape[0]),
                )
                if self._tracking_signature != tracking_signature:
                    self._reset_tracking()
                    self._tracking_signature = tracking_signature
                detections = detector.detect(
                    gray,
                    estimate_tag_pose=True,
                    camera_params=camera_params,
                    tag_size=size,
                )
                accepted, rejected = self._records_from_detections(
                    detections,
                    selected_family,
                    source="direct",
                )
                records.extend(accepted)
                decoder_rejections.extend(rejected)
            timings_ms["direct_decode"] = round((time.perf_counter() - direct_started) * 1000.0, 3)
            direct_detection_count = len(records)
            track_count_before = len(self._tracks)
            now = time.monotonic()
            detected_keys = {f'{record["family"]}:{record["tag_id"]}' for record in records}
            # Try the inexpensive planar tracker before a second full decode.
            # This shortens the frame interval precisely when motion blur makes
            # the decoder miss, improving recovery during hand-held movement.
            flow_started = time.perf_counter()
            flow_records, tracking_diagnostics = self._optical_flow_records(
                gray,
                detected_keys,
                camera_params,
                now,
            )
            timings_ms["optical_flow"] = round((time.perf_counter() - flow_started) * 1000.0, 3)
            records.extend(flow_records)
            flow_keys = {
                f'{record["family"]}:{record["tag_id"]}'
                for record in flow_records
            }
            # In a multi-tag scene, one stable tag used to suppress the
            # enhanced decoder for the whole frame.  A tag returning to view
            # could therefore alternate between a confirmed decode and an
            # optical-flow-only estimate.  The Viewer intentionally does not
            # publish optical-flow-only poses, so that alternation eventually
            # exceeded its freshness timeout.  Retry the enhanced decoder for
            # flow-only tags on the first miss and then every third miss.
            # This keeps the fallback bounded while allowing each tag to
            # recover independently of the other tags in the frame.
            flow_recovery_due = any(
                int(record.get("tracking_age_frames", 0)) == 1
                or int(record.get("tracking_age_frames", 0)) % 3 == 0
                for record in flow_records
            )
            if not records or flow_recovery_due:
                enhanced_started = time.perf_counter()
                enhanced_records: list[dict[str, Any]] = []
                for selected_family in families:
                    detector = self._detector(selected_family)
                    enhanced = self._motion_enhanced_frame(gray)
                    enhanced_detections = detector.detect(
                        enhanced,
                        estimate_tag_pose=True,
                        camera_params=camera_params,
                        tag_size=size,
                    )
                    accepted, rejected = self._records_from_detections(
                        enhanced_detections,
                        selected_family,
                        source="enhanced",
                    )
                    if flow_keys:
                        accepted = [
                            record for record in accepted
                            if f'{record["family"]}:{record["tag_id"]}' in flow_keys
                        ]
                    enhanced_records.extend(accepted)
                    decoder_rejections.extend(rejected)
                enhanced_keys = {
                    f'{record["family"]}:{record["tag_id"]}'
                    for record in enhanced_records
                }
                if enhanced_keys:
                    records = [
                        record for record in records
                        if f'{record["family"]}:{record["tag_id"]}' not in enhanced_keys
                    ]
                    records.extend(enhanced_records)
                timings_ms["enhanced_decode"] = round((time.perf_counter() - enhanced_started) * 1000.0, 3)
                enhanced_detection_count = len(enhanced_records)
            for record in records:
                if record.get("tracking_state") == "optical_flow":
                    continue
                key = f'{record["family"]}:{record["tag_id"]}'
                corners = np.asarray(record["corners_px"], dtype=np.float32)
                self._tracks[key] = {
                    "record": deepcopy(record),
                    "corners": corners,
                    "feature_points": self._feature_points(gray, corners),
                    "misses": 0,
                    "last_decoded_at": now,
                }
            self._previous_gray = gray.copy()
            track_count_after = len(self._tracks)
        records.sort(key=lambda item: (str(item["family"]), int(item["tag_id"]), -float(item["decision_margin"])))
        output_records = [_distort_record_pixels(record, camera_params, distortion) for record in records]
        tracked_count = sum(record.get("tracking_state") == "optical_flow" for record in records)
        processing_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return {
            "status": "verified",
            "frame": {"width": int(gray.shape[1]), "height": int(gray.shape[0])},
            "camera_model": {
                "frame": "camera_optical",
                "axis": {"x": "right", "y": "down", "z": "forward"},
                "fx": camera_params[0],
                "fy": camera_params[1],
                "cx": camera_params[2],
                "cy": camera_params[3],
                "tag_size_m": size,
                "distortion_model": "opencv_plumb_bob" if distortion else "none",
                "distortion_coefficients": list(distortion),
            },
            "requested_family": family,
            "scanned_families": families,
            "detection_count": len(output_records),
            "detector_detection_count": len(records) - tracked_count,
            "tracked_detection_count": tracked_count,
            "detections": output_records,
            "processing_ms": processing_ms,
            "diagnostics": {
                "schema_version": "apriltag_live_frame_diagnostics_v1",
                "frame_sequence": frame_sequence,
                "timings_ms": timings_ms,
                "direct_detection_count": direct_detection_count,
                "enhanced_detection_count": enhanced_detection_count,
                "optical_flow_detection_count": tracked_count,
                "track_count_before": track_count_before,
                "track_count_after": track_count_after,
                "decoder_rejections": decoder_rejections,
                "tracking": tracking_diagnostics,
                "thresholds": {
                    "max_tracking_frames": MAX_OPTICAL_FLOW_MISSES,
                    "max_tracking_age_ms": int(MAX_OPTICAL_FLOW_AGE_SECONDS * 1000),
                    "max_forward_backward_error_px": MAX_OPTICAL_FLOW_FB_ERROR_PX,
                    "min_features": MIN_OPTICAL_FLOW_FEATURES,
                    "min_homography_inliers": MIN_HOMOGRAPHY_INLIERS,
                    "min_homography_inlier_ratio": MIN_HOMOGRAPHY_INLIER_RATIO,
                    "min_template_seed_score": MIN_TEMPLATE_SEED_SCORE,
                    "max_detection_hamming": MAX_DETECTION_HAMMING,
                    "min_detection_decision_margin": MIN_DETECTION_DECISION_MARGIN,
                },
            },
            "persisted": False,
        }
