from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from statistics import median
from typing import Any

from PIL import Image


IMAGE_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".png", ".webp"}


class PersonMaskError(RuntimeError):
    """Raised when a required person-mask stage cannot produce a valid mask set."""


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
        path.chmod(0o644)
    finally:
        temporary.unlink(missing_ok=True)


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PersonMaskError(f"cannot_read_person_mask_config:{error}") from error
    if not isinstance(config, dict) or config.get("schema_version") != "1.0.0":
        raise PersonMaskError("unsupported_person_mask_config")
    backend = config.get("backend")
    if backend not in {"grounding_dino_sam2", "mediapipe_image_segmenter"}:
        raise PersonMaskError("unsupported_person_mask_backend")
    model = config.get("model")
    if not isinstance(model, dict) or model.get("license") != "Apache-2.0":
        raise PersonMaskError("person_mask_model_license_not_approved")
    required_model_fields = (
        {
            "detector", "detector_revision", "segmenter", "segmenter_revision",
            "runtime_package", "runtime_version",
        }
        if backend == "grounding_dino_sam2"
        else {
            "name",
            "version",
            "runtime_package",
            "runtime_version",
            "model_asset_path",
            "model_asset_url",
            "model_asset_sha256",
            "person_confidence_mask_index",
        }
    )
    if any(field not in model or model[field] in (None, "") for field in required_model_fields):
        raise PersonMaskError("person_mask_model_config_incomplete")
    if backend == "grounding_dino_sam2" and model["runtime_package"] != "transformers":
        raise PersonMaskError("person_mask_model_config_invalid")
    if backend == "grounding_dino_sam2":
        for field in ("detector_revision", "segmenter_revision"):
            revision = str(model[field])
            if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
                raise PersonMaskError(f"person_mask_{field}_invalid")
    if backend == "mediapipe_image_segmenter" and (model["runtime_package"] != "mediapipe" or int(model["person_confidence_mask_index"]) < 0):
        raise PersonMaskError("person_mask_model_config_invalid")
    if backend == "grounding_dino_sam2":
        for key in ("box_threshold", "text_threshold"):
            threshold = config.get(key)
            if not isinstance(threshold, (int, float)) or not 0 < float(threshold) < 1:
                raise PersonMaskError(f"invalid_person_mask_{key}")
        if not isinstance(config.get("prompt"), str) or not config["prompt"].strip():
            raise PersonMaskError("invalid_person_mask_prompt")
    else:
        threshold = config.get("confidence_threshold")
        if not isinstance(threshold, (int, float)) or not 0 < float(threshold) < 1:
            raise PersonMaskError("invalid_person_mask_confidence_threshold")
    gate = config.get("quality_gate")
    if not isinstance(gate, dict) or int(gate.get("minimum_image_count", 0)) < 1:
        raise PersonMaskError("invalid_person_mask_quality_gate")
    maximum_coverage = gate.get("maximum_person_coverage_ratio")
    if not isinstance(maximum_coverage, (int, float)) or not 0 < float(maximum_coverage) <= 1:
        raise PersonMaskError("invalid_person_mask_maximum_coverage")
    rejected_ratio = gate.get("maximum_rejected_mask_ratio", 0.0)
    if not isinstance(rejected_ratio, (int, float)) or not 0 <= float(rejected_ratio) <= 1:
        raise PersonMaskError("invalid_person_mask_rejected_ratio")
    if backend == "grounding_dino_sam2":
        candidate_ratio = gate.get("minimum_candidate_image_ratio", 0)
        if not isinstance(candidate_ratio, (int, float)) or not 0 <= float(candidate_ratio) <= 1:
            raise PersonMaskError("invalid_person_mask_candidate_ratio")
        large_mask_ratio = gate.get("large_mask_review_ratio", maximum_coverage)
        if (
            not isinstance(large_mask_ratio, (int, float))
            or not 0 < float(large_mask_ratio) <= float(maximum_coverage)
        ):
            raise PersonMaskError("invalid_person_mask_large_mask_review_ratio")
        large_mask_score = gate.get(
            "minimum_large_mask_score",
            gate.get("minimum_box_score", config.get("box_threshold")),
        )
        if (
            not isinstance(large_mask_score, (int, float))
            or not 0 < float(large_mask_score) < 1
        ):
            raise PersonMaskError("invalid_person_mask_large_mask_score")
    return config


def image_files(image_dir: Path) -> list[Path]:
    files = sorted(path for path in image_dir.rglob("*") if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES)
    if not files:
        raise PersonMaskError("person_mask_input_images_missing")
    keys: set[str] = set()
    duplicates: list[str] = []
    for path in files:
        key = path.relative_to(image_dir).as_posix().casefold()
        if key in keys:
            duplicates.append(key)
        keys.add(key)
    if duplicates:
        raise PersonMaskError("duplicate_image_mask_keys:" + ",".join(sorted(duplicates)))
    return files


def mask_path(mask_dir: Path, image_dir: Path, image_path: Path) -> Path:
    return mask_dir / (image_path.relative_to(image_dir).as_posix() + ".png")


def ensure_static_masks(image_dir: Path, static_mask_dir: Path, files: Iterable[Path]) -> None:
    missing = [str(path.relative_to(image_dir)) for path in files if not mask_path(static_mask_dir, image_dir, path).is_file()]
    if missing:
        examples = ",".join(missing[:10])
        raise PersonMaskError(f"static_mask_mapping_incomplete:{len(missing)}:{examples}")


def combine_masks(static_mask: Image.Image, person_mask: Image.Image) -> tuple[Image.Image, Image.Image, float]:
    """Return white=person and white=kept masks at the source image resolution.

    Supported App A inputs have valid RGB across the complete rendered frame.
    The renderer's legacy nearest-camera ownership partition is therefore not
    a semantic exclusion mask.  COLMAP/LFS excludes only detected people.
    """
    if static_mask.size != person_mask.size:
        raise PersonMaskError("person_mask_static_mask_dimension_mismatch")
    static = static_mask.convert("L")
    person = person_mask.convert("L")
    person_bytes = person.tobytes()
    person_binary = bytes(255 if value else 0 for value in person_bytes)
    kept_binary = bytes(0 if person_value else 255 for person_value in person_bytes)
    total = len(person_binary)
    coverage = sum(value == 255 for value in person_binary) / total if total else 0.0
    return Image.frombytes("L", static.size, person_binary), Image.frombytes("L", static.size, kept_binary), coverage


def box_iou(first: list[float], second: list[float]) -> float:
    first_x1, first_y1, first_x2, first_y2 = first
    second_x1, second_y1, second_x2, second_y2 = second
    intersection = max(0.0, min(first_x2, second_x2) - max(first_x1, second_x1)) * max(0.0, min(first_y2, second_y2) - max(first_y1, second_y1))
    first_area = max(0.0, first_x2 - first_x1) * max(0.0, first_y2 - first_y1)
    second_area = max(0.0, second_x2 - second_x1) * max(0.0, second_y2 - second_y1)
    union = first_area + second_area - intersection
    return intersection / union if union > 0.0 else 0.0


def non_maximum_suppression(boxes: list[list[float]], scores: list[float], threshold: float) -> list[int]:
    order = sorted(range(len(boxes)), key=lambda index: scores[index], reverse=True)
    selected: list[int] = []
    while order:
        current = order.pop(0)
        selected.append(current)
        order = [candidate for candidate in order if box_iou(boxes[current], boxes[candidate]) < threshold]
    return selected


def create_grounding_sam_segmenter(model: dict[str, Any], config: dict[str, Any]) -> tuple[Callable[[Image.Image], tuple[Image.Image, dict[str, Any]]], str, Callable[[], None]]:
    """Create the detector+segmenter used for production person masks.

    Grounding DINO supplies object boxes and SAM 2.1 Large turns those boxes
    into pixel masks.  Keeping both models in one process avoids reloading
    several gigabytes of weights for every frame.
    """
    try:
        import cv2
        import numpy as np
        import torch
        from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor, Sam2Model, Sam2Processor
    except ImportError as error:
        raise PersonMaskError("grounding_dino_sam2_dependency_missing") from error

    detector_id = str(model["detector"])
    detector_revision = str(model["detector_revision"])
    segmenter_id = str(model["segmenter"])
    segmenter_revision = str(model["segmenter_revision"])
    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.float16 if device == "cuda" else torch.float32
    try:
        detector_processor = AutoProcessor.from_pretrained(detector_id, revision=detector_revision)
        detector = AutoModelForZeroShotObjectDetection.from_pretrained(
            detector_id, revision=detector_revision
        ).to(device)
        detector.eval()
        sam_processor = Sam2Processor.from_pretrained(segmenter_id, revision=segmenter_revision)
        sam = Sam2Model.from_pretrained(
            segmenter_id, revision=segmenter_revision, torch_dtype=dtype
        ).to(device)
        sam.eval()
    except Exception as error:  # transformers raises several model-specific exception types
        raise PersonMaskError(f"grounding_dino_sam2_model_load_failed:{error}") from error

    prompt = str(config["prompt"])
    box_threshold = float(config["box_threshold"])
    text_threshold = float(config["text_threshold"])
    nms_threshold = float(config.get("nms_threshold", 0.5))
    minimum_score = float(config.get("quality_gate", {}).get("minimum_box_score", box_threshold))
    margin = int(config.get("mask_margin_px", 0))

    def segment(image: Image.Image) -> tuple[Image.Image, dict[str, Any]]:
        rgb = image.convert("RGB")
        width, height = rgb.size
        detector_inputs = detector_processor(images=rgb, text=prompt, return_tensors="pt").to(device)
        with torch.inference_mode():
            detector_outputs = detector(**detector_inputs)
        detections = detector_processor.post_process_grounded_object_detection(
            detector_outputs,
            detector_inputs.input_ids,
            threshold=box_threshold,
            text_threshold=text_threshold,
            target_sizes=[(height, width)],
        )[0]
        raw_boxes = [[float(value) for value in box.tolist()] for box in detections["boxes"]]
        raw_scores = [float(score.item()) for score in detections["scores"]]
        selected = [index for index in non_maximum_suppression(raw_boxes, raw_scores, nms_threshold) if raw_scores[index] >= minimum_score]
        boxes = [raw_boxes[index] for index in selected]
        scores = [raw_scores[index] for index in selected]
        person_mask = np.zeros((height, width), dtype=np.uint8)
        if boxes:
            sam_inputs = sam_processor(images=rgb, input_boxes=[boxes], return_tensors="pt").to(device)
            with torch.inference_mode():
                if device == "cuda":
                    with torch.autocast(device_type="cuda", dtype=torch.float16):
                        sam_outputs = sam(**sam_inputs, multimask_output=False)
                else:
                    sam_outputs = sam(**sam_inputs, multimask_output=False)
            masks = sam_processor.post_process_masks(sam_outputs.pred_masks.float().cpu(), sam_inputs["original_sizes"].cpu())[0]
            masks = masks > 0
            masks = masks.reshape(-1, height, width)
            person_mask = masks.any(dim=0).numpy().astype(np.uint8) * 255
            if margin > 0:
                kernel_size = margin * 2 + 1
                kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
                person_mask = cv2.dilate(person_mask, kernel, iterations=1)
        metadata = {
            "candidate_count": len(boxes),
            "candidate_scores": scores,
            "candidate_boxes_xyxy": boxes,
            "device": device,
        }
        return Image.fromarray(person_mask, mode="L"), metadata

    def close() -> None:
        nonlocal detector, sam, detector_processor, sam_processor
        del detector, sam, detector_processor, sam_processor
        if device == "cuda":
            torch.cuda.empty_cache()

    return segment, f"{detector_id}+{segmenter_id}:{getattr(torch, '__version__', 'unknown')}", close


def create_segmenter(model: dict[str, Any], model_asset: Path, config: dict[str, Any]) -> tuple[Callable[[Image.Image], Any], str, Callable[[], None]]:
    if config.get("backend") == "grounding_dino_sam2":
        return create_grounding_sam_segmenter(model, config)
    try:
        import mediapipe as mp
        import numpy as np
    except ImportError as error:
        raise PersonMaskError("mediapipe_person_mask_dependency_missing") from error
    if not model_asset.is_file():
        raise PersonMaskError(f"person_mask_model_asset_missing:{model_asset}")
    actual_hash = hashlib.sha256(model_asset.read_bytes()).hexdigest()
    if actual_hash != str(model["model_asset_sha256"]):
        raise PersonMaskError("person_mask_model_asset_hash_mismatch")
    try:
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision
        options = vision.ImageSegmenterOptions(
            base_options=mp_python.BaseOptions(model_asset_path=str(model_asset)),
            output_category_mask=False,
            output_confidence_masks=True,
            running_mode=vision.RunningMode.IMAGE,
        )
        segmenter = vision.ImageSegmenter.create_from_options(options)
    except (AttributeError, ImportError, OSError, RuntimeError, ValueError) as error:
        raise PersonMaskError(f"mediapipe_image_segmenter_init_failed:{error}") from error

    def segment(image: Image.Image) -> Image.Image:
        rgb = np.asarray(image.convert("RGB"))
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        try:
            result = segmenter.segment(mp_image)
        except (OSError, RuntimeError, ValueError) as error:
            raise PersonMaskError(f"mediapipe_person_mask_inference_failed:{error}") from error
        person_index = int(model["person_confidence_mask_index"])
        if not result.confidence_masks or len(result.confidence_masks) <= person_index:
            raise PersonMaskError("mediapipe_person_mask_missing_person_confidence")
        values = np.asarray(result.confidence_masks[person_index].numpy_view(), dtype="float32").squeeze()
        if values.ndim != 2:
            raise PersonMaskError("mediapipe_person_mask_invalid_shape")
        if not np.isfinite(values).all():
            raise PersonMaskError("mediapipe_person_mask_non_finite_output")
        return Image.fromarray(values, mode="F")

    return segment, str(getattr(mp, "__version__", "unknown")), segmenter.close


def threshold_mask(probabilities: Image.Image, threshold: float, size: tuple[int, int]) -> Image.Image:
    resized = probabilities.resize(size, Image.Resampling.BILINEAR)
    if resized.mode != "F":
        resized = resized.convert("F")
    values = (value[0] for value in struct.iter_unpack("f", resized.tobytes()))
    return Image.frombytes("L", size, bytes(255 if value >= threshold else 0 for value in values))


def generate_person_masks(
    image_dir: Path,
    static_mask_dir: Path,
    person_mask_dir: Path,
    colmap_mask_dir: Path,
    config: dict[str, Any],
    segment: Callable[[Image.Image], Any],
) -> dict[str, Any]:
    files = image_files(image_dir)
    ensure_static_masks(image_dir, static_mask_dir, files)
    backend = str(config.get("backend", "mediapipe_image_segmenter"))
    threshold = float(config.get("confidence_threshold", 0.5))
    max_coverage = float(config["quality_gate"]["maximum_person_coverage_ratio"])
    max_rejected_ratio = float(config["quality_gate"].get("maximum_rejected_mask_ratio", 0.0))
    large_mask_review_ratio = float(
        config["quality_gate"].get("large_mask_review_ratio", max_coverage)
    )
    minimum_large_mask_score = float(
        config["quality_gate"].get(
            "minimum_large_mask_score",
            config["quality_gate"].get(
                "minimum_box_score", config.get("box_threshold", 0.0)
            ),
        )
    )
    coverage_values: list[float] = []
    raw_coverage_values: list[float] = []
    rejected_coverage_count = 0
    rejected_examples: list[str] = []
    discarded_low_confidence_large_count = 0
    discarded_low_confidence_large_examples: list[str] = []
    candidate_image_count = 0
    candidate_box_count = 0
    candidate_scores: list[float] = []
    records: list[dict[str, Any]] = []
    person_mask_dir.mkdir(parents=True, exist_ok=True)
    colmap_mask_dir.mkdir(parents=True, exist_ok=True)
    for image_path in files:
        with Image.open(image_path) as opened:
            image = opened.convert("RGB")
        with Image.open(mask_path(static_mask_dir, image_dir, image_path)) as opened:
            static = opened.convert("L")
        segmentation = segment(image)
        metadata: dict[str, Any] = {}
        if isinstance(segmentation, tuple):
            binary, metadata = segmentation
        else:
            binary = threshold_mask(segmentation, threshold, image.size)
        if binary.size != image.size:
            raise PersonMaskError("person_mask_output_dimension_mismatch")
        candidate_count = int(metadata.get("candidate_count", 0))
        scores = [float(value) for value in metadata.get("candidate_scores", [])]
        candidate_image_count += int(candidate_count > 0)
        candidate_box_count += candidate_count
        candidate_scores.extend(scores)
        person, colmap, raw_coverage = combine_masks(static, binary)
        maximum_score = max(scores) if scores else 0.0
        discarded_low_confidence_large = (
            backend == "grounding_dino_sam2"
            and raw_coverage > large_mask_review_ratio
            and maximum_score < minimum_large_mask_score
        )
        rejected = raw_coverage > max_coverage and not discarded_low_confidence_large
        if discarded_low_confidence_large:
            # Large masks near the detector threshold are a distinct failure
            # mode from a real close-up person. Keep the scene pixels, record
            # the discarded prediction, and reserve the hard coverage gate
            # for confident masks that are effectively full-frame.
            person = Image.new("L", static.size, 0)
            colmap = Image.new("L", static.size, 255)
            coverage = 0.0
            discarded_low_confidence_large_count += 1
            if len(discarded_low_confidence_large_examples) < 10:
                discarded_low_confidence_large_examples.append(
                    image_path.relative_to(image_dir).as_posix()
                )
        elif rejected:
            # A near-full-frame "person" mask is not usable for SfM.  It is
            # normally a detector/SAM false positive around the box threshold.
            # Treat the rejected prediction as no person for this frame and
            # keep the run fail-closed if such rejects become systematic.
            person = Image.new("L", static.size, 0)
            colmap = Image.new("L", static.size, 255)
            coverage = 0.0
            rejected_coverage_count += 1
            if len(rejected_examples) < 10:
                rejected_examples.append(image_path.relative_to(image_dir).as_posix())
        else:
            coverage = raw_coverage
        person_output = mask_path(person_mask_dir, image_dir, image_path)
        colmap_output = mask_path(colmap_mask_dir, image_dir, image_path)
        person_output.parent.mkdir(parents=True, exist_ok=True)
        colmap_output.parent.mkdir(parents=True, exist_ok=True)
        person.save(person_output)
        colmap.save(colmap_output)
        coverage_values.append(coverage)
        raw_coverage_values.append(raw_coverage)
        records.append({
            "relative_image": image_path.relative_to(image_dir).as_posix(),
            "candidate_count": candidate_count,
            "candidate_scores": scores,
            "person_coverage_ratio": coverage,
            "raw_person_coverage_ratio": raw_coverage,
            "low_confidence_large_mask_discarded": discarded_low_confidence_large,
            "coverage_rejected": rejected,
        })
    failures: list[str] = []
    if len(files) < int(config["quality_gate"]["minimum_image_count"]):
        failures.append("insufficient_person_mask_images")
    rejected_ratio = rejected_coverage_count / len(files) if files else 0.0
    if rejected_ratio > max_rejected_ratio:
        failures.append("excessive_rejected_person_mask_ratio")
    candidate_ratio = candidate_image_count / len(files) if files else 0.0
    if backend == "grounding_dino_sam2":
        minimum_candidate_ratio = float(config["quality_gate"].get("minimum_candidate_image_ratio", 0.0))
        if candidate_ratio < minimum_candidate_ratio:
            failures.append("insufficient_person_candidate_coverage")
    return {
        "schema_version": "1.0.0",
        "stage": "person_mask",
        "status": "verified" if not failures else "unverified",
        "backend": config["backend"],
        "configuration": dict(config),
        "model": dict(config["model"]),
        "confidence_threshold": threshold if backend == "mediapipe_image_segmenter" else None,
        "detector": config.get("model", {}).get("detector"),
        "segmenter": config.get("model", {}).get("segmenter"),
        "mask_convention": config["mask_convention"],
        "input_image_count": len(files),
        "person_mask_count": len(coverage_values),
        "colmap_mask_count": len(coverage_values),
        "candidate_image_count": candidate_image_count,
        "candidate_image_ratio": candidate_ratio,
        "candidate_box_count": candidate_box_count,
        "candidate_score": {
            "mean": sum(candidate_scores) / len(candidate_scores) if candidate_scores else None,
            "min": min(candidate_scores) if candidate_scores else None,
        },
        "coverage": {
            "mean": sum(coverage_values) / len(coverage_values) if coverage_values else 0.0,
            "median": median(coverage_values) if coverage_values else 0.0,
            "min": min(coverage_values) if coverage_values else 0.0,
            "max": max(coverage_values) if coverage_values else 0.0,
            "raw_max": max(raw_coverage_values) if raw_coverage_values else 0.0,
        },
        "rejected_masks": {
            "coverage_count": rejected_coverage_count,
            "coverage_ratio": rejected_ratio,
            "maximum_allowed_ratio": max_rejected_ratio,
            "examples": rejected_examples,
        },
        "discarded_masks": {
            "low_confidence_large_count": discarded_low_confidence_large_count,
            "large_mask_review_ratio": large_mask_review_ratio,
            "minimum_large_mask_score": minimum_large_mask_score,
            "examples": discarded_low_confidence_large_examples,
        },
        "outputs": {"person_masks": str(person_mask_dir), "colmap_masks": str(colmap_mask_dir)},
        "records": records,
        "failure_reasons": failures,
        "warnings": (
            ([f"rejected_excessive_person_masks:{rejected_coverage_count}"] if rejected_coverage_count else [])
            + ([f"discarded_low_confidence_large_masks:{discarded_low_confidence_large_count}"] if discarded_low_confidence_large_count else [])
            + (["no_person_candidates_detected; emitted empty person masks"] if backend == "grounding_dino_sam2" and candidate_box_count == 0 else [])
            + ([] if backend == "grounding_dino_sam2" else ["MediaPipe Selfie Segmentation is legacy-only and is not accepted by the default production configuration."])
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate person masks and COLMAP keep masks from virtual images.")
    parser.add_argument("--image-dir", type=Path, required=True)
    parser.add_argument("--static-mask-dir", type=Path, required=True)
    parser.add_argument("--person-mask-dir", type=Path, required=True)
    parser.add_argument("--colmap-mask-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--model-asset", type=Path)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if config["backend"] == "grounding_dino_sam2":
            model_asset = Path("/")
        else:
            configured_asset = str(config["model"].get("model_asset_path", ""))
            model_asset = args.model_asset or Path(configured_asset)
            if not configured_asset and args.model_asset is None:
                raise PersonMaskError("person_mask_model_asset_path_missing")
        segment, runtime_version, close_segmenter = create_segmenter(dict(config["model"]), model_asset, config)
        try:
            result = generate_person_masks(args.image_dir, args.static_mask_dir, args.person_mask_dir, args.colmap_mask_dir, config, segment)
        finally:
            close_segmenter()
        result["model"]["runtime_version"] = runtime_version
    except PersonMaskError as error:
        result = {
            "schema_version": "1.0.0",
            "stage": "person_mask",
            "status": "failed",
            "failure_reasons": [str(error)],
            "warnings": [],
        }
    atomic_json(args.report, result)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "verified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
