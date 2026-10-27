"""Reviewed analysis-purpose catalog and purpose-specific output contracts.

The model choice controls capacity and VRAM use.  An analysis policy controls
what the model is asked to observe, which schema is accepted, and which
downstream workflows may consume a reviewed result.  Policies are deliberately
closed and versioned: arbitrary prompts cannot silently create records that
look compatible with operational evidence.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any


DEFAULT_ANALYSIS_POLICY_ID = "site-observation"
LEGACY_ANALYSIS_POLICY_ID = "legacy-caption"


@dataclass(frozen=True)
class AnalysisPolicy:
    analysis_policy_id: str
    label: str
    description: str
    use_when: str
    prompt_template_id: str
    prompt_version: str
    output_schema_version: str
    downstream_uses: tuple[str, ...]
    output_fields: tuple[str, ...]
    prompt: str
    review_required: bool = True

    @property
    def prompt_template_sha256(self) -> str:
        return hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()

    def api_payload(self) -> dict[str, Any]:
        return {
            "analysis_policy_id": self.analysis_policy_id,
            "label": self.label,
            "description": self.description,
            "use_when": self.use_when,
            "prompt_template_id": self.prompt_template_id,
            "prompt_version": self.prompt_version,
            "prompt_template_sha256": self.prompt_template_sha256,
            "output_schema_version": self.output_schema_version,
            "downstream_uses": list(self.downstream_uses),
            "output_fields": list(self.output_fields),
            "review_required": self.review_required,
        }


_COMMON_HEADER = (
    "これは現場画像を検索・3D確認・人手レビューへつなぐローカル解析です。"
    "画像で直接確認できる事実だけを日本語で記録し、故障、危険、設備の固有名称、"
    "人物の属性を推測しないでください。見えない内容はlimitationsへ記録してください。"
    "JSON以外は絶対に出力しないでください。"
)


ANALYSIS_POLICIES: dict[str, AnalysisPolicy] = {
    "site-observation": AnalysisPolicy(
        analysis_policy_id="site-observation",
        label="現場全体の観測",
        description="場所・設備・状態・注意事項・表示文字を横断検索できる標準観測を作ります。",
        use_when="現場画像を幅広く検索し、3D視点と現場AIで再利用するとき",
        prompt_template_id="site-observation",
        prompt_version="spatial-observation-v2",
        output_schema_version="frame-observation-v2",
        downstream_uses=("search", "viewer", "grounded-ai", "change-review"),
        output_fields=(
            "場所",
            "見える設備",
            "状態",
            "注意事項",
            "表示文字",
            "画像品質",
            "制約",
        ),
        prompt=_COMMON_HEADER
        + (
            "目的は現場全体の観測索引です。形式は "
            '{"summary":"1文の観測要約","keywords":["検索語"],"extra":{'
            '"schema_version":"frame-observation-v2",'
            '"area":{"label":"場所種別","confidence":0.0},'
            '"visible_assets":[{"label":"見える対象","category":"分類",'
            '"state":"見たままの状態","evidence":"画像上の根拠","confidence":0.0}],'
            '"conditions":[{"subject":"対象","observation":"観測事実",'
            '"evidence":"画像上の根拠","severity":"info|attention|critical",'
            '"confidence":0.0}],'
            '"safety_observations":[{"label":"観測事実","evidence":"画像上の根拠",'
            '"severity":"info|attention|critical","confidence":0.0}],'
            '"visible_text":[{"text":"読めた文字","context":"表示場所",'
            '"confidence":0.0}],'
            '"image_quality":{"usable":true,"visibility":"good|partial|poor",'
            '"blur":"none|mild|severe","occlusion":"none|partial|heavy"},'
            '"limitations":["判断できない理由"]}} です。'
            "注意候補には根拠とconfidenceを必ず付けてください。"
        ),
    ),
    "asset-condition": AnalysisPolicy(
        analysis_policy_id="asset-condition",
        label="設備・状態確認",
        description="見える設備、識別表示、外観状態、点検時の要確認事項を根拠付きで整理します。",
        use_when="設備候補や外観状態を画像から拾い、人が点検対象を確認するとき",
        prompt_template_id="asset-condition",
        prompt_version="asset-condition-v1",
        output_schema_version="asset-condition-observation-v1",
        downstream_uses=("search", "viewer", "grounded-ai", "inspection"),
        output_fields=(
            "設備候補",
            "識別表示",
            "外観状態",
            "点検観測",
            "重要度",
            "画像品質",
            "制約",
        ),
        prompt=_COMMON_HEADER
        + (
            "目的は設備と外観状態の点検候補作成です。画像だけで故障や適否を確定しないでください。形式は "
            '{"summary":"1文の設備観測要約","keywords":["設備検索語"],"extra":{'
            '"schema_version":"asset-condition-observation-v1",'
            '"area":{"label":"場所種別","confidence":0.0},'
            '"assets":[{"label":"設備候補","category":"分類",'
            '"identifier_text":"画像で読めた識別表示","condition":"見たままの外観",'
            '"evidence":"画像上の根拠","severity":"info|attention|critical",'
            '"confidence":0.0}],'
            '"inspection_observations":[{"subject":"対象","observation":"点検候補",'
            '"evidence":"画像上の根拠","severity":"info|attention|critical",'
            '"confidence":0.0}],'
            '"image_quality":{"usable":true,"visibility":"good|partial|poor",'
            '"blur":"none|mild|severe","occlusion":"none|partial|heavy"},'
            '"limitations":["確認できない事項"]}} です。'
        ),
    ),
    "text-signage": AnalysisPolicy(
        analysis_policy_id="text-signage",
        label="表示・文書記録",
        description="掲示、標識、銘板などの読めた文字と表示位置・可読性を検索可能にします。",
        use_when="銘板、案内、注意表示、室名などを画像横断で探すとき",
        prompt_template_id="text-signage",
        prompt_version="text-signage-v1",
        output_schema_version="text-signage-observation-v1",
        downstream_uses=("search", "viewer", "grounded-ai", "document-review"),
        output_fields=(
            "表示文字",
            "表示位置",
            "言語",
            "可読性",
            "表示種別",
            "画像品質",
            "制約",
        ),
        prompt=_COMMON_HEADER
        + (
            "目的は表示文字と掲示物の記録です。読めない文字を補完しないでください。形式は "
            '{"summary":"1文の表示観測要約","keywords":["文字検索語"],"extra":{'
            '"schema_version":"text-signage-observation-v1",'
            '"area":{"label":"場所種別","confidence":0.0},'
            '"text_regions":[{"text":"実際に読めた文字","context":"表示位置",'
            '"language":"言語","legibility":"clear|partial|unreadable",'
            '"confidence":0.0}],'
            '"signage":[{"label":"表示種別","text":"読めた文字",'
            '"evidence":"画像上の特徴","confidence":0.0}],'
            '"image_quality":{"usable":true,"visibility":"good|partial|poor",'
            '"blur":"none|mild|severe","occlusion":"none|partial|heavy"},'
            '"limitations":["判読できない理由"]}} です。'
        ),
    ),
    "concise-caption": AnalysisPolicy(
        analysis_policy_id="concise-caption",
        label="簡潔な画像説明",
        description="教材・報告書向けに、推測を避けた短い説明と主要被写体を作ります。",
        use_when="運用判断ではなく、人が読む一覧や教材用の短い説明が必要なとき",
        prompt_template_id="concise-caption",
        prompt_version="concise-caption-v1",
        output_schema_version="concise-caption-v1",
        downstream_uses=("search", "viewer", "report"),
        output_fields=("短い説明", "主要被写体", "場所種別", "画像品質", "制約"),
        prompt=_COMMON_HEADER
        + (
            "目的は教材・報告書向けの簡潔な画像説明です。業務上の状態判定や安全判定をしないでください。形式は "
            '{"summary":"80文字以内の自然な説明","keywords":["主要検索語"],"extra":{'
            '"schema_version":"concise-caption-v1","caption":"80文字以内の説明",'
            '"scene_type":"場所種別","main_subjects":["主要被写体"],'
            '"image_quality":{"usable":true,"visibility":"good|partial|poor",'
            '"blur":"none|mild|severe","occlusion":"none|partial|heavy"},'
            '"limitations":["説明上の制約"]}} です。'
        ),
    ),
}


def reviewed_analysis_policy(value: Any) -> AnalysisPolicy:
    if not isinstance(value, str) or value not in ANALYSIS_POLICIES:
        raise ValueError(f"frame_analysis_policy_not_reviewed:{value}")
    return ANALYSIS_POLICIES[value]


def analysis_policy_catalog() -> dict[str, Any]:
    return {
        "default_analysis_policy_id": DEFAULT_ANALYSIS_POLICY_ID,
        "policies": [policy.api_payload() for policy in ANALYSIS_POLICIES.values()],
    }


def _text(value: Any, field: str, *, maximum: int = 600, required: bool = False) -> str:
    if value in (None, ""):
        if required:
            raise ValueError(f"frame_analysis_policy_{field}_required")
        return ""
    if not isinstance(value, str):
        raise ValueError(f"frame_analysis_policy_{field}_invalid")
    result = value.strip()
    if required and not result:
        raise ValueError(f"frame_analysis_policy_{field}_required")
    if len(result) > maximum:
        raise ValueError(f"frame_analysis_policy_{field}_too_long")
    return result


def _confidence(value: Any, field: str) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"frame_analysis_policy_{field}_invalid")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise ValueError(f"frame_analysis_policy_{field}_invalid")
    return round(result, 4)


def _string_list(value: Any, field: str, *, maximum: int = 32) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"frame_analysis_policy_{field}_invalid")
    result: list[str] = []
    for item in value[:maximum]:
        normalized = _text(item, field, maximum=160)
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _quality(value: Any) -> dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("frame_analysis_image_quality_invalid")
    usable = value.get("usable", True)
    visibility = value.get("visibility", "good")
    blur = value.get("blur", "none")
    occlusion = value.get("occlusion", "none")
    if not isinstance(usable, bool):
        raise ValueError("frame_analysis_image_quality_usable_invalid")
    if visibility not in {"good", "partial", "poor"}:
        raise ValueError("frame_analysis_image_quality_visibility_invalid")
    if blur not in {"none", "mild", "severe"}:
        raise ValueError("frame_analysis_image_quality_blur_invalid")
    if occlusion not in {"none", "partial", "heavy"}:
        raise ValueError("frame_analysis_image_quality_occlusion_invalid")
    return {
        "usable": usable,
        "visibility": visibility,
        "blur": blur,
        "occlusion": occlusion,
    }


def _severity(value: Any, field: str) -> str:
    result = "info" if value in (None, "") else value
    if result not in {"info", "attention", "critical"}:
        raise ValueError(f"frame_analysis_policy_{field}_severity_invalid")
    return str(result)


def _object_list(
    value: Any,
    field: str,
    text_fields: tuple[str, ...],
    *,
    with_severity: bool = False,
    extra_enums: dict[str, set[str]] | None = None,
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"frame_analysis_policy_{field}_invalid")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(value[:32]):
        if not isinstance(raw, dict):
            raise ValueError(f"frame_analysis_policy_{field}_invalid")
        item = {
            name: _text(raw.get(name), f"{field}_{index}_{name}")
            for name in text_fields
        }
        if extra_enums:
            for name, allowed in extra_enums.items():
                enum_value = raw.get(name, sorted(allowed)[0])
                if enum_value not in allowed:
                    raise ValueError(
                        f"frame_analysis_policy_{field}_{index}_{name}_invalid"
                    )
                item[name] = enum_value
        if with_severity:
            item["severity"] = _severity(raw.get("severity"), f"{field}_{index}")
        item["confidence"] = _confidence(
            raw.get("confidence"), f"{field}_{index}_confidence"
        )
        if any(item.get(name) for name in text_fields):
            result.append(item)
    return result


def _area(value: Any) -> dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("frame_analysis_area_invalid")
    return {
        "label": _text(value.get("label"), "area_label", maximum=160),
        "confidence": _confidence(value.get("confidence"), "area_confidence"),
    }


def normalize_policy_extra(analysis_policy_id: str, value: Any) -> dict[str, Any]:
    policy = reviewed_analysis_policy(analysis_policy_id)
    if not isinstance(value, dict):
        raise ValueError("frame_analysis_extra_must_be_object")
    common = {
        "schema_version": policy.output_schema_version,
        "image_quality": _quality(value.get("image_quality")),
        "limitations": _string_list(
            value.get("limitations"), "limitations", maximum=16
        ),
    }
    if analysis_policy_id == "site-observation":
        return {
            "schema_version": policy.output_schema_version,
            "area": _area(value.get("area")),
            "visible_assets": _object_list(
                value.get("visible_assets"),
                "visible_assets",
                ("label", "category", "state", "evidence"),
            ),
            "conditions": _object_list(
                value.get("conditions"),
                "conditions",
                ("subject", "observation", "evidence"),
                with_severity=True,
            ),
            "safety_observations": _object_list(
                value.get("safety_observations"),
                "safety_observations",
                ("label", "evidence"),
                with_severity=True,
            ),
            "visible_text": _object_list(
                value.get("visible_text"), "visible_text", ("text", "context")
            ),
            **common,
        }
    if analysis_policy_id == "asset-condition":
        return {
            "schema_version": policy.output_schema_version,
            "area": _area(value.get("area")),
            "assets": _object_list(
                value.get("assets"),
                "assets",
                ("label", "category", "identifier_text", "condition", "evidence"),
                with_severity=True,
            ),
            "inspection_observations": _object_list(
                value.get("inspection_observations"),
                "inspection_observations",
                ("subject", "observation", "evidence"),
                with_severity=True,
            ),
            **common,
        }
    if analysis_policy_id == "text-signage":
        return {
            "schema_version": policy.output_schema_version,
            "area": _area(value.get("area")),
            "text_regions": _object_list(
                value.get("text_regions"),
                "text_regions",
                ("text", "context", "language"),
                extra_enums={"legibility": {"clear", "partial", "unreadable"}},
            ),
            "signage": _object_list(
                value.get("signage"), "signage", ("label", "text", "evidence")
            ),
            **common,
        }
    return {
        "schema_version": policy.output_schema_version,
        "caption": _text(value.get("caption"), "caption", maximum=160, required=True),
        "scene_type": _text(value.get("scene_type"), "scene_type", maximum=160),
        "main_subjects": _string_list(value.get("main_subjects"), "main_subjects"),
        **common,
    }


def normalized_observation_items(
    analysis_policy_id: str,
    extra: dict[str, Any],
) -> list[dict[str, Any]]:
    """Flatten a validated purpose-specific payload into queryable DB rows."""

    normalized = normalize_policy_extra(analysis_policy_id, extra)
    items: list[dict[str, Any]] = []

    def add(
        kind: str,
        *,
        subject: str = "",
        category: str = "",
        observation: str = "",
        evidence: str = "",
        severity: str = "info",
        confidence: float = 0.0,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        items.append(
            {
                "item_kind": kind,
                "subject": subject,
                "category": category,
                "observation": observation,
                "evidence": evidence,
                "severity": severity,
                "confidence": confidence,
                "attributes": dict(attributes or {}),
            }
        )

    area = normalized.get("area")
    if isinstance(area, dict) and area.get("label"):
        add(
            "area",
            subject=str(area["label"]),
            confidence=float(area.get("confidence", 0.0)),
        )
    if analysis_policy_id == "site-observation":
        for item in normalized["visible_assets"]:
            add(
                "asset",
                subject=item["label"],
                category=item["category"],
                observation=item["state"],
                evidence=item["evidence"],
                confidence=item["confidence"],
            )
        for item in normalized["conditions"]:
            add(
                "condition",
                subject=item["subject"],
                observation=item["observation"],
                evidence=item["evidence"],
                severity=item["severity"],
                confidence=item["confidence"],
            )
        for item in normalized["safety_observations"]:
            add(
                "safety",
                subject=item["label"],
                evidence=item["evidence"],
                severity=item["severity"],
                confidence=item["confidence"],
            )
        for item in normalized["visible_text"]:
            add(
                "text",
                subject=item["text"],
                observation=item["context"],
                confidence=item["confidence"],
            )
    elif analysis_policy_id == "asset-condition":
        for item in normalized["assets"]:
            add(
                "asset",
                subject=item["label"],
                category=item["category"],
                observation=item["condition"],
                evidence=item["evidence"],
                severity=item["severity"],
                confidence=item["confidence"],
                attributes={"identifier_text": item["identifier_text"]},
            )
        for item in normalized["inspection_observations"]:
            add(
                "inspection",
                subject=item["subject"],
                observation=item["observation"],
                evidence=item["evidence"],
                severity=item["severity"],
                confidence=item["confidence"],
            )
    elif analysis_policy_id == "text-signage":
        for item in normalized["text_regions"]:
            add(
                "text",
                subject=item["text"],
                observation=item["context"],
                confidence=item["confidence"],
                attributes={
                    "language": item["language"],
                    "legibility": item["legibility"],
                },
            )
        for item in normalized["signage"]:
            add(
                "signage",
                subject=item["label"],
                observation=item["text"],
                evidence=item["evidence"],
                confidence=item["confidence"],
            )
    else:
        add("caption", observation=normalized["caption"])
        if normalized.get("scene_type"):
            add("area", subject=str(normalized["scene_type"]))
        for subject in normalized["main_subjects"]:
            add("subject", subject=subject)
    for limitation in normalized["limitations"]:
        add("limitation", observation=limitation)
    add("image_quality", attributes=normalized["image_quality"])
    return items
