"""Reviewed local VLM catalog.

Model identifiers are provenance identifiers, never network locations at
runtime.  Weights are installed explicitly below ``.external/models`` and the
worker always opens them with ``local_files_only=True``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReviewedVlm:
    model_id: str
    revision: str
    local_directory: str
    storage_directory: str
    license_id: str
    parameter_scale: str
    approximate_download_gib: float
    minimum_vram_gib: int
    recommended_vram_gib: int
    quality_tier: str


@dataclass(frozen=True)
class VramRecommendation:
    vram_gib: int
    model_id: str
    label: str
    note: str


QWEN3_VL_2B = ReviewedVlm(
    model_id="Qwen/Qwen3-VL-2B-Instruct",
    revision="89644892e4d85e24eaac8bacfd4f463576704203",
    local_directory=".external/models/qwen3-vl-2b-instruct",
    storage_directory="qwen3-vl-2b-instruct",
    license_id="Apache-2.0",
    parameter_scale="2B",
    approximate_download_gib=4.0,
    minimum_vram_gib=6,
    recommended_vram_gib=8,
    quality_tier="省メモリ",
)


QWEN3_VL_4B = ReviewedVlm(
    model_id="Qwen/Qwen3-VL-4B-Instruct",
    revision="ebb281ec70b05090aa6165b016eac8ec08e71b17",
    local_directory=".external/models/qwen3-vl-4b-instruct",
    storage_directory="qwen3-vl-4b-instruct",
    license_id="Apache-2.0",
    parameter_scale="4B",
    approximate_download_gib=8.3,
    minimum_vram_gib=10,
    recommended_vram_gib=12,
    quality_tier="標準",
)


QWEN3_VL_8B = ReviewedVlm(
    model_id="Qwen/Qwen3-VL-8B-Instruct",
    revision="0c351dd01ed87e9c1b53cbc748cba10e6187ff3b",
    local_directory=".external/models/local-vlm",
    storage_directory="local-vlm",
    license_id="Apache-2.0",
    parameter_scale="8B",
    approximate_download_gib=16.4,
    minimum_vram_gib=20,
    recommended_vram_gib=24,
    quality_tier="高精度",
)

DEFAULT_VLM = QWEN3_VL_8B
REVIEWED_VLMS = {
    model.model_id: model
    for model in (QWEN3_VL_2B, QWEN3_VL_4B, QWEN3_VL_8B)
}

# These smaller-GPU recommendations remain conservative operational targets,
# not vendor guarantees.  Phase 9 measures every model on the accepted RTX
# 3090 host; 8/12/16 GiB hardware still needs its own acceptance evidence.
# Image resolution, CUDA/driver versions and allocator fragmentation can
# change peak usage.
VRAM_RECOMMENDATIONS = (
    VramRecommendation(
        vram_gib=8,
        model_id=QWEN3_VL_2B.model_id,
        label="8 GB: 2B（省メモリ）",
        note="8 GB GPUでは2Bを推奨します。速度と安定性を優先する構成です。",
    ),
    VramRecommendation(
        vram_gib=12,
        model_id=QWEN3_VL_4B.model_id,
        label="12 GB: 4B（標準）",
        note="12 GB GPUでは4Bを推奨します。高解像度入力で不足する場合は2Bへ戻してください。",
    ),
    VramRecommendation(
        vram_gib=16,
        model_id=QWEN3_VL_4B.model_id,
        label="16 GB: 4B（標準・余裕あり）",
        note="16 GB GPUでは4Bを余裕を持って実行できます。",
    ),
    VramRecommendation(
        vram_gib=24,
        model_id=QWEN3_VL_8B.model_id,
        label="24 GB以上: 8B（高精度）",
        note="24 GB以上では8Bを推奨します。精度を優先する構成です。",
    ),
)


def reviewed_vlm(model_id: str) -> ReviewedVlm:
    try:
        return REVIEWED_VLMS[model_id]
    except KeyError as error:
        raise ValueError(f"frame_analysis_model_not_reviewed:{model_id}") from error


def reviewed_vlm_directory(repo_root: Path, model_id: str) -> Path:
    """Resolve a reviewed model to its local, writable model-store path.

    ``SPATIAL_LOCAL_VLM_MODEL`` remains a compatibility override for the
    current default model.  New deployments should mount one model store and
    set ``SPATIAL_VLM_MODEL_STORE`` so future reviewed models can coexist.
    """

    model = reviewed_vlm(model_id)
    compatibility_override = os.environ.get("SPATIAL_LOCAL_VLM_MODEL")
    if compatibility_override and model.model_id == DEFAULT_VLM.model_id:
        path = Path(compatibility_override).expanduser()
    elif model_store := os.environ.get("SPATIAL_VLM_MODEL_STORE"):
        path = Path(model_store).expanduser() / model.storage_directory
    else:
        path = Path(model.local_directory).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve()
