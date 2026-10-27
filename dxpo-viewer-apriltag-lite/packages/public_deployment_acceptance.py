"""Validate operator-supplied public deployment acceptance evidence.

This module is intentionally independent from the Phase 9 collector so the
release-readiness command does not import pipeline, image, or Viewer runtime
dependencies merely to validate a small JSON contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _read_report(path: Path | None) -> tuple[dict[str, Any], str | None]:
    if path is None:
        return {}, "evidence_not_supplied"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {}, f"evidence_unreadable:{error}"
    if not isinstance(value, dict):
        return {}, "evidence_must_be_object"
    return value, None


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def public_deployment_failures(path: Path | None) -> list[str]:
    """Return fail-closed reasons for an invalid public deployment report."""

    report, error = _read_report(path)
    if error:
        return [f"public_deployment_{error}"]
    failures: list[str] = []
    if report.get("schema_version") != "public-deployment-acceptance-v1":
        failures.append("public_deployment_schema_invalid")
    if report.get("status") != "verified":
        failures.append("public_deployment_not_verified")
    if not str(report.get("base_url", "")).startswith("https://"):
        failures.append("tls_not_configured")
    authentication = _object(report.get("authentication"))
    if authentication.get("proxy_kind") not in {"oidc", "saml"}:
        failures.append("authentication_proxy_not_verified")
    if authentication.get("anonymous_access_rejected") is not True:
        failures.append("anonymous_access_not_rejected")
    if authentication.get("identity_header_spoofing_rejected") is not True:
        failures.append("identity_header_spoofing_not_rejected")
    tls = _object(report.get("tls"))
    if tls.get("validated") is not True or tls.get("minimum_protocol") not in {"TLSv1.2", "TLSv1.3"}:
        failures.append("tls_not_verified")
    authorization = _object(report.get("authorization"))
    if authorization.get("role_matrix_verified") is not True:
        failures.append("multi_user_role_matrix_not_verified")
    if authorization.get("site_isolation_verified") is not True:
        failures.append("multi_user_site_isolation_not_verified")
    network = _object(report.get("network"))
    if network.get("direct_api_exposed") is not False:
        failures.append("direct_api_exposure_not_rejected")
    if network.get("docker_socket_exposed") is not False:
        failures.append("docker_socket_exposure_not_rejected")
    return sorted(set(failures))


__all__ = ["public_deployment_failures"]
