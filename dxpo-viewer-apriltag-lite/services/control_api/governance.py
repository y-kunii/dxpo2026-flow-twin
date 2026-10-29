"""Small, fail-closed RBAC boundary for field-memory APIs.

Authentication remains an installation concern.  This module consumes only
identity headers injected by the trusted local proxy.  Development keeps the
historic single-user behaviour unless ``SPATIAL_RBAC_REQUIRED=1`` is set.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any


ROLES = frozenset({"viewer", "operator", "reviewer", "admin"})
IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$")


@dataclass(frozen=True)
class Principal:
    actor_id: str
    role: str
    site_id: str | None


def principal_from_headers(headers: Any) -> Principal:
    required = os.environ.get("SPATIAL_RBAC_REQUIRED", "0").strip() == "1"
    actor = str(headers.get("x-spatial-actor-id", "")).strip()
    role = str(headers.get("x-spatial-role", "")).strip().lower()
    site = str(headers.get("x-spatial-site-id", "")).strip() or None
    if not actor and not role and not required:
        return Principal("local-operator", "admin", None)
    if IDENTITY_RE.fullmatch(actor) is None:
        raise PermissionError("spatial_actor_id_required")
    if role not in ROLES:
        raise PermissionError("spatial_role_invalid")
    if site is not None and IDENTITY_RE.fullmatch(site) is None:
        raise PermissionError("spatial_site_scope_invalid")
    if role != "admin" and site is None:
        raise PermissionError("spatial_site_scope_required")
    return Principal(actor, role, site)


def authorize(principal: Principal, method: str, path: str) -> None:
    """Authorize the closed API families before their service is called."""

    if principal.role == "admin":
        return
    if (
        path.startswith("/api/frame-analysis/scene-versions/")
        and (path.endswith("/reset-preflight") or path.endswith("/reset"))
    ):
        raise PermissionError("spatial_admin_role_required")
    if path in {"/api/field-memory/audit-events", "/api/field-memory/summary"}:
        raise PermissionError("spatial_admin_role_required")
    if path.startswith("/api/change-review/"):
        if principal.role != "reviewer":
            raise PermissionError("spatial_reviewer_role_required")
        return
    if (
        path.startswith("/api/frame-analysis/frames/")
        and path.endswith("/reviews")
        and method == "POST"
    ):
        if principal.role != "reviewer":
            raise PermissionError("spatial_reviewer_role_required")
        return
    if path.startswith("/api/field-memory/corrective-actions/") and path.endswith("/approvals"):
        if principal.role not in {"reviewer", "admin"}:
            raise PermissionError("spatial_reviewer_role_required")
        return
    if method in {"GET", "HEAD", "OPTIONS"}:
        return
    if path.startswith("/api/evidence-ai/inquiries"):
        # Asking a site-scoped question does not mutate canonical field facts.
        if principal.role in {"viewer", "operator", "reviewer"}:
            return
    if principal.role not in {"operator", "reviewer"}:
        raise PermissionError("spatial_operator_role_required")


def enforce_site_scope(principal: Principal, payload: dict[str, Any] | None, result: Any) -> None:
    """Reject cross-site payloads and service results for non-admin callers."""

    if principal.role == "admin" or principal.site_id is None:
        return
    if payload is not None:
        requested_site = payload.get("site_id")
        if requested_site is not None and requested_site != principal.site_id:
            raise PermissionError("spatial_site_scope_forbidden")

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            site_id = value.get("site_id")
            if site_id is not None and site_id != principal.site_id:
                raise PermissionError("spatial_site_scope_forbidden")
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(result)


def filter_site_scoped_collection(principal: Principal, path: str, result: Any) -> Any:
    """Project global collection routes onto a non-admin caller's Site."""

    if principal.role == "admin" or principal.site_id is None or not isinstance(result, list):
        return result
    collection_routes = {
        "/api/field-memory/sites",
        "/api/spatial-search/sites",
        "/api/frame-analysis/scene-versions",
        "/api/frame-analysis/runs",
    }
    if path not in collection_routes:
        return result
    return [
        item for item in result
        if isinstance(item, dict) and item.get("site_id") == principal.site_id
    ]
