#!/usr/bin/env bash
# deploy/workspace/run.sh
set -euo pipefail

owner=${1:?usage: run.sh <workspace-id>}
if [[ ! $owner =~ ^[a-z0-9][a-z0-9_-]{1,31}$ ]]; then
  printf 'invalid workspace id: %s\n' "$owner" >&2
  exit 2
fi
root="${BW_WORKSPACE_USERS_ROOT:-/srv/eastwatch/users}/$owner"
env_file="$root/runner.env"
container="bw-workspace-$owner"
image=${BW_WORKSPACE_IMAGE:-eastwatch-workspace:pilot}

install -d -m 0700 -o 1000 -g 1000 "$root/home"
if [[ ! -f $env_file ]]; then
  printf 'missing root-owned runner environment: %s\n' "$env_file" >&2
  exit 1
fi
chmod 0600 "$env_file"
docker network inspect bw-internal >/dev/null 2>&1 || docker network create --internal bw-internal >/dev/null
docker network inspect bw-egress >/dev/null 2>&1 || docker network create bw-egress >/dev/null
docker rm -f "$container" >/dev/null 2>&1 || true
docker run --detach \
  --name "$container" \
  --hostname "$container" \
  --network bw-egress \
  --restart unless-stopped \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 4096 \
  --tmpfs /tmp:rw,nosuid,nodev,size=4g \
  --env-file "$env_file" \
  --env EASTWATCH_GLAB_BOARD=/opt/eastwatch/skills/forge/scripts/glab-board \
  --volume "$root/home:/home/bw" \
  "$image"
docker network connect bw-internal "$container"
