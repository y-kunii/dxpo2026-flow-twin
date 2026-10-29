#!/usr/bin/env python3
"""Validate semantic person masks and inverse COLMAP keep masks."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageOps

ROOT = Path(__file__).resolve().parents[2]
if not __package__ and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if __package__:
    from .generate_person_masks import image_files, mask_path
    from .scene_dataset import sha256_directory, sha256_file
else:
    from generate_person_masks import image_files, mask_path
    from scene_dataset import sha256_directory, sha256_file


def validate(
    image_dir: Path,
    person_mask_dir: Path,
    colmap_mask_dir: Path,
    report_path: Path,
) -> dict[str, Any]:
    failures: list[str] = []
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {"status": "failed", "failure_reasons": [f"mask_report_invalid:{error}"]}
    files = image_files(image_dir)
    person_files = [mask_path(person_mask_dir, image_dir, path) for path in files]
    colmap_files = [mask_path(colmap_mask_dir, image_dir, path) for path in files]
    missing_person = [path for path in person_files if not path.is_file()]
    missing_colmap = [path for path in colmap_files if not path.is_file()]
    if missing_person:
        failures.append(f"person_masks_missing:{len(missing_person)}")
    if missing_colmap:
        failures.append(f"colmap_masks_missing:{len(missing_colmap)}")

    positive_count = 0
    total_person_pixels = 0
    total_pixels = 0
    for image_path, person_path, colmap_path in zip(
        files, person_files, colmap_files, strict=True
    ):
        if not person_path.is_file() or not colmap_path.is_file():
            continue
        with Image.open(image_path) as source:
            source_size = source.size
        with Image.open(person_path) as opened:
            person = opened.convert("L")
        with Image.open(colmap_path) as opened:
            colmap = opened.convert("L")
        if person.size != source_size or colmap.size != source_size:
            failures.append(f"mask_dimensions_mismatch:{image_path.relative_to(image_dir).as_posix()}")
            continue
        histogram = person.histogram()
        if sum(histogram[1:255]) != 0:
            failures.append(f"person_mask_not_binary:{person_path.relative_to(person_mask_dir).as_posix()}")
            continue
        if ImageChops.difference(colmap, ImageOps.invert(person)).getbbox() is not None:
            failures.append(f"colmap_mask_not_inverse:{colmap_path.relative_to(colmap_mask_dir).as_posix()}")
            continue
        person_pixels = histogram[255]
        positive_count += int(person_pixels > 0)
        total_person_pixels += person_pixels
        total_pixels += person.width * person.height
        if len(failures) >= 20:
            break

    expected = len(files)
    if report.get("status") != "verified":
        failures.append("mask_generation_report_not_verified")
    if report.get("input_image_count") != expected:
        failures.append("mask_generation_input_count_mismatch")
    if report.get("person_mask_count") != expected or report.get("colmap_mask_count") != expected:
        failures.append("mask_generation_output_count_mismatch")
    candidate_image_count = report.get("candidate_image_count")
    rejected_masks = report.get("rejected_masks")
    rejected_coverage_count = (
        rejected_masks.get("coverage_count") if isinstance(rejected_masks, dict) else None
    )
    discarded_masks = report.get("discarded_masks")
    discarded_large_count = (
        discarded_masks.get("low_confidence_large_count")
        if isinstance(discarded_masks, dict)
        else 0
    )
    if (
        not isinstance(candidate_image_count, int)
        or not isinstance(rejected_coverage_count, int)
        or not isinstance(discarded_large_count, int)
    ):
        failures.append("mask_generation_candidate_counts_invalid")
    elif candidate_image_count - rejected_coverage_count - discarded_large_count != positive_count:
        failures.append("mask_generation_positive_count_mismatch")
    model = report.get("model")
    if report.get("backend") == "grounding_dino_sam2":
        configuration = report.get("configuration")
        if not isinstance(configuration, dict):
            failures.append("mask_generation_configuration_missing")
        else:
            configured_model = configuration.get("model")
            if configuration.get("backend") != "grounding_dino_sam2":
                failures.append("mask_generation_configuration_backend_mismatch")
            quality_gate = configuration.get("quality_gate")
            if (
                not isinstance(configured_model, dict)
                or not isinstance(model, dict)
                or any(
                    configured_model.get(field) != model.get(field)
                    for field in (
                        "detector", "detector_revision", "segmenter",
                        "segmenter_revision", "runtime_package",
                    )
                )
                or not isinstance(configuration.get("box_threshold"), (int, float))
                or not isinstance(configuration.get("text_threshold"), (int, float))
                or not isinstance(quality_gate, dict)
                or not isinstance(
                    quality_gate.get("maximum_person_coverage_ratio"), (int, float)
                )
            ):
                failures.append("mask_generation_configuration_quality_gate_invalid")
        if not isinstance(model, dict):
            failures.append("mask_generation_model_missing")
        else:
            for field in ("detector_revision", "segmenter_revision"):
                revision = model.get(field)
                if not isinstance(revision, str) or len(revision) != 40:
                    failures.append(f"mask_generation_{field}_invalid")
    return {
        "schema_version": "1.0.0",
        "stage": "person_mask_set_validation",
        "status": "verified" if not failures else "failed",
        "input_image_count": expected,
        "person_mask_count": expected - len(missing_person),
        "colmap_mask_count": expected - len(missing_colmap),
        "positive_mask_count": positive_count,
        "person_pixel_ratio": total_person_pixels / total_pixels if total_pixels else 0.0,
        "person_masks_sha256": sha256_directory(person_mask_dir),
        "colmap_masks_sha256": sha256_directory(colmap_mask_dir),
        "generation_report_sha256": sha256_file(report_path),
        "failure_reasons": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-dir", type=Path, required=True)
    parser.add_argument("--person-mask-dir", type=Path, required=True)
    parser.add_argument("--colmap-mask-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = validate(args.image_dir, args.person_mask_dir, args.colmap_mask_dir, args.report)
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["status"] == "verified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
