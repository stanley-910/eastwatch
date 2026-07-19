# Handoff: fleet-resume — interactive chat on parked workers

Companion to `handoff-tmux-observability.md` (same vault twin, built and
verified there first). One work item: a command + TUI key that opens a live
interactive chat on any *parked* worker conversation.

## Story

> Worker parked issue #62 with a question in its comment. I want to ask one
> clarifying thing back — not re-queue the whole bounce loop. Today: open
> state.json, hand-copy the session path, cd into the repo, remember the pi
> resume flag, run it in some pane I'll lose. By the time I'm in, I've
> stopped caring.

## Why

Every parked conversation already carries its resume handle in the row schema
(`session`). The bounce loop is the right channel for *dispatching* work, but
humans also want to just talk to a parked worker — ask why it chose X, probe
before approving. That's one command away if something joins rows → picker →
`pi --session` in a tmux window. The vault board built it and it holds up.

## Decision

- Ship `fleet-resume` beside the watcher. No args → picker (fzf, else numbered
  menu) over fleet-status rows with `session != "" && !tmux_alive`; an arg
  fragment-matches key/slug. Flags: `-p` split pane, `-n` dry-run (print cmd).
- Open the chat in `tmux new-window -n "chat-<key>" -c <repo-root>`; outside
  tmux, exec inline in the current terminal.
- Pick the client by session shape: uuid → `claude --resume <sid> --model
  <row.model>`; anything else → `pi --session <path|id>` (verified: pi's
  interactive resume flag).
- Refuse live rows (`tmux_alive`) — a running worker is watched via
  `tmux attach`, never resumed a second time.
- TUI: bind `i` on the selected row → same script; inside tmux it opens the
  window without leaving the TUI, outside it suspends and returns on quit.
- Race caveat: close the chat before anything re-queues that conversation —
  the reconcile/bounce loop resumes the same session, and two processes on
  one session race the history.
- Interactive turns join the worker's thread (that's the point), but the
  comment loop still owns all GitLab writes — chat is for asking, not for
  replying on its behalf.

<details><summary>Agent detail</summary>

- Reference implementation (working, dry-run verified against 9 live rows):
  花园 `.claude/skills/dispatch/scripts/fleet-resume.sh`, commit `66d5a403`;
  TUI `i` key in `fleet-tui-prototype.py` same commit.
- Row selection filter: `jq -c '.rows[] | select(.session != "" and
  .tmux_alive == false)'` over `fleet-status --json`. Depends on the phase-2
  emitter from the tmux-observability handoff; if fleet-status isn't built
  yet, derive the same rows straight from state.json + `tmux has-session`
  as an interim.
- Window name sanitize (same as session names):
  `wname="chat-$(printf '%s' "$key" | tr -c 'a-zA-Z0-9_-' '-')"`,
  strip trailing `-`, cap ~40 chars.
- Session-shape dispatch: `case "$sid" in [0-9a-f]*-*-*-*-*) claude ;; *)
  pi ;; esac` — pi session paths/ids never look like a uuid; if that ever
  changes, add an explicit `client` field to the row schema instead of
  guessing harder.
- Compose commands with `printf %q` per argument (paths contain spaces).
- fzf line format: `[.derived, .model, .slug] | @tsv`, slug is field 3.
- Dry-run output shape (for tests): `[window] chat-<name>: <cmd>`;
  no-match exits 1.

</details>
