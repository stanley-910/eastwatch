# eastwatch runbook

A reconciling poller: every ~15s launchd runs one `eastwatch` cycle, which reads
GitLab board gestures (comments, trigger labels, ✅ awards), collects detached
worker results from prior cycles, dispatches new work up to global capacity, and
exits. Long-running `claude`/`pi` sessions run in detached worker-wrapper
processes; replies are posted back to the issue by a later reconcile cycle as
comments authored by the project bot.

## Install / start

```sh
git clone https://github.com/stanley-910/eastwatch.git ~/Developer/eastwatch
cd ~/Developer/eastwatch && ./install.sh
```

`install.sh` is idempotent: creates dirs, seeds `~/.config/eastwatch/config.yaml`
from the example (first run only), renders `com.stanwang.eastwatch.plist.example`
with the current repo and home paths, and (re)loads the launchd agent
`com.stanwang.eastwatch` (StartInterval 15, RunAtLoad). The rendered personal
plist lives only under `~/Library/LaunchAgents/` and is not tracked.
When upgrading to the detached-worker version, re-run `./install.sh`; `git pull`
alone does not apply the plist's `AbandonProcessGroup` setting to launchd.

When upgrading from board-watcher, the installer stops the old job, moves its
config and state into the Eastwatch directories, leaves one-release symlinks at
the old paths, removes the old plist, and starts only the Eastwatch job. It
refuses to move state while a detached worker run is active, and fails instead
of merging when both old and new directories already contain data.

Prereqs: `uv` on the plist PATH; `claude`/`pi` available after `~/.zshenv`
exports PATH. Worker-wrapper and provider processes refresh PATH through `/bin/zsh -lc`
before launching agents from launchd. Bot token in the macOS keychain (see Token rotation).

## Status / stop / start / uninstall

```sh
# status: is the job loaded + when did the last cycle run?
launchctl print gui/$(id -u)/com.stanwang.eastwatch | grep -E "state|last exit"; tail -5 ~/.local/state/eastwatch/logs/watcher.log

# stop (until next install.sh / boot)
launchctl bootout gui/$(id -u)/com.stanwang.eastwatch

# start again (after a stop)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.stanwang.eastwatch.plist

# force a cycle right now (job must be loaded)
launchctl kickstart gui/$(id -u)/com.stanwang.eastwatch

# run one cycle manually (same thing launchd does)
~/Developer/eastwatch/eastwatch

# uninstall completely
launchctl bootout gui/$(id -u)/com.stanwang.eastwatch
rm ~/Library/LaunchAgents/com.stanwang.eastwatch.plist
rm -rf ~/.local/state/eastwatch ~/.config/eastwatch   # state+config; convo dirs live under state
test ! -L ~/.local/state/board-watcher || rm ~/.local/state/board-watcher
test ! -L ~/.config/board-watcher || rm ~/.config/board-watcher
```

## Locations

| What | Where |
|---|---|
| Config | `~/.config/eastwatch/config.yaml` (token is NOT here) |
| State (authoritative) | `~/.local/state/eastwatch/state.json` |
| State backup | `~/.local/state/eastwatch/state.json.bak` (previous generation after each successful save) |
| Logs | `~/.local/state/eastwatch/logs/watcher.log` (+ `launchd.{out,err}.log`) |
| Per-conversation cwd + pi session files | `~/.local/state/eastwatch/convos/<project>-<iid>/` |
| Per-run artifacts | `~/.local/state/eastwatch/convos/<project>-<iid>/runs/<run-id>/{request.json,result.json,error.json,run.jsonl,wrapper.pid,child.pid,stderr.log}`; optional raw capture is `stream.log.gz` |
| Locks | `~/.local/state/eastwatch/{cycle,pi}.lock` |

Path overrides, mainly for tests and one-off dry runs: set `EASTWATCH_STATE_DIR`,
`EASTWATCH_CONFIG_DIR`, `EASTWATCH_CONFIG_PATH`, or
`EASTWATCH_LOG_DIR` before invoking `eastwatch`. The former `BOARD_WATCHER_*`
names remain fallback aliases for one release. Tests import the packaged module
from `src/` with temp state paths; never exercise save paths against
`~/.local/state/eastwatch`.

## Trigger vocabulary

| Gesture | Meaning |
|---|---|
| `agent::ready` label on an issue | launch implement session (reconcile: label present + unclaimed = work) |
| `agent::ready-research` label | launch research session; findings post back as a comment |
| Owner reply inside a bot-authored issue discussion **with a conversation in state** | resumes that session with the comment text — no syntax needed |
| Owner reply inside a bot-authored discussion on a mapped merge request | resumes the issue conversation that opened the MR; diff-note resumes include `path:line` context |
| Owner top-level/new-thread comment on an existing conversation | ignored unless it starts with `@agent`; closed issues/MRs also ignore plain comments. The `@agent` prefix is watcher protocol and is stripped before the worker sees the resume text. |
| Comment-triggered answers | post to the surface (issue or MR thread) where the triggering comment was made |
| Owner comment starting `@agent …` on any watched issue | launches a fresh Q&A session; answer posts in-thread |
| ✅ (or 👍) award **on a parked question or a done answer** | resume with "approved — proceed with your recommendation" |
| Reply comment on a parked question | resume with your words (overrides the default) |
| `[provider:model(:effort)]` in trigger comment, issue body, or owner resume comment | model hint/override, e.g. `[pi:gpt-5.6-sol:medium]`, `[claude:opus:max]`; resume-comment hints apply before the next dispatch. Switching provider starts a fresh provider session in the same conversation cwd. |

Labels the watcher manages: `agent::working` while a detached run is active; it
can legitimately persist across cycles. On a terminal state the watcher retires
the trigger label (`agent::ready`/`agent::ready-research`) and adds
`agent::for-human` — your review queue. `agent::parked` additionally marks "the agent
asked you a question" (vs. done = answer ready). Any resume gesture removes
`agent::for-human` at dispatch; **re-adding a retired trigger label means "run it
again"**. Every bot comment ends with a
resume footer (`claude --resume <sid> — cwd <dir>` or `pi --session <path>`) so you
can jump into any thread by hand.

Multiple comments landing between cycles coalesce into ONE resume. Comments that
land while a detached run is active stay queued for the next dispatch after that
run is collected. Pi expands `@...` file references only for argv message tokens
that begin with `@`; an `@token` in the middle of the single prompt argument is
not expanded. The watcher still strips the protocol `@agent` prefix at enqueue
and prefixes a defensive space if a generated pi prompt would otherwise begin
with `@`. Max 3 conversations work concurrently by default (config
`concurrency_cap`, enforced globally across all configured projects and cycles);
`pi` and `claude` share that scheduler capacity. `pi` launch windows are still
serialized with `pi.lock` until the child is past auth (session file / first
output, capped at ~30s), and the defensive auth-race retry remains in place.

## Bootstrap / adoption semantics

On the FIRST cycle for a project (no state yet), everything already on the board is
**adopted, not dispatched**: the comment cursor jumps to now, existing trigger-label
add-events are recorded as consumed (logged as `bootstrap: adopted ...`), and the
emoji snapshot is taken silently. To trigger work on a pre-existing labeled issue,
**remove and re-add the trigger label** — re-adding always re-triggers, including
after a conversation completes.

## Adding a repo

1. Create a project access token (Developer, `api` scope) on the new project;
   note its bot username/user-id (`GET /user` with that token).
2. Either reuse the same keychain item (one token can't span projects — so
   usually: add a second keychain item and a second `keychain:`-style entry is NOT
   supported in v1; simplest is one watcher config per token-compatible project
   group) — v1 assumes all configured projects accept the one keychain token.
3. Append the project to `projects:` in `~/.config/eastwatch/config.yaml`
   (host, path, numeric id, bot username/id, triggers, `local_checkout`,
   optional `worker_briefing`).
4. Create the labels on the project: `agent::ready`, `agent::ready-research`,
   `agent::for-human`, `agent::working`, `agent::parked`.
   Project access tokens are per-project: mint one for the new repo, store it
   under its own keychain entry, and set `keychain: {service, account}` in the
   project block.
5. Next cycle bootstraps it (adopt-only) automatically.

## Worker workspace & context

Before the first implementation/research launch, the watcher runs Forge's
`glab-board start <iid> ... --json` from the configured machine-local checkout.
Forge creates or resumes the issue worktree, claims the issue, and records its
link; the watcher then launches the worker with the returned worktree as its
actual cwd. This avoids Pi's fixed-cwd trap and keeps the shared checkout
untouched. The prompt tells implementation workers to read AGENTS.md/CLAUDE.md
and, after a normal commit, use the single `glab-board finish <iid>` command to
verify, push, create a ready MR, verify its source/target/change state, and
record it. Workers can pass `--description-file <path>` with a concise summary
and verification notes; otherwise Forge derives the body from commit subjects
and the verifier it ran. Research workers report findings without an MR unless
requested.

If automatic Forge onboarding fails, the worker remains in the shared checkout
but is explicitly told not to edit it; the prompt includes the error and exact
`start` recovery command. On a fresh machine where `local_checkout` does not
exist, the worker is told to clone it and park if that fails. Launch prompts
also include the issue's existing comment thread (last 20 non-system notes) and
linked issues, and the worker env gets `GITLAB_HOST=<host>`. Override the Forge
script for testing or a nonstandard install with `EASTWATCH_GLAB_BOARD`.
`worker_briefing` remains available for machine-specific instructions that do
not belong in repository guidance.

## Pi GPT routing through Headroom

Model specs exposed by config, trigger hints, state, and fleet output remain
`pi:<model>[:effort]`. The backend route is an operational detail selected by
eastwatch:

| Spec family | First route | Fallback |
|---|---|---|
| `pi:gpt-*[:effort]` | Pi provider `headroom-copilot` | same model and effort through `github-copilot` |
| `pi:gemini-*[:effort]` | `github-copilot` | none |
| `pi:claude-*[:effort]` | `github-copilot` | none |
| `claude:<model>[:effort]` | Claude CLI | unchanged; outside Pi/Headroom routing |

The GPT rule is the literal `gpt-` model prefix. Board-watcher executes `pi`
directly; it does not invoke the interactive `hpi` shell wrapper. It also does
not install, start, stop, restart, or reconfigure Headroom.

### Prerequisites and authentication boundary

Before selecting a Pi GPT model:

- the Pi configuration discovered by the detached worker must register
  `headroom-copilot`, with its base URL pointing at the operator-managed local
  Headroom proxy;
- the same GPT model ID must be available from both `headroom-copilot` and
  `github-copilot`, or fallback cannot preserve the requested model;
- Headroom must already be running and managed outside eastwatch; and
- Pi must already have a valid interactive GitHub Copilot login.

Board-watcher resolves `XDG_CONFIG_HOME` and `PI_CODING_AGENT_DIR` in the
worker environment, defaulting XDG configuration to `$HOME/.config` and the Pi
directory under the resolved XDG path, then passes both explicitly into tmux.
The tmux login shell restores those values after startup files run, so an
existing tmux server or shell override cannot redirect a detached worker to a
different Pi configuration.

Headroom reuses Pi's stored GitHub Copilot OAuth credential and may keep an
exchanged access token in memory. Do not inspect, print, copy, or hand-edit the
credential store. A stale in-memory exchanged token can fail only the Headroom
route while direct Copilot continues to work; the external service operator
must restart Headroom to discard that token. A revoked or replaced shared
refresh credential can fail both routes until `pi` is run in a real interactive
terminal and Pi's `/login` flow completes. The operator must then restart the
externally managed Headroom service. Board-watcher itself does not need a
restart in either case.

Headroom and direct Copilot are two paths to the same GitHub Copilot
entitlement. They consume the same quota. Direct fallback does not bypass a
quota limit, subscription restriction, account outage, or model entitlement.

### Session-safe fallback

A failed Headroom attempt does not automatically replay the original request:

1. If no tool started and no Pi session was created or changed, direct Copilot
   retries a new request's original prompt in a fresh session. For a resumed
   request, it reuses the unchanged existing session and sends the original
   prompt once.
2. If Headroom created or changed a recoverable session, direct Copilot resumes
   that session with a continuation instruction. It inspects repository and
   session state before proceeding instead of repeating completed tool work.
3. If a tool started but there is no recoverable session, replay is blocked and
   the run is terminal. An operator must inspect the repository and artifacts
   before deciding how to continue.
4. Both attempts share the original run deadline. An exhausted timeout is
   terminal; fallback never grants a second timeout window.
5. If direct Copilot also fails, the terminal error preserves both provider
   failures as bounded provider-attributed messages with labeled tails from
   each non-empty stdout and stderr stream.

This protects against a down proxy and other Headroom-path failures when work
can be retried or resumed safely. It does not protect against a failure shared
by both routes, an unavailable model on both routes, exhausted quota, exhausted
time, or unrecoverable execution after a tool side effect.

### Provider evidence and diagnostics

The normal per-run directory remains
`convos/<project>-<iid>/runs/<run-id>/`. Inspect these retained artifacts:

| Artifact | Routing evidence |
|---|---|
| `request.json` | logical worker `provider`, `model`, `effort`, session paths, and `timeout_seconds` run budget |
| `stderr.log` | bounded 64 KiB tail including provider attempt/fallback markers and process stderr |
| `run.jsonl` | versioned lifecycle facts: provider attempts, session discovery, first tool start, terminal/guard actions, exit/timeout, and reply extraction |
| `result.json` | actual model/effort/provider, Pi session-file handle, and extracted MR/commit evidence |
| `error.json` | terminal failure class plus bounded, provider-attributed stdout/stderr tails from failed routes |
| `stream.log.gz` | debug-only raw provider stream when `EASTWATCH_RAW_CAPTURE=1`; removed after 14 days |

The fleet row continues to show the logical worker/model, not a replacement
user-facing model spec. Provider markers and `result.json` are authoritative
for the concrete GPT route.

Recovery by symptom:

| Symptom | Action |
|---|---|
| Proxy down | Let safe direct fallback finish the current run. Diagnose and restart Headroom through its external service owner; do not restart eastwatch just to restore the proxy. |
| Stale Headroom exchanged access token | Only the Headroom route may fail; direct Copilot may succeed. Have the external service operator restart Headroom to discard the in-memory token. |
| Revoked/replaced shared refresh credential | Both routes may fail until `pi` is run in a real interactive terminal and `/login` completes. Then have the external service operator restart Headroom. Never put credential material in logs or a shell transcript. |
| Model unavailable | Confirm the exact model is registered and entitled on both Pi providers. Select a common model; fallback never substitutes a different model or effort. |
| Copilot quota/subscription limit | Wait for quota recovery or resolve the entitlement. Switching routes does not create more quota. |
| Unrecoverable partial execution | Treat the run as terminal. Review `stderr.log`, `run.jsonl`, the Pi session, and repository changes; use `scripts/fleet-resume --dry-run <conversation>` before a deliberate manual resume. Do not blindly replay the issue prompt. |

### Isolated smoke test

**Never stop, reload, or reconfigure the production eastwatch service or the
managed Headroom service for this test. Do not target the production proxy on
port 8787.** The recipe below creates a detached worktree, copies Pi's
non-secret model configuration read-only into a temporary
`PI_CODING_AGENT_DIR`, starts a foreground Headroom process on an alternate
port, and calls Pi with tools and session persistence disabled. All
configuration edits stay in the temporary copy. It does not run or import
`watcher.py`, signal a running watcher or launchd job, or exercise
eastwatch's fallback logic.

Run it only from a shell that already inherits the same supported Pi-credential
bridge as the managed Headroom service. The recipe deliberately does not name,
copy, or print the credential source. Its temporary Headroom workspace and
stateless mode prevent it from using or changing the managed service's runtime
state.

```sh
set -eu

REPO="$(git rev-parse --show-toplevel)"
SOURCE_PI_DIR="${PI_CODING_AGENT_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/pi/agent}"

SMOKE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/eastwatch-headroom.XXXXXX")"
SMOKE_WORKTREE="$SMOKE_ROOT/worktree"
SMOKE_PI_DIR="$SMOKE_ROOT/pi-agent"
SMOKE_PORT=18789
SMOKE_PROXY_PID=

cleanup() {
  if [ -n "$SMOKE_PROXY_PID" ]; then
    kill "$SMOKE_PROXY_PID" 2>/dev/null || true
    wait "$SMOKE_PROXY_PID" 2>/dev/null || true
  fi
  git -C "$REPO" worktree remove --force "$SMOKE_WORKTREE" 2>/dev/null || true
  rm -rf "$SMOKE_ROOT"
}
trap cleanup EXIT INT TERM

git -C "$REPO" worktree add --detach "$SMOKE_WORKTREE" HEAD
mkdir -p "$SMOKE_PI_DIR"
cp "$SOURCE_PI_DIR/models.json" "$SMOKE_PI_DIR/models.json"

python3 - "$SMOKE_PORT" <<'PY'
import socket
import sys

with socket.socket() as sock:
    try:
        sock.bind(("127.0.0.1", int(sys.argv[1])))
    except OSError as exc:
        raise SystemExit(f"alternate smoke port is unavailable: {exc}")
PY

SMOKE_MODEL="$(python3 - "$SMOKE_PI_DIR/models.json" "$SMOKE_PORT" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
data = json.loads(path.read_text())
provider = data["providers"]["headroom-copilot"]
provider["baseUrl"] = f"http://127.0.0.1:{sys.argv[2]}/v1"
models = [model["id"] for model in provider["models"]
          if model["id"].startswith("gpt-")]
if not models:
    raise SystemExit("headroom-copilot has no gpt-* model")
path.write_text(json.dumps(data, indent=2) + "\n")
print(models[0])
PY
)"

HEADROOM_WORKSPACE_DIR="$SMOKE_ROOT/headroom" \
GITHUB_COPILOT_API_URL=https://api.githubcopilot.com \
GITHUB_COPILOT_USE_TOKEN_EXCHANGE=1 \
headroom proxy \
  --host 127.0.0.1 \
  --port "$SMOKE_PORT" \
  --openai-api-url https://api.githubcopilot.com \
  --stateless \
  >"$SMOKE_ROOT/headroom.log" 2>&1 &
SMOKE_PROXY_PID=$!

i=0
until curl -fsS "http://127.0.0.1:$SMOKE_PORT/livez" >/dev/null; do
  kill -0 "$SMOKE_PROXY_PID" 2>/dev/null || exit 1
  i=$((i + 1))
  [ "$i" -lt 30 ] || exit 1
  sleep 1
done
kill -0 "$SMOKE_PROXY_PID" 2>/dev/null

cd "$SMOKE_WORKTREE"
PI_CODING_AGENT_DIR="$SMOKE_PI_DIR" \
pi -p \
  --no-tools \
  --no-session \
  --no-extensions \
  --no-skills \
  --no-prompt-templates \
  --no-context-files \
  --provider headroom-copilot \
  --model "$SMOKE_MODEL" \
  --mode text \
  'Reply exactly: headroom-smoke-ok'
```

Success is a `headroom-smoke-ok` response and a zero exit. Cleanup kills only
the foreground PID created by the recipe and removes only the temporary
worktree/config. Run the repository unit tests separately to cover routing and
fallback behavior.

## Watching a worker (tmux)

Each worker runs in its own detached tmux session named `task-<project>-<iid>`
(the conversation's session-dir basename), so you can watch the agent think
turn-by-turn instead of guessing from a quiet log:

```sh
tmux ls                              # every live worker session
tmux attach -t =task-example-org-example-repo-73   # watch one (detach: C-b d)
```

The pane renders each turn readably (assistant text, `· thinking…`, `→ tool`,
`── done`). Provider events are projected into bounded collector state and
`run.jsonl`; they are not retained as a duplicate transcript. Set
`EASTWATCH_RAW_CAPTURE=1` only for temporary debugging (the capture is
compressed to `stream.log.gz` on close). Both providers stream — claude via
`--output-format stream-json`, pi via `--mode json`. A non-dead pane is also the liveness signal the reconcile loop uses:
**state says working ∧ no live pane or result artifact ⇒ crashed**, and the run
takes the normal resume path. Successful panes use tmux `remain-on-exit`, so
their output stays attachable until an operator deletes the finished row.

If tmux is not installed the watcher falls back to a bare detached process (no
pane to attach; bounded artifacts and pid-liveness still apply).

### Fleet view

`scripts/fleet-status` joins `state.json` + `tmux ls` + each `run.jsonl` last
fact into one row per active conversation. Pi trace previews follow the canonical
Pi session file rather than the provider event stream:

```sh
scripts/fleet-status            # human table: dot · key · derived-state · model
scripts/fleet-status --json     # the row schema a TUI renders
```

`status` is the source-of-truth conversation state; `derived`
(`working`/`finishing`/`finished`/`queued`/`crashed`/`parked-input`/
`parked-review`) is computed at read time and never stored. `finishing` means
the worker wrote a successful result and exited while the watcher is still
collecting or posting it. `finished` retains the completed pane and trace for
review; only an artifact-less exit is shown as `crashed`.

### Fleet console

```sh
cd ~/Developer/eastwatch
./fleet_tui.py
```

The first launch lets `uv` install Textual from the script's inline dependency
metadata. The console polls `scripts/fleet-status --json` every two seconds; a
slow or malformed status response leaves the last good snapshot visible and
marks the feed stale. `CYCLE Ns` is the age of `state.json`; it turns stale
after 180 seconds without a successful reconcile write.

Keys:

- `↑` / `↓` or `j` / `k` — select a conversation
- `a` — attach to a live or retained-finished tmux session; detach with `C-b d`
- `i` — open interactive chat on a parked, crashed, or finished session
- `o` — open the selected GitLab issue or merge request
- `r` — refresh immediately
- `s`, then `s` again within five seconds — kill the selected worker session
- `x` — delete a finished row and its retained tmux pane; run logs stay on disk
- `[` — widen the live trace; `]` — narrow it
- after focusing the trace, `Home` / `Page Up` scroll through history and `End`
  returns to the live tail
- `?` — show key help; `q` — quit

The fleet list and live trace sit side by side. The list drops model and age
columns as its pane narrows and never scrolls horizontally. Polls update stable
rows in place rather than rebuilding the table. Claude and pi logs are parsed
directly in Python; the selected trace is parsed from its first event and then
followed live, with no `tail | jq` child pipeline. Parsed traces and byte
positions are cached, so switching rows resumes from the last read position
without reparsing or duplicating history. Full parsed history is retained while
a worker is active and for 24 hours after it finishes; after that grace period
the in-memory cache is compacted to the latest 500 rendered lines and retained
until `x` explicitly deletes the finished row. The view follows new events only
while already at the bottom, preserving manual scrollback. Parked conversations
show their archived last-run trace because they no longer have a live
`current_run`. Finished conversations remain selectable and attachable until
`x` clears the archived row; press `i` instead to continue the same provider
session with a follow-up. The per-run files are deliberately preserved.
Text selection is bounded to the trace widget: drag within the right pane, then
press `Ctrl-C` to copy only that trace selection. On macOS the console also
writes the selection through `pbcopy`, so it is available to normal paste
shortcuts outside the TUI.

Standalone resumable-session chat:

```sh
scripts/fleet-resume                     # fzf picker, numbered fallback
scripts/fleet-resume issue-62            # key/identity/URL fragment
scripts/fleet-resume --pane issue-62     # split current tmux window
scripts/fleet-resume --dry-run issue-62  # print exact client command
```

Interactive chat writes to the same provider session history as the reconcile
loop. Close chat before posting or applying a gesture that re-queues that
conversation. The command refuses live workers and uses a per-session lock to
prevent two interactive clients; live work should be watched with `a`, not
resumed.

Headless UI verification:

```sh
uv run python -m unittest tests.test_fleet_tui
```

## Token rotation

```sh
security delete-generic-password -a eastwatch -s eastwatch-gitlab-bot 2>/dev/null
security add-generic-password -a eastwatch -s eastwatch-gitlab-bot -w '<NEW TOKEN>'
```

No restart needed — the token is read from the keychain every cycle. Never put the
token in config, logs, or the repo.

## Troubleshooting

- `another cycle is already running` — an overlapping reconcile cycle was skipped;
  detached workers are not blocked by this.
- Stopping the launchd job with `launchctl bootout` kills any watcher process that
  is mid-cycle. In the old synchronous-dispatch design this also killed the only
  in-memory copy of state changes made after the last save; detached workers make
  provider runs survive independently, but a reconcile cycle can still lose
  unsaved in-memory state if it is stopped before `state.json` is written. Check
  `state.json.bak` for the previous generation when recovering.
- Active runs are visible in `state.json` under `current_run` and in the per-run
  artifact directory. `result.json`/`error.json` are written atomically by the
  worker wrapper; the next cycle posts or marks failure. Daily retention removes
  successful tails after 7 days, failed tails and orphan runs after 30 days, and
  raw captures after 14 days; request/result/error/journal artifacts remain. Run
  `uv run --script watcher.py sweep` for an explicit sweep, including legacy runs.
- Failed, timed-out, or disappeared detached workers are healed automatically with
  a failure comment, `agent::for-human`, and `agent::working` removed. Reply in the
  agent's thread (or use `@agent`) to retry. A `launching` run with no pid is
  treated as an interrupted two-phase launch and its consumed messages are
  requeued.
- Orphan `task-*` tmux sessions after a hard crash are harmless: the next cycle
  reaps the run (session-gone-without-result ⇒ resume path) and a re-dispatch kills
  any stale same-named session before starting. Kill one by hand with
  `tmux kill-session -t =task-<...>` if you want it gone sooner.
- pi `No API key for provider: github-copilot` — auth race; the worker serializes
  only the launch/auth window and still retries 3x with 25s sleeps. For a
  persistent failure, run `pi` in a real interactive terminal and complete
  Pi's `/login` flow. Then have the external service operator restart Headroom
  so it consumes the renewed login; do not restart eastwatch.
- State is authoritative over labels; if labels drift, fix state or just re-trigger.
