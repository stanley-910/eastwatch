#!/usr/bin/env bash
# deploy/headroom/launch.sh — container entrypoint.
# Ported from dotfiles headroom-pi-copilot (refresh mode only): extract the
# Copilot access/refresh tokens from the mounted pi auth.json, derive the
# upstream API host from the access token's proxy-ep, and exec headroom.
# Tokens stay in this process's environment — never logged.
set -euo pipefail

auth_json="${AUTH_JSON:-/auth/auth.json}"
host="${HEADROOM_HOST:-0.0.0.0}"
port="${HEADROOM_PORT:-8787}"

if [[ ! -f "$auth_json" ]]; then
  echo "error: pi auth file not found: $auth_json" >&2
  exit 1
fi

eval "$(HEADROOM_COPILOT_UPSTREAM="${HEADROOM_COPILOT_UPSTREAM:-}" python3 - "$auth_json" <<'PY'
import json, os, pathlib, re, shlex, sys

auth = json.loads(pathlib.Path(sys.argv[1]).read_text())
try:
    copilot = auth["github-copilot"]
except KeyError:
    raise SystemExit("missing github-copilot entry in auth.json")

access = str(copilot.get("access") or "")
refresh = str(copilot.get("refresh") or "")
forced = os.environ.get("HEADROOM_COPILOT_UPSTREAM", "").strip()
if forced:
    upstream = forced.rstrip("/")
else:
    match = re.search(r"proxy-ep=([^;]+)", access)
    hostname = match.group(1) if match else "api.individual.githubcopilot.com"
    if hostname.startswith("proxy."):
        hostname = "api." + hostname[len("proxy."):]
    upstream = "https://" + hostname

for key, value in {"COPILOT_REFRESH": refresh, "UPSTREAM": upstream}.items():
    print(f"{key}={shlex.quote(value)}")
PY
)"

if [[ -z "${COPILOT_REFRESH:-}" ]]; then
  echo "error: auth.json has no github-copilot refresh token" >&2
  exit 1
fi

echo "headroom upstream: $UPSTREAM, listening on $host:$port"

export GITHUB_COPILOT_API_URL="$UPSTREAM"
export OPENAI_TARGET_API_URL="$UPSTREAM"
export HEADROOM_TELEMETRY="${HEADROOM_TELEMETRY:-off}"
export GITHUB_COPILOT_GITHUB_TOKEN="$COPILOT_REFRESH"
export GITHUB_COPILOT_USE_TOKEN_EXCHANGE=1

exec headroom proxy \
  --host "$host" \
  --port "$port" \
  --mode token \
  --no-subscription-tracking \
  --no-rate-limit \
  --lossless
