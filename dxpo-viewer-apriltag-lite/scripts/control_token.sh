#!/usr/bin/env bash

# Shared by every supported launcher. The token is kept below ignored runtime
# state, never printed, never embedded in browser JavaScript, and reused across
# restarts so separately restarted API/Web containers continue to agree.
spatial_ensure_control_token() {
  local repository_root="$1"
  if [[ -n "${SPATIAL_CONTROL_TOKEN:-}" ]]; then
    if (( ${#SPATIAL_CONTROL_TOKEN} < 32 )) || [[ "$SPATIAL_CONTROL_TOKEN" =~ [[:space:]] ]]; then
      printf 'SPATIAL_CONTROL_TOKEN must contain at least 32 non-whitespace characters.\n' >&2
      return 2
    fi
    export SPATIAL_CONTROL_TOKEN
    return 0
  fi

  local secret_directory="$repository_root/var/spatial/secrets"
  local secret_file="$secret_directory/control_api_token"
  local legacy_secret_file="$repository_root/data/work/application_preview/.secrets/control_api_token"
  umask 077
  mkdir -p -- "$secret_directory"
  if [[ ! -s "$secret_file" && -s "$legacy_secret_file" ]]; then
    cp -- "$legacy_secret_file" "$secret_file"
    chmod 0600 "$secret_file"
  fi
  if [[ ! -s "$secret_file" ]]; then
    python3 -c 'import secrets; print(secrets.token_urlsafe(48))' > "$secret_file"
  fi
  SPATIAL_CONTROL_TOKEN="$(<"$secret_file")"
  if (( ${#SPATIAL_CONTROL_TOKEN} < 32 )) || [[ "$SPATIAL_CONTROL_TOKEN" =~ [[:space:]] ]]; then
    printf 'Stored Spatial control token is invalid; remove %s and retry.\n' "$secret_file" >&2
    return 2
  fi
  export SPATIAL_CONTROL_TOKEN
}
