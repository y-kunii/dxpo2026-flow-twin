#!/usr/bin/env python3
"""Compatibility entry point for the current Scene Repository migration.

The supported migration consolidates all durable data below
``data/scene_repository`` and retires the former flat ``data`` directories.
Keep this short command name for operators and automation; the implementation
lives in ``migrate_scene_repository_layout_v2`` so its format version remains
explicit in journals and tests.
"""

from __future__ import annotations

from migrate_scene_repository_layout_v2 import (
    MIGRATION_ID,
    apply_migration,
    audit,
    main,
    verify,
)


__all__ = ["MIGRATION_ID", "apply_migration", "audit", "verify"]


if __name__ == "__main__":
    raise SystemExit(main())
