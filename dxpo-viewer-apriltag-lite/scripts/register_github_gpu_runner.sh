#!/usr/bin/env bash
set -euo pipefail

# GitHub Actions runner release pinned on 2026-08-01. Update the version and
# checksum together after reviewing the official actions/runner release.
RUNNER_VERSION="2.336.0"
RUNNER_ARCHIVE="actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
RUNNER_URL="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${RUNNER_ARCHIVE}"
RUNNER_SHA256="04cf0be1aff4c3ec3554466c39124ca250e3effd8873bb7e8d68535aa9505d5d"

fail() {
  printf 'GPU runner registration failed: %s\n' "$1" >&2
  exit 2
}

repo_url="${GITHUB_RUNNER_URL:-}"
registration_token="${GITHUB_RUNNER_TOKEN:-}"
install_dir="${GITHUB_RUNNER_INSTALL_DIR:-${HOME}/.local/state/spatial-3dgs-actions-runner}"
runner_name="${GITHUB_RUNNER_NAME:-$(hostname)-spatial-3dgs-gpu}"
install_service="${GITHUB_RUNNER_INSTALL_SERVICE:-1}"

[[ -n "$repo_url" ]] || fail 'set GITHUB_RUNNER_URL to the GitHub repository URL'
[[ "$repo_url" == https://* ]] || fail 'GITHUB_RUNNER_URL must be an HTTPS URL'
[[ -n "$registration_token" ]] || fail 'set the short-lived GITHUB_RUNNER_TOKEN in this shell'
[[ "$(uname -m)" == "x86_64" ]] || fail 'the pinned runner archive supports Linux x86_64 only'
[[ "$install_service" == "0" || "$install_service" == "1" ]] || fail 'GITHUB_RUNNER_INSTALL_SERVICE must be 0 or 1'

command -v curl >/dev/null || fail 'curl is required'
command -v sha256sum >/dev/null || fail 'sha256sum is required'
command -v tar >/dev/null || fail 'tar is required'
command -v docker >/dev/null || fail 'Docker is required by the GPU verification workflow'
command -v nvidia-smi >/dev/null || fail 'nvidia-smi is required by the GPU verification workflow'
docker info >/dev/null 2>&1 || fail 'the current user cannot access the Docker daemon'
nvidia-smi >/dev/null 2>&1 || fail 'the NVIDIA GPU/driver is not available'

if [[ -e "$install_dir" ]] && [[ -n "$(find "$install_dir" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
  fail "install directory is not empty: $install_dir"
fi

temporary_dir="$(mktemp -d -t spatial-gpu-runner.XXXXXXXX)"
cleanup() {
  rm -rf -- "$temporary_dir"
}
trap cleanup EXIT

archive_path="$temporary_dir/$RUNNER_ARCHIVE"
printf 'Downloading GitHub Actions runner v%s...\n' "$RUNNER_VERSION"
curl -fsSL --retry 3 --output "$archive_path" "$RUNNER_URL"
printf '%s  %s\n' "$RUNNER_SHA256" "$archive_path" | sha256sum --check --status \
  || fail 'downloaded runner archive checksum does not match the pinned release'

mkdir -p -- "$install_dir"
tar -xzf "$archive_path" -C "$install_dir"

cd "$install_dir"
./config.sh \
  --url "$repo_url" \
  --token "$registration_token" \
  --name "$runner_name" \
  --labels gpu \
  --work _work \
  --unattended
unset registration_token GITHUB_RUNNER_TOKEN

if [[ "$install_service" == "1" ]]; then
  command -v sudo >/dev/null || fail 'sudo is required to install the runner service'
  runner_user="$(id -un)"
  sudo ./svc.sh install "$runner_user"
  sudo ./svc.sh start
  sudo ./svc.sh status
else
  printf 'Registration completed. Start interactively with: %s/run.sh\n' "$install_dir"
fi

printf 'Registered GPU runner %s for %s with label gpu.\n' "$runner_name" "$repo_url"
