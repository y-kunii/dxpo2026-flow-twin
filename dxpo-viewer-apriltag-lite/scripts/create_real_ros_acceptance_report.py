#!/usr/bin/env python3
"""Create Phase 9 evidence from a live, recorded rosbridge transform log."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from examples.rosbridge.transform_log import TOPIC, load_records


OPERATOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$")


def assess(
    input_path: Path,
    *,
    scene_package_id: str,
    operator_id: str,
    real_hardware: bool,
    visual_confirmation: bool,
    minimum_messages: int,
    minimum_duration_sec: float,
) -> dict[str, Any]:
    if OPERATOR_RE.fullmatch(operator_id) is None:
        raise ValueError("operator_id_invalid")
    records = load_records(input_path)
    recorded_times = [int(item["recorded_at_ns"]) for item in records]
    duration_sec = max(0.0, (recorded_times[-1] - recorded_times[0]) / 1_000_000_000)
    monotonic = all(right >= left for left, right in zip(recorded_times, recorded_times[1:], strict=False))
    messages = [item["message"] for item in records]
    boxes = sorted({str(item["child_frame_id"]) for item in messages})
    fixed_frames = sorted({str(item["header"]["frame_id"]) for item in messages})
    reasons: list[str] = []
    if not real_hardware:
        reasons.append("real_hardware_not_asserted")
    if not visual_confirmation:
        reasons.append("viewer_visual_confirmation_missing")
    if len(records) < minimum_messages:
        reasons.append("minimum_message_count_not_met")
    if duration_sec < minimum_duration_sec:
        reasons.append("minimum_observation_duration_not_met")
    if not monotonic:
        reasons.append("record_receive_time_not_monotonic")
    if not boxes:
        reasons.append("tracked_box_missing")
    digest = hashlib.sha256(input_path.read_bytes()).hexdigest()
    return {
        "schema_version": "real-ros2-dynamic-tracking-acceptance-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "verified" if not reasons else "incomplete",
        "real_hardware": real_hardware,
        "visual_confirmation": visual_confirmation,
        "operator_id": operator_id,
        "scene_package_id": scene_package_id,
        "topic": TOPIC,
        "source_log": {
            "path": str(input_path),
            "sha256": digest,
            "message_count": len(records),
            "duration_sec": round(duration_sec, 3),
            "receive_time_monotonic": monotonic,
        },
        "observed": {"fixed_tag_frames": fixed_frames, "box_ids": boxes},
        "acceptance_thresholds": {
            "minimum_messages": minimum_messages,
            "minimum_duration_sec": minimum_duration_sec,
        },
        "failure_reasons": reasons,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scene-package-id", required=True)
    parser.add_argument("--operator-id", required=True)
    parser.add_argument("--real-hardware", action="store_true")
    parser.add_argument("--visual-confirmation", action="store_true")
    parser.add_argument("--minimum-messages", type=int, default=20)
    parser.add_argument("--minimum-duration-sec", type=float, default=2.0)
    args = parser.parse_args()
    if args.minimum_messages < 1 or args.minimum_duration_sec <= 0:
        parser.error("acceptance thresholds must be positive")
    report = assess(
        args.input.resolve(),
        scene_package_id=args.scene_package_id,
        operator_id=args.operator_id,
        real_hardware=args.real_hardware,
        visual_confirmation=args.visual_confirmation,
        minimum_messages=args.minimum_messages,
        minimum_duration_sec=args.minimum_duration_sec,
    )
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    print(json.dumps({"status": report["status"], "report": str(output)}, ensure_ascii=False))
    return 0 if report["status"] == "verified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
