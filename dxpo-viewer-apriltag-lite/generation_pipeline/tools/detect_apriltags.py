from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

if __package__:
    from .apriltag_family_contract import (
        DEFAULT_APRILTAG_FAMILY,
        SUPPORTED_APRILTAG_FAMILIES,
    )
else:
    from apriltag_family_contract import (
        DEFAULT_APRILTAG_FAMILY,
        SUPPORTED_APRILTAG_FAMILIES,
    )


SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg"}


def list_images(image_root: Path) -> list[Path]:
    images = [
        path
        for path in image_root.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    ]
    return sorted(images)


def canonical_corners_from_homography(
    detection: Any,
) -> list[list[float]]:
    homography = np.asarray(detection.homography, dtype=float)

    ideal_corners = np.array(
        [
            [-1.0, 1.0, 1.0],
            [1.0, 1.0, 1.0],
            [1.0, -1.0, 1.0],
            [-1.0, -1.0, 1.0],
        ],
        dtype=float,
    )

    projected = (homography @ ideal_corners.T).T

    if np.any(np.abs(projected[:, 2]) < 1e-12):
        raise RuntimeError("Invalid AprilTag homography projection.")

    projected_xy = projected[:, :2] / projected[:, 2:3]

    return [
        [float(x), float(y)]
        for x, y in projected_xy
    ]


def to_jsonable_detection(detection: Any) -> dict[str, Any]:
    tag_family = detection.tag_family

    if isinstance(tag_family, bytes):
        tag_family = tag_family.decode("utf-8")

    homography = np.asarray(detection.homography, dtype=float)

    return {
        "family": str(tag_family),
        "id": int(detection.tag_id),
        "hamming": int(detection.hamming),
        "decision_margin": float(detection.decision_margin),
        "center_px": [
            float(detection.center[0]),
            float(detection.center[1]),
        ],
        "corners_px": canonical_corners_from_homography(detection),
        "raw_corners_px": [
            [float(x), float(y)]
            for x, y in detection.corners
        ],
        "homography": homography.tolist(),
    }


def detect_image(detector: Any, image_path: Path) -> list[dict[str, Any]]:
    gray = np.asarray(Image.open(image_path).convert("L"))

    if gray.dtype != np.uint8:
        gray = gray.astype(np.uint8)

    detections = detector.detect(
        gray,
        estimate_tag_pose=False,
        camera_params=None,
        tag_size=None,
    )

    return [to_jsonable_detection(d) for d in detections]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image_root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--tag_family",
        choices=sorted(SUPPORTED_APRILTAG_FAMILIES),
        default=DEFAULT_APRILTAG_FAMILY,
    )
    parser.add_argument("--quad_decimate", type=float, default=1.0)
    parser.add_argument("--quad_sigma", type=float, default=0.0)
    parser.add_argument("--decode_sharpening", type=float, default=0.25)
    args = parser.parse_args()

    try:
        from pupil_apriltags import Detector
    except ImportError as exc:
        raise RuntimeError(
            "pupil-apriltags is not installed. "
            "Install it in the generation environment before running detection."
        ) from exc

    if not args.image_root.exists():
        raise RuntimeError(f"image_root does not exist: {args.image_root}")

    detector = Detector(
        families=args.tag_family,
        nthreads=1,
        quad_decimate=args.quad_decimate,
        quad_sigma=args.quad_sigma,
        refine_edges=1,
        decode_sharpening=args.decode_sharpening,
        debug=0,
    )

    images = list_images(args.image_root)

    records = []
    total_detections = 0

    for image_path in images:
        detections = detect_image(detector, image_path)
        total_detections += len(detections)

        records.append(
            {
                "image_name": image_path.relative_to(args.image_root).as_posix(),
                "detections": detections,
            }
        )

    output_data = {
        "schema_version": "1.0.0",
        "image_root": str(args.image_root),
        "tag_family": args.tag_family,
        "image_count": len(images),
        "detection_count": total_detections,
        "images": records,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"Images scanned: {len(images)}")
    print(f"Detections: {total_detections}")
    print(f"Output: {args.output}")


if __name__ == "__main__":
    main()
