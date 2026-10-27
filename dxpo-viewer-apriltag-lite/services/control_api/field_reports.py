"""Auditable CSV/PDF reports for the field-operations product profile."""

from __future__ import annotations

import csv
import hashlib
import json
import textwrap
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image, ImageDraw, ImageFont

if TYPE_CHECKING:
    from .field_memory import FieldMemoryService


REPORT_SCHEMA_VERSION = "1.0.0"
PAGE_SIZE = (1240, 1754)


def _canonical_digest(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _runtime_version(repo_root: Path) -> dict[str, str]:
    path = repo_root / "packages/distribution_contracts/runtime_release.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        value = {}
    return {
        "runtime_id": str(value.get("runtime_id", "spatial-3dgs-runtime")),
        "runtime_version": str(value.get("version", "unknown")),
        "contract_version": str(value.get("contract_version", "unknown")),
    }


def _report_snapshot(service: FieldMemoryService, site_id: str) -> dict[str, Any]:
    service.site(site_id)
    with service.read_connection() as connection:
        site = dict(connection.execute("SELECT * FROM sites WHERE site_id = ?", (site_id,)).fetchone())
        inspections = [
            dict(row) for row in connection.execute(
                "SELECT * FROM inspections WHERE site_id = ? ORDER BY created_at", (site_id,)
            )
        ]
        observations = [
            dict(row) for row in connection.execute(
                "SELECT observations.* FROM inspection_observations AS observations "
                "JOIN inspections USING(inspection_id) WHERE inspections.site_id = ? ORDER BY observations.created_at",
                (site_id,),
            )
        ]
        issues = [
            dict(row) for row in connection.execute(
                "SELECT issues.*, assets.name AS asset_name, inspections.title AS inspection_title, "
                "scene_versions.scene_package_id FROM issues "
                "JOIN inspections USING(inspection_id) "
                "LEFT JOIN assets USING(asset_id) "
                "LEFT JOIN scene_versions ON issues.scene_version_id = scene_versions.scene_version_id "
                "WHERE issues.site_id = ? ORDER BY issues.created_at",
                (site_id,),
            )
        ]
        actions = [
            dict(row) for row in connection.execute(
                "SELECT actions.* FROM corrective_actions AS actions JOIN issues USING(issue_id) "
                "WHERE issues.site_id = ? ORDER BY actions.created_at",
                (site_id,),
            )
        ]
        approvals = [
            dict(row) for row in connection.execute(
                "SELECT approvals.* FROM corrective_action_approvals AS approvals "
                "JOIN corrective_actions AS actions USING(corrective_action_id) "
                "JOIN issues USING(issue_id) WHERE issues.site_id = ? ORDER BY approvals.created_at",
                (site_id,),
            )
        ]
        scene_versions = [
            dict(row) for row in connection.execute(
                "SELECT scene_version_id, scene_package_id, captured_at FROM scene_versions "
                "WHERE site_id = ? ORDER BY created_at", (site_id,),
            )
        ]
        ai_models = [
            dict(row) for row in connection.execute(
                "SELECT DISTINCT model_id, model_revision, prompt_version FROM analysis_runs "
                "WHERE site_id = ? ORDER BY model_id, model_revision", (site_id,),
            )
        ]
        database_schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    return {
        "site": site,
        "inspections": inspections,
        "observations": observations,
        "issues": issues,
        "corrective_actions": actions,
        "approvals": approvals,
        "scene_versions": scene_versions,
        "ai_models": ai_models,
        "database_schema_version": database_schema_version,
    }


def _write_csv(path: Path, snapshot: dict[str, Any], provenance: dict[str, Any]) -> None:
    actions_by_issue: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for action in snapshot["corrective_actions"]:
        actions_by_issue[action["issue_id"]].append(action)
    fields = [
        "report_schema_version", "generated_at", "product_profile", "runtime_version",
        "site_id", "inspection_id", "inspection_title", "issue_id", "title", "description",
        "severity", "issue_status", "assignee", "due_at", "scene_version_id", "scene_package_id",
        "asset_id", "asset_name", "position_x", "position_y", "position_z",
        "evidence_before_document_id", "corrective_action_id", "corrective_action",
        "corrective_action_status", "assigned_to", "performed_at", "evidence_after_document_id",
        "approval_decision", "approval_reviewer", "record_set_sha256",
    ]
    approvals_by_action: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for approval in snapshot["approvals"]:
        approvals_by_action[approval["corrective_action_id"]].append(approval)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for issue in snapshot["issues"]:
            actions = actions_by_issue[issue["issue_id"]] or [{}]
            for action in actions:
                approvals = approvals_by_action.get(action.get("corrective_action_id"), [])
                approval = approvals[-1] if approvals else {}
                writer.writerow({
                    "report_schema_version": REPORT_SCHEMA_VERSION,
                    "generated_at": provenance["generated_at"],
                    "product_profile": "field-operations",
                    "runtime_version": provenance["runtime_version"],
                    "site_id": issue["site_id"],
                    "inspection_id": issue["inspection_id"],
                    "inspection_title": issue["inspection_title"],
                    "issue_id": issue["issue_id"],
                    "title": issue["title"],
                    "description": issue["description"],
                    "severity": issue["severity"],
                    "issue_status": issue["status"],
                    "assignee": issue["assignee"] or "",
                    "due_at": issue["due_at"] or "",
                    "scene_version_id": issue["scene_version_id"] or "",
                    "scene_package_id": issue["scene_package_id"] or "",
                    "asset_id": issue["asset_id"] or "",
                    "asset_name": issue["asset_name"] or "",
                    "position_x": issue["position_x"] if issue["position_x"] is not None else "",
                    "position_y": issue["position_y"] if issue["position_y"] is not None else "",
                    "position_z": issue["position_z"] if issue["position_z"] is not None else "",
                    "evidence_before_document_id": issue["evidence_before_document_id"] or "",
                    "corrective_action_id": action.get("corrective_action_id", ""),
                    "corrective_action": action.get("summary", ""),
                    "corrective_action_status": action.get("status", ""),
                    "assigned_to": action.get("assigned_to") or "",
                    "performed_at": action.get("performed_at") or "",
                    "evidence_after_document_id": action.get("evidence_after_document_id") or "",
                    "approval_decision": approval.get("decision", ""),
                    "approval_reviewer": approval.get("reviewer", ""),
                    "record_set_sha256": provenance["record_set_sha256"],
                })


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        "/usr/share/fonts/opentype/ipafont-gothic/ipagp.ttf",
        "/usr/share/fonts/opentype/unifont/unifont_jp.otf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def _write_pdf(path: Path, snapshot: dict[str, Any], provenance: dict[str, Any]) -> None:
    title_font, heading_font, body_font, small_font = _font(38), _font(25), _font(20), _font(15)
    pages: list[Image.Image] = []

    def page() -> tuple[Image.Image, ImageDraw.ImageDraw, int]:
        image = Image.new("RGB", PAGE_SIZE, "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, PAGE_SIZE[0], 86), fill="#0f2740")
        draw.text((48, 22), "Spatial 3DGS — 点検・是正報告", fill="white", font=heading_font)
        return image, draw, 116

    image, draw, y = page()
    draw.text((48, y), str(snapshot["site"]["name"]), fill="#102030", font=title_font)
    y += 62
    provenance_lines = [
        f"現場ID: {snapshot['site']['site_id']}",
        f"生成日時: {provenance['generated_at']}",
        f"製品: Field Operations / Runtime {provenance['runtime_version']}",
        f"Report schema: {REPORT_SCHEMA_VERSION} / DB schema: {snapshot['database_schema_version']}",
        f"Record set SHA-256: {provenance['record_set_sha256']}",
        "SceneVersion: " + ", ".join(item["scene_version_id"] for item in snapshot["scene_versions"]) or "なし",
        "AI model: " + (", ".join(
            f"{item['model_id']}@{item['model_revision']}" for item in snapshot["ai_models"]
        ) or "なし"),
    ]
    for line in provenance_lines:
        draw.text((48, y), line, fill="#24384c", font=small_font)
        y += 28
    y += 18
    draw.text((48, y), "集計", fill="#102030", font=heading_font)
    y += 42
    counts = {
        "点検": len(snapshot["inspections"]),
        "観測": len(snapshot["observations"]),
        "指摘": len(snapshot["issues"]),
        "未完了指摘": sum(item["status"] not in {"closed", "rejected"} for item in snapshot["issues"]),
        "是正処置": len(snapshot["corrective_actions"]),
        "承認": sum(item["decision"] == "approved" for item in snapshot["approvals"]),
    }
    for label, value in counts.items():
        draw.text((66, y), f"{label}: {value}件", fill="#102030", font=body_font)
        y += 36
    pages.append(image)

    actions_by_issue: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for action in snapshot["corrective_actions"]:
        actions_by_issue[action["issue_id"]].append(action)
    for issue in snapshot["issues"]:
        image, draw, y = page()
        draw.text((48, y), f"指摘 {issue['issue_id']}", fill="#102030", font=heading_font)
        y += 48
        for label, value in (
            ("件名", issue["title"]), ("重要度 / 状態", f"{issue['severity']} / {issue['status']}"),
            ("点検", f"{issue['inspection_title']} ({issue['inspection_id']})"),
            ("設備", issue["asset_name"] or issue["asset_id"] or "未指定"),
            ("SceneVersion", issue["scene_version_id"] or "未指定"),
            ("担当 / 期限", f"{issue['assignee'] or '未割当'} / {issue['due_at'] or '未設定'}"),
            ("是正前証跡", issue["evidence_before_document_id"] or "未登録"),
        ):
            draw.text((48, y), f"{label}: {value}", fill="#24384c", font=body_font)
            y += 37
        y += 8
        draw.text((48, y), "内容", fill="#102030", font=heading_font)
        y += 38
        for line in textwrap.wrap(issue["description"] or "（説明なし）", width=54):
            draw.text((66, y), line, fill="#24384c", font=body_font)
            y += 32
        y += 16
        draw.text((48, y), "是正処置", fill="#102030", font=heading_font)
        y += 40
        actions = actions_by_issue.get(issue["issue_id"], [])
        if not actions:
            draw.text((66, y), "未登録", fill="#8b3a3a", font=body_font)
        for action in actions:
            for line in textwrap.wrap(
                f"[{action['status']}] {action['summary']} / 担当 {action['assigned_to'] or '未割当'} / "
                f"是正後証跡 {action['evidence_after_document_id'] or '未登録'}", width=58,
            ):
                draw.text((66, y), line, fill="#24384c", font=body_font)
                y += 31
            y += 10
        draw.text((48, PAGE_SIZE[1] - 58), f"record-set sha256:{provenance['record_set_sha256']}",
                  fill="#596b7a", font=small_font)
        pages.append(image)
    first, *rest = pages
    first.save(path, "PDF", resolution=150.0, save_all=True, append_images=rest)


def generate_field_operations_report(service: FieldMemoryService, site_id: str) -> dict[str, Any]:
    snapshot = _report_snapshot(service, site_id)
    generated_at = datetime.now(timezone.utc).isoformat()
    report_id = f"field-report_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}_{uuid.uuid4().hex[:8]}"
    report_root = service.repository_layout.reports / "field-operations" / report_id
    report_root.mkdir(parents=True, exist_ok=False)
    runtime = _runtime_version(service.repo_root)
    provenance = {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "report_id": report_id,
        "generated_at": generated_at,
        "product_profile": "field-operations",
        **runtime,
        "site_id": site_id,
        "scene_versions": snapshot["scene_versions"],
        "inspection_ids": [item["inspection_id"] for item in snapshot["inspections"]],
        "ai_models": snapshot["ai_models"],
        "database_schema_version": snapshot["database_schema_version"],
        "record_set_sha256": _canonical_digest(snapshot),
    }
    csv_path = report_root / "issues.csv"
    pdf_path = report_root / "inspection-report.pdf"
    _write_csv(csv_path, snapshot, provenance)
    _write_pdf(pdf_path, snapshot, provenance)
    files = [
        {"filename": csv_path.name, "media_type": "text/csv", "sha256": _file_digest(csv_path), "size": csv_path.stat().st_size},
        {"filename": pdf_path.name, "media_type": "application/pdf", "sha256": _file_digest(pdf_path), "size": pdf_path.stat().st_size},
    ]
    manifest = {**provenance, "files": files}
    manifest_path = report_root / "report-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest["files"].append({
        "filename": manifest_path.name, "media_type": "application/json",
        "sha256": _file_digest(manifest_path), "size": manifest_path.stat().st_size,
    })
    return {
        **manifest,
        "downloads": [
            {**item, "url": f"/api/field-reports/{report_id}/{item['filename']}"}
            for item in manifest["files"]
        ],
    }


def resolve_report_file(service: FieldMemoryService, report_id: str, filename: str) -> tuple[Path, str, str]:
    if not report_id.startswith("field-report_") or any(part in report_id for part in ("/", "\\", "..")):
        raise ValueError("field_report_id_invalid")
    if filename not in {"issues.csv", "inspection-report.pdf", "report-manifest.json"}:
        raise ValueError("field_report_filename_invalid")
    root = (service.repository_layout.reports / "field-operations" / report_id).resolve()
    path = (root / filename).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.is_symlink():
        raise FileNotFoundError(f"field_report_not_found:{report_id}:{filename}")
    try:
        manifest = json.loads((root / "report-manifest.json").read_text(encoding="utf-8"))
        site_id = str(manifest["site_id"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("field_report_manifest_invalid") from error
    media = {
        "issues.csv": "text/csv; charset=utf-8",
        "inspection-report.pdf": "application/pdf",
        "report-manifest.json": "application/json",
    }[filename]
    return path, media, site_id
