# Vault provider (`forge: vault`)

**Status: ADOPTED 2026-07-19.** A third eastwatch forge that watches a local
Obsidian **TaskNotes** board instead of a remote issue tracker. It replaces the
vault's retired home-grown bash dispatcher (`dispatch.sh` + launchd
`watcher.sh`), so eastwatch is now the single board-watcher for GitLab, GitHub,
and the vault.

## Principle

Same as the [label contract](label-contract.md): **one vocabulary,
platform-native authority.** On the vault the authority is each task note's
frontmatter **`status:`** field — read and written directly on disk. There is no
label shadow (the vault has no labels) and Obsidian does **not** need to be
running; eastwatch parses and rewrites frontmatter itself, which is why it works
headless under launchd where the old `obsidian-cli` sweep could not.

The vault is structurally the GitHub Projects v2 provider with a local board:
a single-select status, a durable dispatch outbox for crash-exactly-once, and
adopt-only bootstrap. It reuses eastwatch's entire launch / RunJournal / heal /
sweep / state machinery unchanged.

## Lifecycle

Dragging a note between kanban columns *is* changing its `status:`. The watcher
owns every transition except the human's drag **into** `agent`.

| Vault `status:` | Meaning | Set by |
| --- | --- | --- |
| `open` | not claimed | human |
| `agent` | **dispatch trigger** — run this note | human (drag) |
| `in-progress` | a worker is running it | watcher |
| `needs-input` | parked (`STATUS: parked`) or a failed run | watcher |
| `review` | done (`STATUS: done`), awaiting the human | watcher |
| `done` / `archived` | closed | human |

A note dispatches when its status transitions **into `agent`** from any other
status — *unless* it is adopt-only (first cycle or first-seen) or blocked by an
unfinished `blockedBy` task. The worker's final message is written verbatim into
the note's **`## Result`** section; the worker never edits the note itself.

```
open ──drag──▶ agent ──watcher──▶ in-progress ──worker done──▶ review
                 ▲                      │
                 │                      └── worker parked ──▶ needs-input
              (re-drag = resume)   ◀───────────────────────────┘
```

## The write-back contract

- The **worker** returns a single final message ending in `STATUS: done` or
  `STATUS: parked`. It must not touch the note's frontmatter/`status:`, must not
  write `## Result`, must not run `/work-task` or any board tooling, and must not
  git-commit (the launch prompt says so explicitly).
- **eastwatch** appends the reply as `## Result`, stamps `session-id:` into the
  note (for resume), moves `status:` (guarded — a human drag mid-run is not
  clobbered), and — when `commit_results` is on — commits **only that one note**,
  one file per commit, serialized in the collect phase. Any *other* files the
  worker changed are left uncommitted for you to review.

## Resume

There is no comment thread. To resume a parked note, **drag it back to
`status: agent`**. The watcher keys conversations by note (a `sha1(relpath)[:12]`
item id), finds the existing conversation (which carries the claude
`session_id`), and continues with `claude --resume`. Because eastwatch stamps
`session-id:` into the note's frontmatter on completion, even a **renamed** note
(new item id) reattaches the same session. Answer a parked question by editing
the note — an `## Owner reply` heading is a good convention; the worker re-reads
the whole note on resume.

## Config

```yaml
- forge: vault
  host: local                  # synthetic; state key is `local/<path>`
  path: myvault                # synthetic ASCII slug (state key, session dir, tmux)
  vault_path: "~/Documents/MyVault"   # the real (possibly CJK) vault directory
  tasks_glob: inbox/tasks/*.md # notes with `tags: task` + a `status:`
  local_checkout: "~/Documents/MyVault"   # worker cwd = vault (loads AGENTS.md natively)
  triggers: [agent::ready]     # only trigger; maps to `status: agent`
  commit_results: true         # watcher commits each note it writes
  default_spec: "claude:sonnet"  # model for notes without their own `model:`
```

`host`/`path` are synthetic ASCII on purpose: `path` becomes the state key,
session-dir slug and tmux session name, so it must stay ASCII even when the vault
directory name is not. A note's own `model:` frontmatter overrides
`default_spec`.

## Design notes & edge cases

- **Surgical writes, never a YAML round-trip.** Reads use a `--- … ---` splitter
  + `yaml.safe_load`; writes are single-line regex substitutions + atomic
  same-dir `os.replace`. TaskNotes frontmatter (`reminders:`, offset durations,
  quoted wikilinks, `cssclasses`) would be reordered/corrupted by a
  `yaml.safe_dump`, so we never dump.
- **Generation key is `item|status`, not mtime** — so iCloud touching a file
  without a content change never looks like a gesture.
- **Adopt-only bootstrap** — on the first cycle every note (including a
  pre-existing `status: agent`) is recorded, none dispatched. Re-drag to fire.
- **iCloud write race** — same-dir temp + `os.replace` is the strongest
  atomicity available; a concurrent human edit during the sub-millisecond write
  window is the only exposure.
- **git contention** — only eastwatch commits, and only in the serial collect
  phase, so concurrent vault workers never race the index. Worker task-file
  changes stay uncommitted by design.

## Migration from the bash dispatcher

The old `<vault>/.agents/skills/dispatch/` scripts and the
`com.stanley.vault-dispatch` launchd agent are retired — **do not run both**,
they would double-dispatch (both launch `task-<slug>` tmux sessions). The
`99-toolbox/bases/tasks-agentic.base` kanban view and the interactive
`work-task` skill stay; eastwatch does not invoke `work-task`.
