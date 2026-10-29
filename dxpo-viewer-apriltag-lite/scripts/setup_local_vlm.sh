#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
model_parent="$repo_root/.external/models"
model_size="${1:-8b}"
case "${model_size,,}" in
  2b)
    storage_directory="qwen3-vl-2b-instruct"
    model_id="Qwen/Qwen3-VL-2B-Instruct"
    revision="89644892e4d85e24eaac8bacfd4f463576704203"
    parameter_scale="2B"
    approximate_download="4.0 GiB"
    ;;
  4b)
    storage_directory="qwen3-vl-4b-instruct"
    model_id="Qwen/Qwen3-VL-4B-Instruct"
    revision="ebb281ec70b05090aa6165b016eac8ec08e71b17"
    parameter_scale="4B"
    approximate_download="8.3 GiB"
    ;;
  8b)
    storage_directory="local-vlm"
    model_id="Qwen/Qwen3-VL-8B-Instruct"
    revision="0c351dd01ed87e9c1b53cbc748cba10e6187ff3b"
    parameter_scale="8B"
    approximate_download="16.4 GiB"
    ;;
  *)
    printf 'Usage: %s [2b|4b|8b]\n' "$0" >&2
    exit 2
    ;;
esac
model_dir="$model_parent/$storage_directory"

mkdir -p "$model_dir"
if [[ ! -f "$model_dir/SPATIAL_MODEL_PROVENANCE.json" ]]; then
  if find "$model_dir" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    printf 'Resuming an incomplete reviewed model download in %s\n' "$model_dir"
  else
    printf 'Downloading %s at immutable revision %s (about %s)...\n' "$model_id" "$revision" "$approximate_download"
  fi

  if ! docker image inspect spatial-3dgs-system-generation:latest >/dev/null 2>&1; then
    docker compose -f "$repo_root/infra/compose/core.yaml" build generation
  fi

  docker run --rm \
    --user "$(id -u):$(id -g)" \
    -e HOME=/tmp \
    -e HF_HUB_DISABLE_TELEMETRY=1 \
    -e HF_HUB_ENABLE_HF_TRANSFER=0 \
    -e MODEL_ID="$model_id" \
    -e MODEL_REVISION="$revision" \
    -e MODEL_STORAGE_DIRECTORY="$storage_directory" \
    -v "$model_parent:/model-store" \
    spatial-3dgs-system-generation:latest \
    python3 -c 'import os; from huggingface_hub import snapshot_download; snapshot_download(repo_id=os.environ["MODEL_ID"], revision=os.environ["MODEL_REVISION"], local_dir="/model-store/" + os.environ["MODEL_STORAGE_DIRECTORY"])'

  cat > "$model_dir/SPATIAL_MODEL_PROVENANCE.json" <<EOF
{
  "license": "Apache-2.0",
  "model_id": "$model_id",
  "parameter_scale": "$parameter_scale",
  "revision": "$revision",
  "source": "https://huggingface.co/$model_id/tree/$revision"
}
EOF
fi

cd "$repo_root"
python3 scripts/verify_local_vlm.py "$model_dir" --model-id "$model_id"
printf 'Reviewed local VLM is ready: %s\n' "$model_dir"
