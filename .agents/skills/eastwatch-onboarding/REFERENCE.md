# Eastwatch onboarding reference

Use this checklist as session state. Never advance a gate with a failed check.

## Required facts

Collect these without asking for secrets in chat:

- Client OS: macOS, Linux, Windows, or WSL; shell; editor
- Eastwatch Git URL and local checkout path
- Remote SSH target, alias, and shared Unix login
- Remote Eastwatch checkout, checkout provenance (Git clone or copied tree), controller root, users root, and controller DB
- GitLab host, username, numeric user ID
- Workspace ID (lowercase filesystem-safe slug)
- Each GitLab project path and numeric project ID
- Whether the shared controller and user workspace already exist

Secrets stay at the keyboard: project access token, personal GitLab token, Pi/Copilot login, and private SSH keys. Public `.pub` keys are identifiers and may be sent to the server admin.

## Credential ownership

Do not substitute one credential for another:

| Credential | Created by | Stored and used where |
|---|---|---|
| SSH private key | User | Client only; never copy it to the server |
| SSH public key | User/admin | Shared Unix account's `~/.ssh/authorized_keys` |
| GitLab project access token | Project maintainer | Shared controller `controller.env`; one route per project |
| GitLab personal access token | User | Workspace glab config; requires `api` and `write_repository` |
| Runner token | `onboard-workspace.sh` | Auto-minted in workspace `runner.env`, hashed in controller DB; never supplied manually |
| Pi/Copilot credential | User | Workspace path named by `PI_CODING_AGENT_DIR` |

## Command context

The three doctor entrypoints are not interchangeable:

| Where the operator is | Command |
|---|---|
| Client laptop | `bw doctor` |
| Remote host | `sudo docker exec bw-workspace-<workspace-id> python -m eastwatch.runner.doctor` |
| Workspace container | `python -m eastwatch.runner.doctor` |

`bw` is the laptop remote-control client. It reads
`~/.config/eastwatch/remote.yaml`, connects through SSH, and asks
`bw-server` to run the workspace doctor. Do not create `remote.yaml` inside a
workspace container; call the runner module directly there.

## Skill distribution

This skill supports both kickoff modes:

- **Operator-driven:** invoke the globally installed skill on the operator's Pi and drive the user's client through a shared terminal or remote session.
- **New-user Pi:** copy this entire skill directory to
  `~/.agents/skills/eastwatch-onboarding/` on the new machine, restart Pi,
  then invoke `/skill:eastwatch-onboarding`. On native Windows the same
  path is `$HOME/.agents/skills/eastwatch-onboarding/` in PowerShell.

Use `scp`, `rsync`, a reviewed archive, or managed device software to transfer
the directory. Never bundle user tokens, SSH private keys, generated
`remote.yaml`, or deployment env files with it.

## Gate A — client ready

Run from the skill directory:

```sh
node scripts/client-check.mjs
```

Required:

- Git
- OpenSSH client
- Node and Pi
- uv
- Eastwatch checkout
- An SSH alias that reaches the remote host

### SSH public-key whitelist

Use an existing dedicated key or generate one on the user's client:

```sh
ssh-keygen -t ed25519 -C '<gitlab-username>@eastwatch'
```

Never copy or display the private key. Send only the matching `.pub` file to a
server admin. Before editing `authorized_keys`, the admin must resolve the
account rather than assuming its login name matches its Unix identity:

```sh
id <ssh-login>
getent passwd <ssh-login>
```

Copy the public key to a temporary server path, then install it idempotently
under the resolved account home:

```sh
resolved_user=$(id -un <ssh-login>)
home=$(getent passwd "$resolved_user" | cut -d: -f6)
group=$(id -gn "$resolved_user")
sudo install -d -m 0700 -o "$resolved_user" -g "$group" "$home/.ssh"
sudo touch "$home/.ssh/authorized_keys"
sudo chown "$resolved_user":"$group" "$home/.ssh/authorized_keys"
sudo chmod 0600 "$home/.ssh/authorized_keys"
sudo grep -qxF -f /tmp/eastwatch-user.pub "$home/.ssh/authorized_keys" \
  || sudo tee -a "$home/.ssh/authorized_keys" </tmp/eastwatch-user.pub >/dev/null
rm -f /tmp/eastwatch-user.pub
```

Verify from the client before any controller work:

```sh
ssh -o BatchMode=yes <ssh-alias> 'id; printf "SSH_OK\\n"'
```

`Permission denied (publickey,...)` is an SSH key/account/host problem. A later
`Permission denied` naming `controller.env` or `config.yaml` is a remote file
ownership problem; do not conflate them or weaken directories to mode 0777.

### OS adapters

- **macOS:** Homebrew is preferred. Config path is `~/.config/eastwatch/remote.yaml`.
- **Linux:** use the distribution package manager for Git/OpenSSH; install uv from its official installer. Config path is the same.
- **WSL:** perform the complete client setup inside one WSL distribution. Use its home directory and OpenSSH. `editor: code` expects WSL Remote support.
- **Native Windows:** use PowerShell, Windows OpenSSH, Git for Windows, Node, and uv. `$HOME/.config/eastwatch/remote.yaml` remains the config path. Do not use `sed`, POSIX-only quoting, or Unix socket assumptions on the client.

The remote host commands remain Linux shell commands executed through SSH.

## Gate B — repository and remote host ready

Clone when absent:

```sh
git clone <eastwatch-git-url> <checkout>
cd <checkout>
```

Read `ONBOARDING.md` and the hosted sections of `RUNBOOK.md`. Confirm the checkout includes:

```text
bw-admin add-hosted-project
src/eastwatch/controller/tokens.py
deploy/host/onboard-workspace.sh
```

On the remote host, verify:

```sh
docker version
docker ps --format '{{.Names}}\t{{.Status}}'
test -x <remote-checkout>/.venv/bin/bw-admin
test -f <controller-root>/config.yaml
test -f <controller-root>/controller.env
test -f <controller-root>/data/controller.db
```

Classify the remote checkout before attempting an update:

```sh
if git -C <remote-checkout> rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git -C <remote-checkout> rev-parse HEAD
  printf 'Git checkout\n'
else
  printf 'Copied tree — do not run git pull\n'
fi
```

Confirm the deployed onboarding script contains the required ownership and
doctor fixes:

```sh
grep -F 'install -d -o 1000 -g 1000' \
  <remote-checkout>/deploy/host/onboard-workspace.sh
grep -F 'python -m eastwatch.runner.doctor' \
  <remote-checkout>/deploy/host/onboard-workspace.sh
bash -n <remote-checkout>/deploy/host/onboard-workspace.sh
```

For a Git checkout, pull/rebuild only with approval. For a copied tree, never
run `git pull`; deploy approved files with `scp` or `rsync`, preserve their
owner/mode, and compare local and remote SHA-256 checksums. Never claim a local
commit is deployed until the remote capability checks and checksum pass.

Do not replace an existing controller DB or workspace home.

For a fresh remote host, follow the repository's hosted RUNBOOK to install the
helper venv, build `eastwatch-controller:pilot`, and create the shared
controller root. Seed it without project secrets:

```yaml
execution:
  mode: hosted
  controller:
    database: /var/lib/eastwatch/controller.db
    listen_host: bw-controller-internal
    listen_port: 8765
    admins: [<admin-gitlab-username>]
    approved_bots: []
projects: []
```

Create an empty mode-0600 `controller.env`, then run
`BW_CONTROLLER_ROOT=<controller-root> deploy/controller/run.sh`. Confirm
`bw-controller`, `bw-internal`, and the controller DB exist. Confirm the Pi
provider route required by the repository's current workspace model template is
running before provisioning users.

## Gate C — one user workspace ready

Create the workspace only if absent. Pass every currently known project in one
single-quoted JSON array and use one `--projects` flag:

```sh
sudo env BW_CONTROLLER_DB=<controller-db> \
  bash <remote-checkout>/deploy/host/onboard-workspace.sh \
  <workspace-id> <gitlab-username> <gitlab-user-id> \
  --projects '[{"host":"<gitlab-host>","path":"<group/project-one>","id":<project-one-id>},{"host":"<gitlab-host>","path":"<group/project-two>","id":<project-two-id>}]' \
  --default-spec pi:<supported-model>:medium \
  --users-root <users-root> \
  --checkout <remote-checkout>
```

For an existing workspace, never call `create-workspace` again. Continue to
Gate D; `add-hosted-project` updates its allowlist safely.

Verify:

```sh
docker ps --filter name=bw-workspace-<workspace-id>
grep '^BW_CONTROLLER_URL=' <users-root>/<workspace-id>/runner.env
grep '^BW_PROJECTS_JSON=' <users-root>/<workspace-id>/runner.env
```

The URL must point to the shared controller, not a project-specific controller.

## Gate D — shared controller routes each project token

Repeat this gate for every project. Project token requirements:

- GitLab project access token
- Developer role
- `api` scope
- Bot identity owned by the same numeric project

Read it invisibly and pipe only through stdin:

```sh
read -rsp 'Project access token: ' PROJECT_TOKEN; echo
printf '%s' "$PROJECT_TOKEN" | sudo \
  <remote-checkout>/.venv/bin/bw-admin add-hosted-project \
  <workspace-id> <gitlab-host> <group/project> <project-id> \
  --token-stdin \
  --controller-root <controller-root> \
  --users-root <users-root> \
  --controller-run-script <remote-checkout>/deploy/controller/run.sh \
  --workspace-run-script <remote-checkout>/deploy/workspace/run.sh
unset PROJECT_TOKEN
```

When controlling the host over SSH, run `read` on the remote interactive terminal. Do not interpolate the token into an `ssh "..."` command.

Expected effects:

- Token validated against `/user` and `/projects/<id>`
- Default `Agent Board` created if none exists
- `projects[].bot_token_env` written to controller YAML
- Token written only to mode-0600 `controller.env`
- Bot username added to approved bots
- Project added to workspace `BW_PROJECTS_JSON`
- Shared controller/workspace recreated only if needed
- Exact rerun reports already routed and changes nothing

## Gate E — user credentials, checkout, board, doctor

Enter the workspace:

```sh
sudo docker exec -it bw-workspace-<workspace-id> zsh -l
```

Inside it, prove the persistent home is writable before opening either
interactive login:

```sh
test "$(id -u)" = 1000
test "$(id -g)" = 1000
test "$PI_CODING_AGENT_DIR" = /home/bw/.config/pi/agent
test -w "$HOME"
test -w "$HOME/.config"
mkdir -p "$PI_CODING_AGENT_DIR/sessions" "$HOME/.config/glab-cli"

pi
# Complete /login, then exit Pi.

# Use the user's personal access token, not the project token.
# Required scopes: api and write_repository.
glab auth login --hostname <gitlab-host>
git config --global user.name '<name>'
git config --global user.email '<email>'

git clone https://<gitlab-host>/<group/project>.git \
  ~/repos/<gitlab-host>/<group/project>
cd ~/repos/<gitlab-host>/<group/project>
/opt/eastwatch/skills/forge/scripts/glab-board setup
python -m eastwatch.runner.doctor
```

Doctor must show `OK` for Git, glab, Pi, Forge, identity, GitLab auth, repositories, home write, controller, and dispatch readiness. Pi credentials must live under `PI_CODING_AGENT_DIR=/home/bw/.config/pi/agent`.

Read the supported model ID from the repository's current
`deploy/host/onboard-workspace.sh` default. Use `medium`, never `med`, as the
effort name.

## Gate F — client Fleet ready

Write the client config from the skill directory:

```sh
node scripts/write-remote-config.mjs \
  --ssh-target '<user@host-or-alias>' \
  --ssh-alias '<ssh-config-alias>' \
  --owner '<gitlab-username>' \
  --server-command '<remote-checkout>/deploy/host/bw-server' \
  --editor 'code'
```

Then, from the Eastwatch checkout:

```sh
uv run bw fleet
uv run fleet_tui.py
```

An empty Fleet is valid before the first run. A connection error, permission error, or stale project-specific `server_command` is not.

## Gate G — live dispatch proven

Get explicit approval for the GitLab issue and label mutation. Prefer an existing disposable issue owned by the user.

1. Record it when available: `agent-link issue <url>`.
2. Wait for the controller's first project poll to set `bootstrapped=1`.
3. Remove any existing trigger label, then re-add `agent::ready`.
4. Confirm exactly one assignee, or leave it unassigned so the promoting user owns dispatch.
5. Verify within the polling window:

```text
GitLab label: agent::working
job state: leased or running
workspace: expected workspace ID
model: the workspace's configured Pi model
effort: medium
bw fleet: row for host/group/project#issue
fleet TUI: same row
```

Do not declare success from `doctor` alone. Success requires a real leased/running job and visibility from the client.

## Symptom table

| Symptom | Check first | Fix |
|---|---|---|
| `agent::ready` stays unchanged | Controller project token | Re-run Gate D; confirm project bot and token env |
| First ready label is ignored | Project bootstrap state | Remove and re-add the label after first poll |
| `agent::failed`, model unsupported | Effort spelling | Set workspace default/allowlist to `pi:gpt-5.6-sol:medium` |
| Doctor controller failure | Runner URL/network | Use shared `bw-controller-internal`; recreate workspace container |
| Fleet empty during active run | Client `server_command`/owner | Rewrite `remote.yaml`; close stale SSH multiplex connections |
| SSH says `Permission denied (publickey,...)` | Public-key whitelist/account | Re-run Gate A; verify resolved home, ownership, modes, alias user, and key selected with `ssh -v` |
| Token/config command names a file permission denial | Remote ownership/sudo path | Use `sudo bw-admin`; do not write controller env directly or use mode 0777 |
| Config permission denied inside controller logs | Config mode/group | Run controller deploy script; config must be root:gid-1000 mode 0640 |
| glab authenticated but API/push fails | Wrong GitLab host | Re-run `glab auth login --hostname <host>` |
| Pi reports `EACCES` creating `agent/sessions` | Persistent `.config` ownership | On the host run `sudo chown -R 1000:1000 <users-root>/<workspace-id>/home/.config`, then rerun the Gate E probes |
| glab reports permission denied creating `.config/glab-cli` | Persistent `.config` ownership | Apply the same targeted `.config` ownership repair; never chmod 0777 |
| `bw doctor` inside the container asks for `remote.yaml` | Wrong command context | Run `python -m eastwatch.runner.doctor`; `bw doctor` is the laptop client command |
| Pi doctor green but worker auth fails | Wrong credential directory | Log in with `PI_CODING_AGENT_DIR=/home/bw/.config/pi/agent` |
| Board setup says no default board | Missing board | Re-run Gate D; it creates `Agent Board` before Forge setup |
| Existing workspace would be overwritten | Wrong command | Stop; use `add-hosted-project`, never `create-workspace` |

## Final handoff checklist

Report all of these:

- Client OS, Eastwatch checkout, SSH alias, and whitelisted public-key fingerprint
- Remote host, resolved SSH account/home, and shared controller name/root
- Workspace ID, durable home, and runner controller URL
- GitLab projects and bot usernames, never token values
- Allowed/default model spec
- Doctor result
- Fleet CLI and TUI result
- Test issue URL, job ID/state, and current label
- Credential renewal commands
- Rollback or backup paths still present
