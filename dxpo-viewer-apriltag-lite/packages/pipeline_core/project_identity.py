"""Stable App A project identity helpers.

This module is intentionally independent of the HTTP adapter so project and
artifact services can share the same durable workspace naming contract.
"""

from __future__ import annotations

import re


PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def project_name_from_workspace_uri(workspace_uri: object) -> str | None:
    """Return the App A project name encoded below the ``projects`` root."""
    if not isinstance(workspace_uri, str) or not workspace_uri:
        return None
    parts = [part for part in workspace_uri.replace("\\", "/").split("/") if part]
    try:
        index = parts.index("projects")
    except ValueError:
        return None
    if index + 1 >= len(parts):
        return None
    candidate = parts[index + 1]
    return candidate if PROJECT_NAME_RE.fullmatch(candidate) else None


__all__ = ["PROJECT_NAME_RE", "project_name_from_workspace_uri"]
