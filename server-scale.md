# Handoff: Server Scaling and Phone-Driven Cloud Execution

## Next-session focus

Decide and, if requested, design the smallest deployment that lets a team use eastwatch + Forge while supporting:

- A cloud-hosted default for users who want phone-triggered, always-on AFK execution.
- Optional local execution for developers who want their existing machine, credentials, skills, worktrees, and Pi sessions.

## Repository context

- Repository: `eastwatch`
- Branch: `main`
- Starting commit: `60cfb2e`
- No server-scaling code was implemented in this session.
- Detailed analysis is in:
  - `.rpiv/artifacts/solutions/2026-07-16_10-09-33_multi-user-execution-backends.md`
- That artifact includes the four evaluated architectures, code references, risks, effort ranges, testing strategy, and a timestamped follow-up covering setup burden, offline behavior, and the phone/cloud scenario. Read it rather than reproducing the research.
- Existing unrelated working-tree changes were present in `PAPERCUTS.md`, `src/eastwatch/watcher.py`, and `tests/test_tmux_observability.py`. Do not overwrite or discard them without inspecting ownership and intent.

## Decisions reached

1. **Use a hybrid architecture as the end state.**
   - Central control plane owns GitLab polling, identity, queueing, leases, and status.
   - Users choose a local pull runner or a personal hosted workspace.
   - Shared ephemeral runners are deferred.

2. **The boss's default should be hosted.**
   - The phone is only a control surface through GitLab mobile/web.
   - Boss comments `@agent`, changes a label, or approves a follow-up.
   - Pi/Claude and Forge execute in an always-on cloud workspace.
   - Results and MR links return to the GitLab issue.
   - The boss's laptop can remain off.

3. **Local execution is opt-in.**
   - It is the only way to preserve actual access to a developer's physical filesystem, keychain, local network, tools, worktrees, and sessions.
   - If that machine is asleep/offline, it cannot execute. A future central queue can retain work, but execution waits or is explicitly rerouted.

4. **Use sticky execution affinity.**
   - Pin each conversation to its runner/workspace.
   - Pi session files, cwd, worktrees, and credentials are backend-local.
   - Rerouting should start a new session unless explicit state migration succeeds.

5. **Do not treat per-user directories under one shared Unix identity as isolation.**
   - A hosted user needs a separate OS user, container, VM, or microVM; persistent `HOME`; private auth/config/skills/repos/sessions; and scoped secrets.
   - Pi skills/packages run with process-level system access.

## Smallest pilot

Use one personal, always-on macOS environment for the boss:

- Office Mac mini, MacStadium host, or EC2 Mac.
- Persistent user session/account.
- Current eastwatch installation and LaunchAgent.
- Boss's configured GitLab identity/token, Pi configuration, chosen skills, Forge installation, repos, worktrees, and sessions.

This is the lowest-change route because current code assumes macOS `launchd` and the `security` Keychain CLI. A Linux VM/container requires a secret-provider abstraction and a service supervisor such as systemd.

Estimated effort from the analysis:

- First cloud macOS workspace/image: **2–6 hours**.
- Additional manually provisioned personal workspace: **30–60 minutes admin time**.
- Boss onboarding after access exists: **5–15 minutes**.
- Current local per-user setup: **45–120 minutes** with prerequisites, or **2–4 hours** fresh.
- Purpose-built local runner installer/checker: **4–12 engineering hours**, reducing happy-path onboarding to roughly **10–30 minutes**.
- Trusted-team central coordinator + local-runner MVP: approximately **15–30 engineering days**.

## Important current-code constraints

See the solutions artifact for line-level evidence. Key constraints:

- The watcher is already a local macOS LaunchAgent polling roughly every five seconds.
- GitLab token retrieval is hardwired to macOS Keychain.
- Config models one `owner`; project-global label triggers are not yet routed to individual users.
- Current liveness/status relies on local files, pids, tmux, and cwd paths.
- Polling reads bounded recent GitLab activity, so the current fully local watcher is not a durable offline queue.
- A useful extraction seam exists: `watcher.py --worker <request.json>` plus request/result/error artifacts.

## Open decisions

1. Which host should the boss pilot use: office Mac mini, MacStadium, EC2 Mac, or a modest Linux adaptation?
2. Should GitLab result comments remain authored by a central project bot, while personal credentials are used only for git/Forge operations?
3. What owns an `@agent` conversation: gesture author, issue assignee, explicit target argument, or a per-user label?
4. Is issue-comment interaction sufficient on the phone, or is interactive terminal/chat access also required?
5. Is the next deliverable an operational pilot runbook or a design for the eventual control-plane/runner split?
6. What repositories are trusted to auto-run without a local approval prompt?

## Recommended next actions

1. Pick the boss's cloud host and confirm that GitLab issue comments are sufficient as the phone UX.
2. Provision one isolated personal macOS runner using the existing installer; do not generalize multi-tenancy yet.
3. Record exact setup, credentials, restart, backup, and offboarding steps as a pilot runbook.
4. Validate from a phone: start work, answer a follow-up, approve, observe MR/result, and recover after host restart.
5. Only after the pilot, design the central identity/queue/lease protocol and local pull runner.

## Suggested skills

- **`research`** — compare current MacStadium, EC2 Mac, office Mac mini, and Linux-hosting costs/constraints using primary sources.
- **`design`** — turn `.rpiv/artifacts/solutions/2026-07-16_10-09-33_multi-user-execution-backends.md` into the control-plane/runner architecture.
- **`blueprint`** — use instead of `design` for a faster phased pilot plan with implementation checkpoints.
- **`forge`** — create or claim a tracked pilot/design issue once scope and ownership are decided.
- **`prototype`** — mock the phone-to-GitLab-to-cloud-runner interaction before building a new UI.
- **`diagnose`** — use if the current watcher fails on the selected cloud macOS host.
