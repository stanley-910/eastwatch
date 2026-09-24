#!/usr/bin/env bash
# deploy/controller/run.sh
set -euo pipefail

root="${BW_CONTROLLER_ROOT:-/srv/eastwatch/controller}"
image=${BW_CONTROLLER_IMAGE:-eastwatch-controller:pilot}
install -d -m 0755 "$root"
# data dir: setgid group-writable so SQLite WAL sidecars created by either
# side (controller uid 10001, host read-only clients in group 1000) stay
# writable by both; the container joins group 1000 below for the same reason.
install -d -m 2770 -o 10001 -g 1000 "$root/data"
install -d -m 2750 -o 10001 -g 1000 "$root/logs"
if [ -f "$root/data/controller.db" ]; then
  chown 10001:1000 "$root/data/controller.db"
  chmod 0664 "$root/data/controller.db"
fi
test -f "$root/config.yaml"
test -f "$root/controller.env"
# The controller runs as uid 10001 with supplemental gid 1000. Keep config
# secret-adjacent but group-readable; Docker itself consumes controller.env.
chown root:1000 "$root/config.yaml"
chmod 0640 "$root/config.yaml"
chmod 0600 "$root/controller.env"
docker network inspect bw-internal >/dev/null 2>&1 || docker network create --internal bw-internal >/dev/null
docker network inspect bw-controller-egress >/dev/null 2>&1 || docker network create bw-controller-egress >/dev/null
docker rm -f bw-controller >/dev/null 2>&1 || true
docker create \
  --name bw-controller \
  --hostname bw-controller-runtime \
  --network bw-controller-egress \
  --restart unless-stopped \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --group-add 1000 \
  --tmpfs /tmp:rw,nosuid,nodev,size=512m \
  --env-file "$root/controller.env" \
  --env EASTWATCH_CONFIG_PATH=/etc/eastwatch/config.yaml \
  --env EASTWATCH_LOG_DIR=/var/log/eastwatch \
  --volume "$root/config.yaml:/etc/eastwatch/config.yaml:ro" \
  --volume "$root/data:/var/lib/eastwatch" \
  --volume "$root/logs:/var/log/eastwatch" \
  "$image" >/dev/null
docker network connect \
  --alias bw-controller \
  --alias bw-controller-internal \
  bw-internal bw-controller
docker start bw-controller >/dev/null
