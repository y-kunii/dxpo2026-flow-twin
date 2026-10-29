#!/usr/bin/env python3
"""Inspect product artifacts and record explicit release/acceptance decisions."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.distribution_contracts.lifecycle import (
    apply_retention_plan,
    artifact_inventory,
    record_acceptance,
    retention_preview,
    transition_release,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("index")
    commands.add_parser("inventory", help="Alias of index")
    retention_preview_parser = commands.add_parser("retention-preview")
    retention_preview_parser.add_argument("--output", type=Path)
    retention_apply = commands.add_parser("retention-apply")
    retention_apply.add_argument("--input", required=True, type=Path)
    retention_apply.add_argument(
        "--confirm-sha256",
        "--confirmation-sha256",
        dest="confirm_sha256",
        required=True,
    )
    retention_apply.add_argument("--actor", required=True)
    acceptance = commands.add_parser("record-acceptance")
    acceptance.add_argument("--input", required=True, type=Path)
    transition = commands.add_parser("transition")
    transition.add_argument("release_id")
    transition.add_argument("target")
    transition.add_argument("--actor", required=True)
    transition.add_argument("--reason", required=True)
    args = parser.parse_args()
    if args.command in {"index", "inventory"}:
        result = artifact_inventory(ROOT)
    elif args.command == "retention-preview":
        result = retention_preview(ROOT)
    elif args.command == "record-acceptance":
        result = record_acceptance(
            ROOT,
            json.loads(args.input.read_text(encoding="utf-8")),
        )
    elif args.command == "retention-apply":
        result = apply_retention_plan(
            ROOT,
            json.loads(args.input.read_text(encoding="utf-8")),
            confirmation_sha256=args.confirm_sha256,
            actor=args.actor,
        )
    else:
        result = transition_release(
            ROOT,
            args.release_id,
            args.target,
            actor=args.actor,
            reason=args.reason,
        )
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    output = getattr(args, "output", None)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
