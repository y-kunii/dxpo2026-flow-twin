#!/usr/bin/env python3
"""Run repeatable Product Profile checks and write one target-scoped acceptance record.

Hardware, offline-install, and third-party-machine evidence is never inferred
from unit tests.  Missing external evidence stays ``incomplete`` and cannot
promote a release to ``qualified``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.distribution_contracts.lifecycle import record_acceptance, release_records
from packages.distribution_contracts.products import load_product_profile


AUTOMATED: dict[str, tuple[list[str], Path]] = {
    "contract": ([sys.executable, "-m", "pytest", "-q", "tests/packages/test_distribution_products.py",
                  "tests/packages/test_distribution_artifacts.py", "tests/packages/test_distribution_lifecycle.py"], ROOT),
    "viewer": (["npm", "run", "build"], ROOT / "web_viewer/app"),
    "continuation": ([sys.executable, "-m", "pytest", "-q", "tests/services/test_frame_analysis.py"], ROOT),
    "inspection-workflow": ([sys.executable, "-m", "pytest", "-q", "tests/services/test_field_memory.py",
                             "-k", "inspection_issue_corrective_action"], ROOT),
    "grounded-inquiry": ([sys.executable, "scripts/evaluate_evidence_ai.py"], ROOT),
    "change-audit": ([sys.executable, "scripts/evaluate_change_audit.py"], ROOT),
    "report-export": ([sys.executable, "-m", "pytest", "-q", "tests/services/test_field_memory.py",
                       "-k", "inspection_issue_corrective_action"], ROOT),
    "recovery": ([sys.executable, "-m", "pytest", "-q",
                  "tests/integration/test_distribution_overlay_management.py"], ROOT),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _external_status(path: Path | None, subject: str) -> tuple[str, dict[str, Any]]:
    if path is None:
        return "incomplete", {"subject": subject, "reason": "external_evidence_not_supplied"}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return "failed", {"subject": subject, "path": str(path), "error": str(error)}
    status = str(value.get("status", value.get("result", ""))).lower()
    result = "passed" if status in {"passed", "verified", "qualified"} else (
        "failed" if status in {"failed", "rejected"} else "incomplete"
    )
    return result, {
        "subject": subject,
        "path": path.resolve().relative_to(ROOT).as_posix() if path.resolve().is_relative_to(ROOT) else str(path.resolve()),
        "sha256": _sha256(path),
        "reported_status": status,
    }


def _run(name: str) -> tuple[str, dict[str, Any]]:
    command, cwd = AUTOMATED[name]
    started = time.monotonic()
    process = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    return ("passed" if process.returncode == 0 else "failed"), {
        "subject": name,
        "command": command,
        "cwd": cwd.relative_to(ROOT).as_posix() if cwd != ROOT else ".",
        "exit_code": process.returncode,
        "duration_seconds": round(time.monotonic() - started, 3),
        "output_tail": (process.stdout + process.stderr)[-8000:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_id")
    parser.add_argument("--profile", required=True, choices=("viewer-lite", "ai-inspection", "field-operations"))
    parser.add_argument("--qualification-target", required=True)
    parser.add_argument("--os", required=True)
    parser.add_argument("--architecture", required=True)
    parser.add_argument("--gpu-vendor")
    parser.add_argument("--minimum-vram-gb", type=int)
    parser.add_argument("--fixture-id", required=True)
    parser.add_argument("--fixture-manifest", required=True, type=Path)
    parser.add_argument("--vlm-acceptance-report", type=Path)
    parser.add_argument("--offline-install-report", type=Path)
    parser.add_argument("--phase9-report", type=Path)
    args = parser.parse_args()
    profile = load_product_profile(args.profile)
    release = next(
        (item for item in release_records(ROOT) if item.get("release_id") == args.release_id),
        None,
    )
    if release is None:
        raise SystemExit("product release was not found")
    if release.get("product", {}).get("profile_id") != args.profile:
        raise SystemExit("product release and requested Product Profile do not match")
    if args.qualification_target not in profile["platforms"]["qualification_targets"]:
        raise SystemExit("qualification target is not declared by the Product Profile")
    fixture = args.fixture_manifest.resolve()
    if not fixture.is_file() or fixture.is_symlink():
        raise SystemExit("fixture manifest must be a regular file")
    tests: dict[str, str] = {}
    evidence: list[dict[str, Any]] = []
    for name in profile["acceptance"]["required"]:
        if name in AUTOMATED:
            tests[name], item = _run(name)
        elif name == "vlm":
            tests[name], item = _external_status(args.vlm_acceptance_report, name)
        elif name == "offline-install":
            tests[name], item = _external_status(args.offline_install_report, name)
        else:
            tests[name], item = "incomplete", {"subject": name, "reason": "no_automated_or_external_evidence"}
        evidence.append(item)
    if args.phase9_report is not None:
        _, item = _external_status(args.phase9_report, "phase9")
        evidence.append(item)
    target = {
        "os": args.os,
        "architecture": args.architecture,
        "qualification_target": args.qualification_target,
    }
    if args.gpu_vendor:
        target["gpu"] = {"vendor": args.gpu_vendor, "minimum_vram_gb": args.minimum_vram_gb}
    record = record_acceptance(ROOT, {
        "release_id": args.release_id,
        "target": target,
        "dataset": {"fixture_id": args.fixture_id, "sha256": _sha256(fixture), "manifest": str(fixture)},
        "tests": tests,
        "evidence": evidence,
        "notes": "Generated by scripts/qualify_product_release.py; external evidence is never inferred.",
    })
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0 if record["result"] == "qualified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
