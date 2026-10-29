#!/usr/bin/env python3
"""Fail closed unless the operator-supplied public release evidence is complete."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from canonical_artifacts import read_and_verify  # noqa: E402
from packages.public_deployment_acceptance import public_deployment_failures  # noqa: E402


def read_json(path: Path, label: str, failures: list[str]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        failures.append(f"{label}_unreadable:{error}")
        return {}
    if not isinstance(value, dict):
        failures.append(f"{label}_must_be_object")
        return {}
    return value


def check(args: argparse.Namespace) -> list[str]:
    failures: list[str] = []
    license_path = args.repo_root / "LICENSE"
    if not license_path.is_file() or license_path.stat().st_size == 0:
        failures.append("repository_license_missing")
    security_path = args.repo_root / "SECURITY.md"
    if not security_path.is_file() or security_path.stat().st_size == 0:
        failures.append("security_policy_missing")
    codeowners_path = args.repo_root / ".github/CODEOWNERS"
    if not codeowners_path.is_file() or codeowners_path.stat().st_size == 0:
        failures.append("codeowners_missing")
    else:
        codeowners = codeowners_path.read_text(encoding="utf-8", errors="replace")
        if "REPLACE_WITH_" in codeowners or not any(
            line.strip() and not line.lstrip().startswith("#") for line in codeowners.splitlines()
        ):
            failures.append("codeowners_not_configured")
    notice = args.repo_root / "THIRD_PARTY_NOTICES.md"
    if not notice.is_file() or notice.stat().st_size == 0:
        failures.append("third_party_notices_missing")

    acceptance = read_json(args.real_acceptance_report, "real_acceptance_report", failures)
    if acceptance.get("status") != "verified" or acceptance.get("real_hardware") is not True:
        failures.append("real_hardware_acceptance_not_verified")
    if acceptance.get("topic") != "/box/relative_transform":
        failures.append("real_hardware_acceptance_topic_mismatch")
    failures.extend(public_deployment_failures(args.public_deployment_report))

    try:
        read_and_verify(args.canonical_backup)
    except (OSError, ValueError) as error:
        failures.append(f"canonical_backup_not_verified:{error}")

    try:
        generation_text = args.generation_verification.read_text(encoding="utf-8")
    except OSError as error:
        failures.append(f"generation_gpu_verification_unreadable:{error}")
    else:
        if "generation environment: VERIFIED" not in generation_text:
            failures.append("generation_gpu_verification_not_verified")

    if not args.allow_dirty:
        status = subprocess.run(
            ["git", "-C", str(args.repo_root), "status", "--porcelain", "--untracked-files=all"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        if status.strip():
            failures.append("repository_not_clean")
    return sorted(set(failures))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--real-acceptance-report", type=Path, required=True)
    parser.add_argument("--canonical-backup", type=Path, required=True)
    parser.add_argument("--generation-verification", type=Path, required=True)
    parser.add_argument("--public-deployment-report", type=Path, required=True)
    parser.add_argument("--allow-dirty", action="store_true", help="diagnostic only; public release must omit this")
    args = parser.parse_args()
    args.repo_root = args.repo_root.resolve()
    failures = check(args)
    report = {"status": "verified" if not failures else "failed", "failure_reasons": failures}
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
