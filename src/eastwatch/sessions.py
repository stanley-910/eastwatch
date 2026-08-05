"""Resolve provider-native session transcripts from a session id.

Eastwatch does not capture raw provider stdout by default — run journals hold
the compact facts, and the provider's own session file holds the full trace.
That only works if we can find the session file, which is what this module is
for.
"""

from __future__ import annotations

from pathlib import Path

from eastwatch.env import getenv


def claude_config_dir() -> Path:
    """Return Claude's configuration root, honouring CLAUDE_CONFIG_DIR."""
    raw = getenv("CLAUDE_CONFIG_DIR")
    return Path(raw).expanduser() if raw else Path.home() / ".claude"


def find_claude_session_file(session_id: str | None) -> str | None:
    """Return Claude's native transcript for a session id, or None.

    Claude stores transcripts at `<config>/projects/<slug>/<session-id>.jsonl`,
    where the slug is derived from the worker cwd by an internal scheme. Glob
    the session id instead of reconstructing the slug, so a change to that
    scheme cannot silently break discovery.
    """
    if not session_id:
        return None
    try:
        files = sorted(claude_config_dir().glob(f"projects/*/{session_id}.jsonl"))
    except OSError:
        return None
    return str(files[-1]) if files else None
