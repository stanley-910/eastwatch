#!/bin/zsh
# Idempotent install: dirs, config, launchd agent. Safe to re-run.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
UID_N="$(id -u)"
LABEL="com.stanwang.eastwatch"
LEGACY_LABEL="com.stanwang.board-watcher"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LEGACY_PLIST="$HOME/Library/LaunchAgents/$LEGACY_LABEL.plist"
PLIST_TEMPLATE="$REPO/$LABEL.plist.example"
CONFIG_DIR="$HOME/.config/eastwatch"
LEGACY_CONFIG_DIR="$HOME/.config/board-watcher"
STATE_DIR="$HOME/.local/state/eastwatch"
LEGACY_STATE_DIR="$HOME/.local/state/board-watcher"

validate_directory_migration() {
  local legacy="$1"
  local current="$2"

  if [[ -L "$legacy" ]]; then
    if [[ "${legacy:A}" != "${current:A}" ]]; then
      echo "refusing to replace legacy symlink $legacy; expected target $current" >&2
      return 1
    fi
  elif [[ -e "$legacy" && -e "$current" ]]; then
    echo "refusing to merge divergent directories $legacy and $current" >&2
    return 1
  fi
}

migrate_directory() {
  local legacy="$1"
  local current="$2"
  local created_current=0

  validate_directory_migration "$legacy" "$current"
  if [[ ! -L "$legacy" && -e "$legacy" ]]; then
    mv "$legacy" "$current" || return 1
    if ! ln -s "$current" "$legacy"; then
      if ! mv "$current" "$legacy"; then
        echo "migration rollback failed; data remains at $current" >&2
      fi
      return 1
    fi
    return 0
  fi

  if [[ ! -e "$current" ]]; then
    mkdir -p "$current"
    created_current=1
  fi
  if [[ ! -e "$legacy" && ! -L "$legacy" ]] && ! ln -s "$current" "$legacy"; then
    (( created_current == 0 )) || rmdir "$current" 2>/dev/null || true
    return 1
  fi
}

ensure_no_active_runs() {
  local state_file="$LEGACY_STATE_DIR/state.json"
  if [[ ! -f "$state_file" ]]; then
    state_file="$STATE_DIR/state.json"
  fi
  [[ -f "$state_file" ]] || return 0

  uv run --project "$REPO" python - "$state_file" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

state = json.loads(Path(sys.argv[1]).read_text())
active = []
for project_key, project in state.get("projects", {}).items():
    for conversation_key, conversation in project.get("conversations", {}).items():
        if conversation.get("current_run"):
            active.append(f"{project_key}:{conversation_key}")
if active:
    print(
        "refusing to move state while worker runs are active: " + ", ".join(active),
        file=sys.stderr,
    )
    raise SystemExit(1)
PY
}

# Validate first so a path conflict cannot stop a currently healthy service.
validate_directory_migration "$LEGACY_CONFIG_DIR" "$CONFIG_DIR"
validate_directory_migration "$LEGACY_STATE_DIR" "$STATE_DIR"

RENDERED_PLIST="$(mktemp "${TMPDIR:-/tmp}/eastwatch-plist.XXXXXX")"
PLIST_BACKUP=""
BOOTSTRAP_ERR=""
if [[ -f "$PLIST" ]]; then
  PLIST_BACKUP="$(mktemp "${TMPDIR:-/tmp}/eastwatch-plist-backup.XXXXXX")"
  cp -p "$PLIST" "$PLIST_BACKUP"
fi

cleanup() {
  [[ -z "$RENDERED_PLIST" ]] || rm -f "$RENDERED_PLIST"
  [[ -z "$PLIST_BACKUP" ]] || rm -f "$PLIST_BACKUP"
  [[ -z "$BOOTSTRAP_ERR" ]] || rm -f "$BOOTSTRAP_ERR"
}
trap cleanup EXIT

STOPPED_JOBS=0
restore_stopped_job() {
  local result_code="$?"
  local restore_plist=""
  trap - ERR
  set +e

  if (( STOPPED_JOBS )); then
    if [[ -n "$PLIST_BACKUP" ]]; then
      cp -p "$PLIST_BACKUP" "$PLIST"
      restore_plist="$PLIST"
    elif [[ -f "$LEGACY_PLIST" ]]; then
      rm -f "$PLIST"
      restore_plist="$LEGACY_PLIST"
    fi
    if [[ -n "$restore_plist" ]]; then
      echo "install failed; restoring the previously installed launchd job" >&2
      launchctl bootstrap "gui/$UID_N" "$restore_plist" 2>/dev/null \
        || launchctl load -w "$restore_plist" 2>/dev/null \
        || echo "could not restore $restore_plist; load it manually" >&2
    fi
  fi
  exit "$result_code"
}
trap restore_stopped_job ERR

# Stop the old job before moving state so only one process can write it.
launchctl bootout "gui/$UID_N/$LEGACY_LABEL" 2>/dev/null || true
launchctl bootout "gui/$UID_N/$LABEL" 2>/dev/null || true
STOPPED_JOBS=1

ensure_no_active_runs
migrate_directory "$LEGACY_CONFIG_DIR" "$CONFIG_DIR"
migrate_directory "$LEGACY_STATE_DIR" "$STATE_DIR"
mkdir -p "$STATE_DIR/logs" "$STATE_DIR/convos"

if [[ ! -f "$CONFIG_DIR/config.yaml" ]]; then
  cp "$REPO/config.yaml.example" "$CONFIG_DIR/config.yaml"
  echo "installed default config to ~/.config/eastwatch/config.yaml — review it"
fi

sed \
  -e "s|__REPO__|$REPO|g" \
  -e "s|__HOME__|$HOME|g" \
  "$PLIST_TEMPLATE" > "$RENDERED_PLIST"
mv "$RENDERED_PLIST" "$PLIST"
RENDERED_PLIST=""

BOOTSTRAP_ERR="$(mktemp "${TMPDIR:-/tmp}/eastwatch-bootstrap.XXXXXX")"
if launchctl bootstrap "gui/$UID_N" "$PLIST" 2>"$BOOTSTRAP_ERR"; then
  echo "bootstrapped $LABEL"
else
  # bootout returns before launchd always finishes unregistering the old job.
  # Retry after that teardown window before using the legacy load command.
  sleep 1
  : > "$BOOTSTRAP_ERR"
  if launchctl bootstrap "gui/$UID_N" "$PLIST" 2>"$BOOTSTRAP_ERR"; then
    echo "bootstrapped $LABEL after retry"
  else
    echo "launchctl bootstrap failed after retry:" >&2
    cat "$BOOTSTRAP_ERR" >&2
    echo "falling back to load -w"
    launchctl load -w "$PLIST"
  fi
fi

rm -f "$LEGACY_PLIST"
trap - ERR
launchctl print "gui/$UID_N/$LABEL" | grep -E "state|pid" | head -3 || true
echo "installed. logs: ~/.local/state/eastwatch/logs/"
