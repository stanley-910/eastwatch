#!/usr/bin/env bash
# deploy/headroom/run.sh — start the shared Headroom proxy container.
# Pilot scope: serves the named workspace owner's Copilot credential
# (mounted read-only from their workspace home). No host ports.
set -euo pipefail

owner=${1:?usage: run.sh <workspace-id>}
if [[ ! $owner =~ ^[a-z0-9][a-z0-9_-]{1,31}$ ]]; then
  printf 'invalid workspace id: %s\n' "$owner" >&2
  exit 2
fi
users_root="${BW_WORKSPACE_USERS_ROOT:-/srv/eastwatch/users}"
auth="$users_root/$owner/home/.config/pi/agent/auth.json"
image=${BW_HEADROOM_IMAGE:-eastwatch-headroom:pilot}
container="bw-headroom"

if [[ ! -f $auth ]]; then
  printf 'missing pi auth.json: %s\n' "$auth" >&2
  exit 1
fi
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
  --pids-limit 512 \
  --tmpfs /tmp:rw,nosuid,nodev,size=256m \
  --volume "$auth:/auth/auth.json:ro" \
  "$image"
docker network connect --alias bw-headroom bw-internal "$container"
