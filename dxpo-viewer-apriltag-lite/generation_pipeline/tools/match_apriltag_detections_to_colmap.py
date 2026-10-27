from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

if __package__:
    from .colmap_text_model import load_colmap_text_model
else:
    from colmap_text_model import load_colmap_text_model


def load_detections(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(f"detections.json not found: {path}")

    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--detections", type=Path, required=True)
    parser.add_argument("--colmap_model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    detection_data = load_detections(args.detections)
    cameras, images = load_colmap_text_model(args.colmap_model)

    records = []
    matched_images = 0
    unmatched_images = 0
    matched_detections = 0
    unmatched_detections = 0

    for image_record in detection_data.get("images", []):
        image_name = image_record["image_name"]
        detections = image_record.get("detections", [])

        colmap_image = images.get(image_name)

        if colmap_image is None:
            unmatched_images += 1
            unmatched_detections += len(detections)
            records.append(
                {
                    "image_name": image_name,
                    "matched": False,
                    "detection_count": len(detections),
                    "reason": "image_name_not_found_in_colmap",
                }
            )
            continue

        camera = cameras[colmap_image.camera_id]
        matched_images += 1
        matched_detections += len(detections)

        records.append(
            {
                "image_name": image_name,
                "matched": True,
                "detection_count": len(detections),
                "colmap_image_id": colmap_image.image_id,
                "camera_id": colmap_image.camera_id,
                "camera_model": camera.model,
                "camera_width": camera.width,
                "camera_height": camera.height,
            }
        )

    output_data = {
        "schema_version": "1.0.0",
        "detections": str(args.detections),
        "colmap_model": str(args.colmap_model),
        "colmap_image_count": len(images),
        "detection_image_count": len(detection_data.get("images", [])),
        "matched_images": matched_images,
        "unmatched_images": unmatched_images,
        "matched_detections": matched_detections,
        "unmatched_detections": unmatched_detections,
        "records": records,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"COLMAP images: {len(images)}")
    print(f"Detection images: {len(detection_data.get('images', []))}")
    print(f"Matched images: {matched_images}")
    print(f"Unmatched images: {unmatched_images}")
    print(f"Matched detections: {matched_detections}")
    print(f"Unmatched detections: {unmatched_detections}")
    print(f"Output: {args.output}")


if __name__ == "__main__":
    main()
