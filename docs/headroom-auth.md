# Why Eastwatch workers avoid Copilot auth races

Working note from 2026-07-19. External Claude sessions launching `pi` in
parallel hit auth races that Eastwatch avoids.

## The race, in one transcript

```
laptop: launches 5× `pi -p --provider github-copilot …` at once
pi #1-5: each checks the shared Copilot bearer cache → all see stale/missing
pi #1-5: all hit the token-exchange endpoint simultaneously
pi #3:   wins; #1's fresh bearer is overwritten/invalidated mid-launch
pi #1:   "No API key" → dead on arrival
```

Direct `github-copilot` means **every pi process mints its own short-lived
Copilot bearer** by exchanging the stored GitHub OAuth credential, through a
shared on-disk cache. The race *is* the per-process exchange; it scales with
parallelism.

## Why headroom-copilot avoids it

The Headroom proxy (`com.stanwang.headroom-proxy`, launchd, port 8787) is the
only process that exchanges tokens. It keeps one bearer refreshed. Workers
talk to `127.0.0.1:8787` with the static `headroom-local` key, so they do not
share a mutable bearer cache. The Keychain step supplies the proxy's startup
credential once.

## Eastwatch's three layers (`watcher.py`)

1. `pi_provider_order` prefers `headroom-copilot` for GPT models.
2. `run_pi_provider_command` holds `pi.lock` through
   `wait_for_pi_launch_window`, serializing the launch and auth window.
3. `run_pi_attempt` retries direct `github-copilot` auth failures up to three
   times, with a 25-second delay.

## Known failure modes that are NOT the race

- Headroom restart windows: launchd may report an I/O error after a plist to
  symlink swap. Headroom 0.30 can also show a blocking Keychain dialog on
  restart. A wedged proxy reports "Model … not found" or stalls.
- Mid-stream drops ("stream ended before a terminal response event") need one
  session resume, not an auth reset.

## Guidance for shell-out sessions (Claude drivers etc.)

Use `--provider headroom-copilot`. Treat direct `github-copilot` as a fallback
for proxy outages; serialize those launches or rely on the auth retry. For a
transient Headroom failure, resume the session once.

Deeper reading: `~/dotfiles/scripts/HEADROOM_PI_COPILOT.md`, RUNBOOK Headroom
section, `~/.config/pi/agent/models.json` provider defs.
