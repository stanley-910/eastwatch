---
name: eastwatch-onboarding
description: Guides and automates end-to-end hosted Eastwatch onboarding for a new user and GitLab project, from cloning through verified Pi dispatch with GitHub Copilot-backed models. Use when provisioning a Eastwatch user, setting up a client machine, adding a project-scoped token, or troubleshooting first-run Fleet and remote dispatch on macOS, Linux, Windows, or WSL.
compatibility: Pi with GitHub Copilot-backed models on macOS, Linux, Windows, or WSL; remote Linux host with Docker; GitLab hosted projects.
---

# Eastwatch onboarding

## Outcome

Finish only when one persistent workspace serves all of the user's configured projects, every project uses its own scoped bot token through the shared controller, Fleet works from the client, GitHub Copilot authentication is usable through Pi, and a real issue reaches `agent::working` with the repository's supported Pi model at `medium` effort.

## Operating rules

- Run the setup; do not only describe it. Pause for interactive login, secret entry, sudo approval, or an explicitly approved GitLab mutation.
- Never request a token or private SSH key in chat. A public `.pub` key is safe to collect for server whitelisting. Accept project tokens only through hidden local input piped to `bw-admin add-hosted-project --token-stdin`.
- Never put tokens in argv, YAML, logs, issues, or shell history.
- One controller database and one workspace per user. Do not create a workspace or controller per project.
- Do not create an issue, comment, or change labels without explicit approval. Record an approved issue with `agent-link issue <url>` when available.
- Treat the remote host as Linux + Docker. Adapt only client-side commands for macOS, Linux, native Windows, or WSL.
- Externalize progress as Gates A–G from [REFERENCE.md](REFERENCE.md). Show the current gate, completed checks, blocker, and next action every turn.

## Workflow

1. **Collect non-secrets.** Ask for client OS/shell, repo URL, remote SSH target/alias, server checkout/root paths and deployment provenance, GitLab host, username/user ID, workspace ID, project path/ID, editor, and whether the workspace already exists.
2. **Bootstrap the client.** Run `node scripts/client-check.mjs`. Install or direct installation of missing Git, OpenSSH, Node/Pi, and uv using the OS adapter in [REFERENCE.md](REFERENCE.md). Generate or locate the user's public SSH key, have an admin whitelist it, and prove noninteractive SSH. Clone Eastwatch if absent and read its `ONBOARDING.md` plus hosted sections of `RUNBOOK.md`.
3. **Prepare the shared host.** Resolve the SSH account's actual UID/home, classify the Eastwatch checkout as a Git clone or copied tree, and confirm Docker, helper venv, shared `bw-controller`, internal network, controller DB, and required onboarding capabilities. Update/rebuild only with approval; preserve existing controllers, databases, homes, and worktrees.
4. **Provision one workspace.** For a new user, run `deploy/host/onboard-workspace.sh` once with every known project and the script's supported Pi default at `medium` effort. For an existing user, reuse the current workspace and durable home.
5. **Route each project.** Use the stdin-safe `bw-admin add-hosted-project` command from [REFERENCE.md](REFERENCE.md). It validates the project token/bot, ensures an issue board, updates per-project token routing and workspace allowlist, and recreates only containers whose environment changed.
6. **Finish interactive credentials.** Enter the workspace; verify UID 1000 and persistent config writability before completing Pi login, `glab auth login --hostname <host>`, Git identity, project clones, and Forge `glab-board setup`. Use the location-specific doctor command from [REFERENCE.md](REFERENCE.md) until every check is `OK`.
7. **Configure the client.** Run `node scripts/write-remote-config.mjs` with the five required values. Use one stable shared `bw-server`, then verify `uv run bw fleet` and `uv run fleet_tui.py` from the cloned repo.
8. **Prove dispatch.** With explicit approval, use a disposable or user-selected issue. Allow the controller's first poll to adopt state, remove/re-add `agent::ready`, and verify the label becomes `agent::working`, the controller job is leased/running at `medium`, and both Fleet views show it.
9. **Close out.** Report paths, project/workspace/controller mapping, model, verification evidence, and any manual credential renewal steps. Do not delete rollback files, worktrees, or controllers.

## Recovery

Stop after three failed iterations. Name the failing gate and assumption, then use the symptom table in [REFERENCE.md](REFERENCE.md). Roll back config/env/runner changes through the repository command rather than hand-editing controller databases.

## Examples

See [EXAMPLES.md](EXAMPLES.md) for first-user and add-project sessions.
