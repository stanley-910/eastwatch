# Handoff: tmux-hosted workers + fleet TUI

From the vault agent-board build (same author, life-side twin of this watcher).
Two work items: (1) move detached worker processes into named tmux sessions with
live streaming; (2) prototype a fleet TUI. Built to a shared row schema so the
vault TUI and this one converge and can be swapped.

## Story

> Worker's been on issue #62 for twenty minutes. Is it reasoning or wedged?
> `ps` says the wrapper pid is alive. `stdout_path` artifact: empty — output
> lands only when `communicate()` returns. Nothing to attach to, nothing to
> tail. Kill it and lose the run, or wait blind? I waited. It was wedged.

## Why

Workers run as `start_new_session=True` Popen wrappers with `stdout=PIPE`, so
provider output is buffered inside the pipe until exit or timeout — mid-run
observability is a pid file. The vault board hit the identical problem and
solved it: host each worker in a named tmux session and stream turn-by-turn
output through `tee`. A quiet artifact and a wedged worker stopped looking the
same; watching an agent think became `tmux attach`.

## Decision

- Host each worker in `tmux new-session -d -c <cwd> -s "task-<key>"` — the
  wrapper survives as the thing tmux runs; launchd reconcile loop unchanged.
- Stream, don't buffer — **and know that `--verbose` alone does NOT stream**:
  `-p` in text mode buffers until exit regardless of verbosity (the vault build
  shipped that mistake and got blank panes; verified against a live run).
  Realtime output requires `--output-format stream-json`. Verified pipeline:
  `claude -p --verbose --output-format stream-json ... 2>>run.err | tee -a stream.jsonl | jq --unbuffered -r "$RENDER"`
  — raw events land in the artifact, jq renders text/thinking/tool lines
  live in the pane. (pi: check its own streaming flag; same trap may apply.)
  Result parsing: the stream's final `type=="result"` event carries the run
  payload — parse `.result` from it, or keep the existing result artifacts
  and treat the stream as purely additive observability.
- Dedup guard: `tmux has-session -t "=task-<key>"` — replaces pid-liveness for
  "already running"; sessions self-exit on completion.
- Crash = state says running ∧ no tmux session → take the existing resume path.
- Humans: `tmux attach -t task-<key>` to watch live, `C-b d` to detach.
- Compose the shell string with `printf %q` per argument — quoting bugs here
  cost the vault build a debugging round.
- Expect quiet stretches mid-turn even with streaming: silence ≠ wedged; the
  tmux session existing is the liveness signal.

## Fleet TUI (phase 2)

First deliverable is `fleet-status --json`, not the TUI: one emitter joining
state.json + `tmux ls` + stream-log tails into one row per active conversation.
The TUI (Python Textual recommended — real mouse events, trivial subprocess
tails) is a dumb renderer of it: dot + key + model + derived state + last log
line per row, tail pane for the selected row, click/keys for attach · stop
(confirm) · force-cycle, footer showing reconcile heartbeat age. Out of v1:
replying to threads from the TUI — the comment loop owns writes.

fd-leak trap the vault prototype hit live (EMFILE while idle): a periodic
table rebuild re-fires the row-highlight event — guard it or every poll
respawns the tail pipeline; and killing a `sh -c 'tail -F | jq'` kills only
sh, orphaning the children. Run the pipeline in its own process group
(`start_new_session=True`), `killpg` + reap on row switch and on app exit.
Reference fix: 花园 fleet-tui-prototype.py commit `0ec63310`.

## The swap contract

Both TUIs render exactly this row; only the emitter differs:

```json
{
  "key":       "task-<slug-or-issue>",
  "surface":   "vault | gitlab",
  "status":    "<source-of-truth state: card status / conversation state>",
  "derived":   "queued | working | crashed | parked-input | parked-review",
  "model":     "<self-identifying, per fleet rule>",
  "session":   "<claude sid / pi session path>",
  "tmux_alive": true,
  "log":       "<stream.jsonl path — raw stream-json events>",
  "last_line": "<tail -1 of log>",
  "url":       "<issue/MR or obsidian:// link>"
}
```

`status` is truth from the source system; `tmux_alive` only disambiguates
working from crashed. Keep that split and the UIs stay swappable.

<details><summary>Agent detail</summary>

- Launch sites to convert: `watcher.py:1624`, `watcher.py:1711`, `watcher.py:2366`
  (`subprocess.Popen(..., start_new_session=True)`); wrapper entry
  `worker_main` at `watcher.py:1807` reads the request JSON and writes
  `wrapper_pid_path` — tmux hosts this wrapper, so pid artifacts keep working
  during migration; retire them for dedup only after `has-session` is trusted.
- Session naming: sanitize like the vault dispatcher —
  `tname="task-$(printf '%s' "$key" | tr -c 'a-zA-Z0-9_-' '-')"; tname="${tname%-}"`;
  guard with `tmux has-session -t "=$tname"` (the `=` forces exact match).
- Vault reference implementation (working, multiple live runs):
  dispatcher `花园/.claude/skills/dispatch/scripts/dispatch.sh`; launch line:
  `cmd=$(printf 'claude -p --verbose --output-format stream-json --session-id %q --model %q --permission-mode acceptEdits %q 2>>%q | tee -a %q | jq --unbuffered -r %q' ...)`
  then `tmux new-session -d -c "$PWD" -s "$tname" "$cmd"`.
- The working pane render filter (vault-verified):
  `jq --unbuffered -r 'if .type=="assistant" then (.message.content[]? | (if .type=="text" then .text elif .type=="thinking" then "· thinking…" elif .type=="tool_use" then "→ \(.name) \(.input.command // .input.file_path // "" | tostring | .[0:120])" else empty end)) elif .type=="result" then "── \(.subtype)" else empty end'`
  Reference: 花园 `.claude/skills/dispatch/scripts/dispatch.sh` (commit 4e63646a
  fixed the --verbose-doesn't-stream mistake; don't repeat it).
- stream.log per run dir beside existing artifacts; state gains nothing new —
  derived states are computed by fleet-status, never stored.
- Vault twin of the TUI spec: `花园/inbox/tasks/build-fleet-tui.md`; shared data
  contract recorded in `花園/04-code/projects/agent-board/agent-board.md`.
  Whichever side ships fleet-status first defines field order; the schema above
  is the agreed shape.

</details>
