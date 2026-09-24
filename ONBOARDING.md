# Eastwatch hosted mode: onboarding

This one document takes you from nothing to working with the hosted
eastwatch: an agent fleet that watches GitLab issue boards and does the
work. You read this, run the steps top to bottom, and at the end you can
assign real work to an agent from your own GitLab issues and review what
comes back. No other document is required.

## 1. What you are setting up

Eastwatch turns a GitLab issue board into a dispatch queue for coding
agents:

- You (or anyone authorized) put the `agent::ready` label on an issue.
- A controller on the server sees it within ~5 seconds and dispatches the
  issue to **your** workspace container, where a pi coding agent works it in
  a git worktree using **your** GitLab and model credentials.
- The agent opens a merge request authored as you, posts a completion note
  on the issue (with a collapsible run-stats footer: duration, tokens,
  cost), and sets a terminal label (`agent::mr-ready`, `agent::for-human`,
  `agent::failed`, `agent::parked`).
- From then on it is a conversation: comment `@agent <question>` on the
  issue or its MR, or just reply in any thread the agent has answered in —
  it resumes the *same session* with full memory of what it did, and
  answers in that thread.

The moving parts, all on one Linux server with Docker (called
`eastwatch-host` below):

| Container | Role |
|---|---|
| `bw-controller` | Polls every configured GitLab project with that project's token, owns one queue database, posts all bot comments/labels |
| `bw-workspace-<you>` | Your agent runtime: your creds, your repos, your sessions |
| `bw-headroom` | Model proxy for GPT-family models via Copilot |

Your laptop gets a thin `bw` CLI and a fleet TUI that talk to the server
over ssh. Nothing runs locally except those clients.

## 2. Prerequisites

Collect these before starting:

- **A GitLab account** on the GitLab host the board lives on (for example
  `gitlab.example.com`), with access to the project(s) the agent
  will work on. You need your **username** and your numeric **user id**
  (Profile → your avatar URL contains it, or run
  `glab api users?username=<you>` anywhere glab is authenticated).
- A **project access token** for each project the controller will watch:
  Developer role with `api` scope. Copy it when GitLab creates it; Part 3 reads
  it from stdin and stores it only in the server's mode-0600 controller env.
- **ssh access to the server** (for example `ssh admin@eastwatch-host`)
  and someone with sudo there — you, or an admin who runs Part 3 for you.
- **Model credentials** you can log in with inside the container:
  a GitHub Copilot login for GPT-family models (used via the headroom
  proxy), and/or whatever provider login your pi setup uses.
- On your laptop: `ssh`, `rsync`, and [uv](https://docs.astral.sh/uv/)
  (for the fleet TUI). macOS and Linux both work.

## 3. Admin step: provision your workspace and projects (~4 minutes)

Someone with sudo on the server runs exactly one command. Pick a short
workspace id (convention: your first name, lowercase):

```bash
sudo env BW_CONTROLLER_DB=/srv/eastwatch/controller/data/controller.db \
  bash /opt/eastwatch/deploy/host/onboard-workspace.sh \
  <workspace-id> <gitlab-username> <gitlab-user-id> \
  --projects '[{"host":"gitlab.example.com","path":"<group/project>","id":<project-id>}]' \
  --default-spec pi:gpt-5.6-sol:medium \
  --users-root /srv/eastwatch \
  --checkout /opt/eastwatch
```

`--projects` lists the project(s) your agent may work on (`id` is the
numeric GitLab project id, shown on the project's front page). Add
`--allowed-spec pi:<model>:<effort>` once per additional model you want to
be able to request; the default spec is always allowed.

The script echoes four steps: it rebuilds the workspace image from the
checkout (this bakes in the board tooling and the model-provider template),
creates your workspace record and container, installs a pi model
configuration into your new home, and finishes by printing the same
checklist as Part 4. When it exits, `docker ps` shows
`bw-workspace-<workspace-id>` running.

Route each project through its own project access token. The command validates
the token, discovers its bot username and numeric id, updates the shared
controller config and secret env, adds the project to this workspace's
allowlist, and recreates only the containers whose immutable environment
changed. The token never appears in argv, config YAML, or logs:

```bash
read -rsp 'Project access token: ' PROJECT_TOKEN; echo
printf '%s' "$PROJECT_TOKEN" | sudo \
  /opt/eastwatch/.venv/bin/bw-admin add-hosted-project \
  <workspace-id> gitlab.example.com <group/project> <project-id> \
  --token-stdin \
  --controller-root /srv/eastwatch/controller \
  --users-root /srv/eastwatch \
  --controller-run-script /opt/eastwatch/deploy/controller/run.sh \
  --workspace-run-script /opt/eastwatch/deploy/workspace/run.sh
unset PROJECT_TOKEN
```

Run that block once per project. Exact reruns are no-ops. Every project shares
the same `bw-controller`, `bw-workspace-<workspace-id>`, controller database,
Fleet endpoint, and laptop `remote.yaml`; only the GitLab credential is routed
per project. Your workspace is not dispatch-ready until you finish Part 4.

## 4. Your setup inside the container (~10 minutes, once)

Everything here happens inside your container. Enter it from the server:

```bash
sudo docker exec -it bw-workspace-<workspace-id> zsh -l
```

Two rules that explain most of what follows:

- **Everything you install or configure must live under `$HOME`** — the
  container's root filesystem is read-only (no `apt install`), but your
  home directory is durable: it survives container restarts, recreates,
  and image upgrades.
- **One known trap is called out in bold.** Read it before running the
  commands.

Steps, in order:

1. **Model login.**

   ```bash
   pi        # complete the /login flow inside pi, then exit pi
   ```

   The image sets `PI_CODING_AGENT_DIR=/home/bw/.config/pi/agent`, so login,
   workers, and `bw resume` all share one credential dir. (Images built
   before 2026-08-06 lacked this — there, `export
   PI_CODING_AGENT_DIR=~/.config/pi/agent` before running `pi`, or logins
   land in `~/.pi/agent` where nothing reads them and interactive sessions
   silently fall back to a default model.)

   A `models.json` was pre-installed into `~/.config/pi/agent/` for you: it
   defines the GPT-family models routed through the `bw-headroom` proxy.
   Leave it in place.

2. **GitLab CLI login.** **Trap: bare `glab auth login` targets
   gitlab.com and will silently authenticate against the wrong host.**

   ```bash
   glab auth login --hostname gitlab.example.com
   ```

   Use a personal access token with `api` + `write_repository` scope when
   prompted (GitLab → Preferences → Access Tokens).

3. **Git identity** (MRs are authored as you):

   ```bash
   git config --global user.name  "Your Name"
   git config --global user.email "you@example.com"
   ```

4. **Clone each configured project** to the path the runner expects —
   `~/repos/<host>/<group>/<project>`:

   ```bash
   git clone https://gitlab.example.com/<group>/<project>.git \
     ~/repos/gitlab.example.com/<group>/<project>
   ```

5. **Create the board labels and lanes** from each checkout. The admin command
   already ensures the GitLab project has an `Agent Board`; Forge fills it with
   the lifecycle labels and lists idempotently:

   ```bash
   cd ~/repos/gitlab.example.com/<group>/<project>
   /opt/eastwatch/skills/forge/scripts/glab-board setup
   ```

6. **Run the workspace doctor until green.** This checks every credential,
   path, and the controller connection — and flips your workspace's `ready`
   flag so the controller will dispatch to it:

   ```bash
   python -m eastwatch.runner.doctor
   ```

   Do not run `bw doctor` inside the container. `bw` is the laptop remote
   client and expects `~/.config/eastwatch/remote.yaml`.

   Repeat after fixing anything it flags. When every line prints OK, your
   workspace is live. Exit the container; you never need to enter it for
   normal work again (only for credential renewals).

## 5. Your laptop setup (~5 minutes, once)

1. An ssh config alias (used by the "open in editor" verb):

   ```
   # ~/.ssh/config
   Host eastwatch
       HostName eastwatch-host
       User admin
   ```

2. The `bw` client config — exactly five keys, all required:

   ```yaml
   # ~/.config/eastwatch/remote.yaml
   ssh_target: admin@eastwatch-host
   ssh_alias: eastwatch
   owner: <your-gitlab-username>
   server_command: /opt/eastwatch/deploy/host/bw-server
   editor: code
   ```

3. Get the repo and smoke-test (any checkout of this repository works):

   ```bash
   uv run bw fleet     # from the repo root; empty table = working, no runs yet
   ```

4. The live console — fleet table, per-run conversation traces, read-only
   attach (`a`) to running agents, and full interactive resume (`i`) into a
   finished agent's session:

   ```bash
   uv run fleet_tui.py
   ```

5. Optional, recommended — sync your personal agent skills into your
   workspace (repeat whenever they change; `bw home` prints your
   workspace's home directory path on the server):

   ```bash
   rsync -av ~/.agents/skills/ "$(uv run bw home | tr -d '\n')/.agents/skills/" \
     -e ssh --rsync-path="rsync" 2>/dev/null \
     || rsync -av ~/.agents/skills/ admin@eastwatch-host:"$(uv run bw home)/.agents/skills/"
   ```

## 6. Working with your agent — the actual workflow

**Dispatch.** Create an issue in a configured project describing the work
(scope, files, how to verify — the better the issue, the better the
result). Then add the label **`agent::ready`** (or **`agent::ready-research`**
for read-only investigation). That label transition is the trigger. You do
not need to assign the issue — an unassigned issue dispatches to whoever
promoted the label. Within seconds the label flips to `agent::working` and
a row appears in your fleet TUI.

**Result.** Minutes later the issue gets a completion note (expand its
`run stats` footer for duration/tokens/cost), a terminal label, and — for
implementation runs — a merge request authored by you, linked to the
issue.

**Conversation.** This is the core of the system; the agent resumes its
original session with full memory every time:

- `@agent <question>` as a comment on the issue — answered as a note.
- Ask inside a discussion thread — answered *in that thread*.
- After the agent has answered in a thread, drop the tag: plain replies in
  that thread reach it too.
- Comment on the **merge request** (including line-level diff comments in
  review threads): routed back to the conversation that authored the MR,
  answered on the MR. You can ask it to fix review findings, rebase, or
  explain itself — it has push access to its own branch.
- Comments sent **while a run is active** are not lost or rejected: they
  queue and fold into the very next run automatically.
- Re-applying `agent::ready` to an issue tells the agent to pick the work
  back up.

**Model choice.** Put a hint anywhere in the issue description or your
comment: `[pi:<model>:<effort>]`, e.g. `[pi:gpt-5.6-sol:medium]`. Use the
full effort name (`medium`, not `med`). It must be in your workspace's
allowed list (set at provisioning); a disallowed spec
is politely rejected with a note.

**Parked runs.** If the agent needs a decision it ends with
`STATUS: parked`, one concrete question, and its recommended default.
Answer it with a comment (✅-emoji approval is not wired up on the hosted
path yet — reply with words).

**Watching and driving from the terminal** (all owner-scoped to you):

```bash
uv run bw fleet                  # one-shot table (--json for scripts)
uv run bw logs <n> --follow      # run lifecycle; add --session for the
                                 #   agent's actual conversation transcript
uv run bw attach <n>             # read-only view of a RUNNING agent
uv run bw resume <n>             # full interactive chat in a FINISHED
                                 #   agent's session, in its worktree
uv run bw path <n> / open <n>    # worktree path / open it in your editor
uv run bw audit <n>              # what a cleaned-up run did
```

`<n>` is an issue number (`18`, `#18`), job id prefix, or anything else
unambiguous from the fleet table; numeric matches resolve to the newest run
on that issue.

## 7. Ground rules and troubleshooting

| Symptom | Cause / fix |
|---|---|
| `bw doctor` all green but runs fail on model auth | You logged into pi before exporting `PI_CODING_AGENT_DIR` (Part 4 trap 1). Re-export and log in again. |
| glab says you are logged in but pushes/API calls 404 | You authenticated against gitlab.com (Part 4 trap 2). Re-run with `--hostname`. |
| Issue with `agent::ready` never dispatches | Your workspace is not `ready` (run the runner doctor), the issue has **multiple** assignees (ambiguous — one or zero only), or the project was not added with `bw-admin add-hosted-project`. The first controller poll adopts existing labels; remove and re-add `agent::ready` once. |
| A rejection note appears instead of a run | The note says why: disallowed model spec, unauthorized commenter, or a wedged conversation awaiting operator cleanup. |
| `@agent` on a merge request gets silence | Only MRs created by the agent (they carry a source marker in the description) are routed; a hand-made MR's mention is recorded but not dispatched — comment on the issue instead. |
| Plain (un-tagged) reply ignored | Plain replies only work inside threads the agent has already answered in, and only while the issue/MR is open. Tag `@agent` to be explicit. |
| Something you installed vanished | It was outside `$HOME` (or in `/tmp`). Only your home survives restarts; system-level tools must be baked into the image by an admin. |
| Model needs you can't satisfy | The GPT-family route (headroom) currently carries one user's Copilot login. A second GPT user needs an admin to extend it; Anthropic-provider models via your own pi login are unaffected. |

**Security expectations**: your container holds *your* credentials and only
your home is mounted into it. The server is trusted-team territory — anyone
with shell on the host can read the mounted homes. Never commit or paste
tokens into issues, comments, or this repo.

That's the whole system. Create an issue, label it `agent::ready`, and
watch your fleet TUI.
