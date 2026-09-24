# Eastwatch onboarding examples

## New user and first project

User request:

> Set up Jamie on macOS for `gitlab.example.com/team/app`.

Agent flow:

1. Collect Jamie's GitLab numeric user ID, workspace slug, SSH alias, editor, project ID, and remote paths.
2. Run the client check, whitelist only Jamie's public SSH key on the resolved server account, verify `BatchMode` SSH, and clone Eastwatch.
3. Classify the remote checkout as a Git clone or copied tree, then verify the project-routing, config-ownership, and runner-doctor capabilities from Gate B.
4. Complete Gate C: read the checkout's supported default and run `onboard-workspace.sh` once for `jamie` with every known project and that Pi model at `medium` effort.
5. Complete Gate D: pause while the operator enters each project token invisibly on the remote host; run `bw-admin add-hosted-project` through stdin.
6. Enter `bw-workspace-jamie`, prove UID/GID 1000 and persistent `.config` writability, then pause for Jamie's Pi login and personal GitLab PAT (`api` + `write_repository`).
7. Clone `team/app`, run Forge board setup, and run `python -m eastwatch.runner.doctor` to all-OK.
8. Write Jamie's client `remote.yaml`; verify Fleet CLI/TUI connectivity.
9. Ask approval to use a test issue, retrigger `agent::ready` after bootstrap, and verify a medium-effort running job.

Expected topology:

```text
shared bw-controller
└── gitlab.example.com/team/app -> project token

bw-workspace-jamie
└── team/app checkout + Jamie's Git/Pi credentials
```

## Add a second project to an existing user

User request:

> Add `gitlab.example.com/team/docs` to Jamie's agent.

Agent flow:

1. Confirm `bw-workspace-jamie` and its durable home already exist.
2. Do not run `onboard-workspace.sh` or create another controller.
3. Complete Gate D with `bw-admin add-hosted-project jamie gitlab.example.com team/docs <id> --token-stdin ...`.
4. Re-enter Jamie's existing workspace, rerun the persistent-home writability probes, and clone the docs repo.
5. Run Forge board setup and `python -m eastwatch.runner.doctor`.
6. Keep Jamie's existing client `remote.yaml`; verify both projects appear through the same Fleet endpoint.
7. With approval, dispatch one docs issue and verify it runs in `bw-workspace-jamie` at medium effort.

Expected topology:

```text
shared bw-controller
├── team/app  -> app project token
└── team/docs -> docs project token

bw-workspace-jamie
├── team/app
└── team/docs
```

## Native Windows client

Use PowerShell and Windows OpenSSH for client operations. Run all Docker/admin commands remotely over SSH on Linux. The skill's Node scripts write `$HOME/.config/eastwatch/remote.yaml` without relying on `sed`, Bash path expansion, or macOS-specific flags. If Unix-oriented repo commands do not run natively, prefer WSL and keep the entire checkout/config in the same WSL distribution.
