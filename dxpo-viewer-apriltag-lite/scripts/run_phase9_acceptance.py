#!/usr/bin/env python3
"""Stable operator entry point for Phase 9 real-data acceptance."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.phase9_acceptance import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
