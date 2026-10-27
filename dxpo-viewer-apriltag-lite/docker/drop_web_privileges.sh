#!/usr/bin/env sh

# A named node_modules volume can outlive the container image and retain files
# created by an older root process. Repair only that disposable dependency
# volume, then run the application as the host-matching unprivileged user.
spatial_drop_web_privileges() {
  if [ "$(id -u)" -ne 0 ]; then
    return
  fi

  app_uid="${LOCAL_UID:-1000}"
  app_gid="${LOCAL_GID:-1000}"
  node_modules=/workspace/web_viewer/app/node_modules
  mkdir -p "$node_modules"
  chown -R "$app_uid:$app_gid" "$node_modules"

  app_home="$(getent passwd "$app_uid" | cut -d: -f6 || true)"
  if [ -z "$app_home" ]; then
    app_home=/tmp
  fi
  export HOME="$app_home"
  exec gosu "$app_uid:$app_gid" "$0" "$@"
}
