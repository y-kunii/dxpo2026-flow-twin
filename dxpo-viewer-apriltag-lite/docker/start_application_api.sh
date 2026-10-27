#!/bin/sh
set -eu

if [ ! -S /var/run/docker.sock ]; then
  echo "application-api: /var/run/docker.sock is required for pinned COLMAP child containers" >&2
  exit 1
fi

app_uid="${LOCAL_UID:-1000}"
app_gid="${LOCAL_GID:-1000}"
socket_gid="$(stat -c '%g' /var/run/docker.sock)"

primary_group="$(getent group "$app_gid" | cut -d: -f1 || true)"
if [ -z "$primary_group" ]; then
  primary_group=spatial-app
  groupadd --gid "$app_gid" "$primary_group"
fi
app_user="$(getent passwd "$app_uid" | cut -d: -f1 || true)"
if [ -z "$app_user" ]; then
  app_user=spatial-app
  useradd --uid "$app_uid" --gid "$app_gid" --no-create-home --home-dir /tmp --shell /bin/sh "$app_user"
fi
socket_group="$(getent group "$socket_gid" | cut -d: -f1 || true)"
if [ -z "$socket_group" ]; then
  socket_group=spatial-docker
  groupadd --gid "$socket_gid" "$socket_group"
fi
usermod --append --groups "$socket_group" "$app_user"

host_workspace="$(docker inspect "$HOSTNAME" --format '{{range .Mounts}}{{if eq .Destination "/workspace"}}{{.Source}}{{end}}{{end}}')"
if [ -z "$host_workspace" ]; then
  echo "application-api: host source for /workspace bind was not found" >&2
  exit 1
fi
export SPATIAL_DOCKER_WORKSPACE_SOURCE="$host_workspace"
exec gosu "$app_user" "$@"
