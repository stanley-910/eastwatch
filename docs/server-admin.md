# Pilot server admin crib

Every command you need to inspect, restart, respin, or tear down the hosted
pilot. Verified against the live host on 2026-08-06. Companion to
[`architecture.md`](architecture.md) (the *why*); this file is the *how*.

All commands run from your Mac via `ssh admin@eastwatch-host` unless marked
"(Mac)". Sudo is passwordless on the host.

## The lay of the land (this pilot host)

| Thing | Where |
|---|---|
| code tree | `/opt/eastwatch` — NOT a git repo, a copied tree; update it by scp/rsync from a real checkout, then rebuild images / re-run `install-helper.sh` as needed |
| data root (canonical) | `/srv/eastwatch` — containers mount from here; `/srv/eastwatch` is the same tree (alias for bw-server's `$HOME` default) |
| controller DB | `.../controller/data/controller.db` → `/var/lib/eastwatch` in-container |
| controller config + env | `.../controller/config.yaml`, `.../controller/controller.env` (root, 0600) |
| workspace home | `.../<workspace_id>/home` → `/home/bw` in-container (only thing that survives respins) |
| host CLI | checkout venv: `/opt/eastwatch/.venv/bin/{bw,bw-admin}`; ssh entry shim `deploy/host/bw-server` (execs `../../.venv/bin/bw` relative to itself). Note: `/opt/eastwatch` from `install-helper.sh` does NOT exist on this host — the pilot skipped that step and runs from the checkout |
| containers | `bw-controller`, `bw-workspace-<id>` (one per teammate), `bw-headroom` |

⚠️ The `deploy/*/run.sh` scripts default to `/srv/eastwatch/...` which does
NOT exist here. Every respin must pass the real roots:

```sh
export BW_CONTROLLER_ROOT=/srv/eastwatch/controller
export BW_WORKSPACE_USERS_ROOT=/srv/eastwatch
```

## Status and health

```sh
sudo docker ps -a --filter name=bw- --format '{{.Names}}\t{{.Status}}'
bw fleet                    # (Mac) live rows per run
bw doctor                   # (Mac) full workspace readiness check
sudo docker exec bw-workspace-<id> python -m eastwatch.runner.doctor   # same, host-side
```

Controller liveness: it publishes no host port. Quickest checks are its log
tail (below) advancing every ~5s, or `bw fleet` returning at all (proves the
DB is readable and fresh).

## Logs

```sh
sudo docker logs -f --tail 100 bw-controller        # poller, dispatch, outbox
sudo docker logs -f --tail 100 bw-workspace-<id>    # runner daemon: claims, leases
sudo docker logs -f --tail 100 bw-headroom          # per-request PERF lines, token exchange
bw logs <job_id> --follow [--session]               # (Mac) one run's journal / transcript
sudo docker exec -it bw-workspace-<id> tmux attach-session -r -t =bw-<run_id>   # watch a live worker (read-only)
```

Every completion note on GitLab also embeds the exact `docker exec ... tail`
command for its own run dir.

## Restart (in place — same container, same config)

```sh
sudo docker restart bw-controller       # safe anytime: leases/outbox live in the DB, runners retry
sudo docker restart bw-headroom         # safe: only breaks gpt requests mid-flight; pi retries fall back direct
sudo docker restart bw-workspace-<id>   # ⚠️ KILLS live workers in that workspace
```

Restarting a workspace kills its tmux server, so every running job dies →
lease expires → controller marks it `runner-lost` → `agent::failed` on the
issue. Started work is never auto-retried. **Drain first**: check `bw fleet`
shows no `working` rows for that workspace, or accept the failures.

## Respin (recreate the container — after an image rebuild or run-flag change)

Only the bind mounts survive (`/home/bw` for workspaces; data/logs/config for
the controller). Rootfs and `/tmp` are disposable by design.

```sh
cd /opt/eastwatch
export BW_CONTROLLER_ROOT=/srv/eastwatch/controller
export BW_WORKSPACE_USERS_ROOT=/srv/eastwatch

# workspace (drain it first — see Restart)
sudo docker build -f deploy/workspace/Dockerfile -t eastwatch-workspace:pilot .
sudo docker rm -f bw-workspace-<id>
sudo -E deploy/workspace/run.sh <id>

# controller
sudo docker build -f deploy/controller/Dockerfile -t eastwatch-controller:pilot .
sudo docker rm -f bw-controller
sudo -E deploy/controller/run.sh

# headroom
sudo docker build -f deploy/headroom/Dockerfile -t eastwatch-headroom:pilot deploy/headroom
sudo docker rm -f bw-headroom
sudo -E deploy/headroom/run.sh <owner_workspace_id>
```

The run scripts do create → network connect → start; that ordering is
load-bearing (the container must join `bw-internal` before starting). Never
substitute a plain `docker run --network`.

## Shut down / start up

```sh
# Stop everything (order: workspaces first so no new claims, then controller)
sudo docker stop bw-workspace-<id> ...   # after draining
sudo docker stop bw-controller bw-headroom

# Start everything (reverse order)
sudo docker start bw-controller bw-headroom
sudo docker start bw-workspace-<id> ...
```

Stopping only the controller is a soft pause: runners idle on failed claims
and resume when it returns; nothing is lost. All containers are
`--restart unless-stopped`, so a host reboot brings the stack back by itself.

## Cleaning

Safe to delete:

- Old images after rebuilds: `sudo docker image prune -f`
- A finished run's artifacts inside a workspace home (or wait — merge-gated
  retention removes worktree + run dir + session 7 days after the MRs merge)

Never delete:

- `.../controller/data/` (the DB **is** the control plane)
- Any `<workspace_id>/home` while that teammate exists (their creds, repos,
  sessions, worktrees)
- The `bw-internal` / `bw-egress` networks (recreate only via the run
  scripts — `bw-internal` must be `--internal`)

Retire a teammate: drain → `sudo docker rm -f bw-workspace-<id>` → archive or
delete `<users_root>/<id>/` → mark the workspace disabled in the DB (via the
controller, see below).

## Database rules (the one sharp edge)

- **Reads**: anything read-only is fine from the host — `bw fleet` does
  `mode=ro`. Ad-hoc: `sudo docker exec bw-controller python -c "..."` with a
  `file:...?mode=ro` URI.
- **Writes**: ONLY through the controller container, e.g. updating a
  workspace allowlist:

  ```sh
  sudo docker exec bw-controller python -c "
  import sqlite3, json
  c = sqlite3.connect('/var/lib/eastwatch/controller.db')
  c.execute('update workspaces set allowed_specs_json=? where workspace_id=?',
            (json.dumps([...]), 'stanley')); c.commit()"
  ```

  A host-uid writer leaves `-wal`/`-shm` sidecars the controller can't write,
  which **silently freezes all dispatch** while `docker ps` looks healthy.
  Symptom in the controller log: `attempt to write a readonly database`.
- **Repair** after any accidental host-side write:

  ```sh
  sudo chown 10001:1000 /srv/eastwatch/controller/data/controller.db*
  sudo chmod 0664       /srv/eastwatch/controller/data/controller.db*
  ```

Allowlist edits apply immediately (dispatch reads the row per event). Config
(`config.yaml`) changes need a controller restart — it reads config at
startup, unlike local mode.

## Common admin ops, one-liners

```sh
# Onboard a teammate (then hand them the printed checklist)
sudo /opt/eastwatch/deploy/host/onboard-workspace.sh <id> <gitlab-user> <uid> \
  --projects '[{"host":"gitlab.example.com","path":"group/repo"}]' \
  --users-root /srv/eastwatch \
  --database /srv/eastwatch/controller/data/controller.db

# Bootstrap a new project's labels + board lanes (from any checkout of it)
glab-board setup

# Add the project to the controller: edit config.yaml, then
sudo docker restart bw-controller

# Add the project to a workspace: edit BW_PROJECTS_JSON in <users_root>/<id>/runner.env,
# respin the workspace, clone the repo inside, then run:
sudo docker exec bw-workspace-<id> python -m eastwatch.runner.doctor
```

Known gaps (as of 2026-08-06): no `bw-admin` command to update an existing
workspace (projects or specs — hand-edit as above); emoji/✅ approval of
parked runs is local-only.
