#!/usr/bin/env bash
# deploy/host/onboard-workspace.sh — one-script teammate onboarding: build the
# workspace image (bakes forge + pi models, killing the stale-image papercut),
# create the workspace record, seed the pi models template, and print the
# paste-able teammate checklist.
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
usage: onboard-workspace.sh <workspace_id> <gitlab_username> <gitlab_user_id> \
    --projects '<JSON>' [--default-spec pi:gpt-5.6-sol:medium] \
    [--allowed-spec SPEC]... [--users-root DIR] [--checkout DIR] \
    [--database PATH]
USAGE
}

if [[ $(id -u) -ne 0 ]]; then
  printf 'onboard-workspace.sh must run as root (sudo): it creates homes and containers\n' >&2
  exit 2
fi

if [[ $# -lt 3 ]]; then
  usage
  exit 2
fi

workspace_id=$1; shift
gitlab_username=$1; shift
gitlab_user_id=$1; shift

projects=""
default_spec="pi:gpt-5.6-sol:medium"
allowed_specs=()
users_root="/srv/eastwatch/users"
checkout="/opt/eastwatch"
database="${BW_CONTROLLER_DB:-/srv/eastwatch/controller/data/controller.db}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --projects)
      projects=${2:?--projects requires a JSON value}
      shift 2
      ;;
    --default-spec)
      default_spec=${2:?--default-spec requires a value}
      shift 2
      ;;
    --allowed-spec)
      allowed_specs+=("${2:?--allowed-spec requires a value}")
      shift 2
      ;;
    --users-root)
      users_root=${2:?--users-root requires a value}
      shift 2
      ;;
    --checkout)
      checkout=${2:?--checkout requires a value}
      shift 2
      ;;
    --database)
      database=${2:?--database requires a value}
      shift 2
      ;;
    *)
      printf 'unknown argument: %s\n' "$1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ -z $projects ]]; then
  printf -- '--projects is required\n' >&2
  usage
  exit 2
fi

if [[ ${#allowed_specs[@]} -eq 0 ]]; then
  allowed_specs=("$default_spec")
fi

echo "==> [1/4] building workspace image from $checkout"
docker build -f "$checkout/deploy/workspace/Dockerfile" -t eastwatch-workspace:pilot "$checkout"

echo "==> [2/4] creating workspace $workspace_id for @$gitlab_username"
admin_args=(
  create-workspace "$workspace_id" "$gitlab_username" "$gitlab_user_id"
  --default-spec "$default_spec"
  --projects "$projects"
  --users-root "$users_root"
  --database "$database"
  --workspace-run-script "$checkout/deploy/workspace/run.sh"
)
for spec in "${allowed_specs[@]}"; do
  admin_args+=(--allowed-spec "$spec")
done
# Creation must run on the HOST: it makes home dirs and (re)creates the
# workspace container, neither of which the controller container can do.
# Routine DB writers still go through `docker exec bw-controller`.
bw_admin="$checkout/.venv/bin/bw-admin"
[[ -x $bw_admin ]] || bw_admin=/opt/eastwatch/.venv/bin/bw-admin
env BW_WORKSPACE_USERS_ROOT="$users_root" "$bw_admin" "${admin_args[@]}"
# Host-side SQLite writes leave root-owned WAL/SHM sidecars that silently
# freeze the container's writers — repair ownership immediately.
chown 10001:1000 "$database" "$database-wal" "$database-shm" 2>/dev/null || true
chmod 0664 "$database" "$database-wal" "$database-shm" 2>/dev/null || true

home="$users_root/$workspace_id/home"
models_dest="$home/.config/pi/agent/models.json"
echo "==> [3/4] installing pi models template at $models_dest"
install -d -o 1000 -g 1000 -m 0755 \
  "$home/.config" \
  "$home/.config/pi" \
  "$home/.config/pi/agent"
if [[ -f $models_dest ]]; then
  echo "notice: $models_dest already exists, skipping"
else
  install -o 1000 -g 1000 -m 0644 "$checkout/deploy/workspace/pi-models.json" "$models_dest"
fi

echo "==> [4/4] teammate checklist"
cat <<CHECKLIST

Paste this into the teammate's terminal, or hand it to them:

1. Enter the container:
   sudo docker exec -it bw-workspace-$workspace_id zsh -l

2. Log in to pi (the image sets PI_CODING_AGENT_DIR, so login lands in the
   worker config root automatically):
   pi
   (complete pi's /login flow now)

3. GitLab auth — NEVER run this bare, bare glab defaults to gitlab.com:
   glab auth login --hostname <host>

4. Git identity:
   git config --global user.name "<Name>"
   git config --global user.email "<email>"

5. Clone each configured repo:
   git clone <repo-url> ~/repos/<host>/<path>

6. Confirm readiness inside the workspace shell from step 1 — repeat until
   every check prints OK (this flips the workspace's ready flag):
   python -m eastwatch.runner.doctor
   (bw doctor is the laptop remote client; do not run it here.)

Mac side, once the container is dispatch-ready:

7. Write ~/.config/eastwatch/remote.yaml with its 5 required keys
   (ssh_target, ssh_alias, owner, server_command, editor) — point
   server_command at deploy/host/bw-server.

8. bw fleet

Personal skills sync — rootfs is read-only and there is no apt at runtime,
so only \$HOME-only installs survive; host_root for this workspace is $home:
   rsync -av ~/.agents/skills/ <ssh-target>:$home/.agents/skills/
CHECKLIST
