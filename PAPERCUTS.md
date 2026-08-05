# Papercuts

Small frictions hit while working here — dead-end tool calls, broken links,
confusing setup steps, flaky commands, misleading errors, non-obvious gotchas.
Not blocking; logged so this repo can be sanded down. Distinct from tracked
bugs and from a work log.

Append with `papercut "<what got in the way>"`. Check off (`- [x]`) or delete
entries as they're fixed.

## Open

- [ ] 2026-08-04 — glab-board setup on GitHub prints 3 'manual:' steps that read like optional polish, but 'enable workflow Item-added -> Status=Triage' is load-bearing: gh_set_status fences on it and dies with 'refusing to write' for any issue not already on the board, so 'glab-board ready <n>' hard-fails on freshly created issues. Setup should mark that step required, or offer to fence-skip. · _claude-opus-5_
- [ ] 2026-08-04 — Handoff doc said to run 'the bundled glab-board setup from this checkout' but glab-board is not tracked in the eastwatch repo -- it lives in ~/dotfiles/agents/.agents/skills/forge/scripts/. Cost a few minutes hunting for a script that was never there. · _claude-opus-5_
