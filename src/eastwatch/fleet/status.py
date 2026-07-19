"""fleet-status: one row per active eastwatch conversation.

Joins the watcher's state.json (source of truth for card status) with `tmux ls`
(is the worker's session still live?) and each run's compact fact journal
(what is it doing right now). Emits the shared row schema that both this fleet
TUI and the vault-side twin render, so the dumb renderer can be swapped while
only the emitter differs.

    fleet-status            human table
    fleet-status --json     JSON array of rows (the swap contract)

`status` is the source-of-truth conversation state. `tmux_alive` only
disambiguates a working run from a crashed one. `derived` is computed here and
never stored — a quiet stdout artifact looks the same whether the worker is
thinking or dead, so liveness has to be joined in at read time.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from eastwatch.env import getenv


def state_dir() -> Path:
    raw = getenv("EASTWATCH_STATE_DIR")
    return Path(raw).expanduser() if raw else Path.home() / ".local/state/eastwatch"


def load_state() -> dict:
    path = state_dir() / "state.json"
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def tmux_bin() -> str | None:
    tb = shutil.which("tmux")
    if not tb:
        for cand in ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux"):
            if os.path.exists(cand):
                tb = cand
                break
    return tb


def live_tmux_sessions() -> set[str]:
    tb = tmux_bin()
    if not tb:
        return set()
    try:
        result = subprocess.run(
            [tb, "list-sessions", "-F", "#{session_name}"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if result.returncode != 0:
        return set()  # no server / no sessions
    return {ln.strip() for ln in result.stdout.splitlines() if ln.strip()}


def running_tmux_sessions() -> set[str]:
    """Sessions with at least one non-dead pane."""
    tb = tmux_bin()
    if not tb:
        return set()
    try:
        result = subprocess.run(
            [tb, "list-panes", "-a", "-F", "#{session_name}\t#{pane_dead}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if result.returncode != 0:
        return set()
    return {
        name
        for line in result.stdout.splitlines()
        if "\t" in line
        for name, dead in [line.split("\t", 1)]
        if name and dead.strip() == "0"
    }


def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def tail_last_line(path: str | None) -> str:
    if not path:
        return ""
    try:
        # Legacy fallback only: tail the end because old stream.log files can be
        # many MB. The window is generous because Pi JSON events are large — a
        # single thinking/toolcall delta carries
        # a multi-KB base64 thinkingSignature, so a small window can hold zero
        # complete renderable lines and yield a blank row.
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            window = min(size, 262144)
            f.seek(size - window)
            data = f.read()
    except OSError:
        return ""
    raw = data.decode(errors="replace").splitlines()
    if window < size and raw:
        raw = raw[1:]  # first line was cut mid-event by the window; drop it
    lines = [ln for ln in raw if ln.strip()]
    # Walk back to the last line that renders to something meaningful (skip noise events).
    for ln in reversed(lines):
        summary = summarize_line(ln)
        if summary:
            return summary
    return ""


def summarize_fact(fact: dict) -> str | None:
    fact_type = fact.get("type")
    if fact_type == "run_started":
        return f"started: {fact.get('provider', '?')}:{fact.get('model', '?')}"
    if fact_type == "session_discovered":
        return "session discovered"
    if fact_type == "provider_attempt":
        provider = fact.get("provider") or "?"
        attempt = fact.get("attempt") or "?"
        total = fact.get("total") or "?"
        outcome = f" {fact['outcome']}" if fact.get("outcome") else ""
        return f"provider: {provider} {attempt}/{total}{outcome}"
    if fact_type == "tool_first_started":
        tool = fact.get("tool") or "tool"
        return f"tool: {tool}"
    if fact_type == "agent_end":
        outcome = fact.get("outcome") or "done"
        return f"agent end: {outcome}"
    if fact_type == "agent_settled":
        return "agent settled"
    if fact_type == "guard_armed":
        return "settled guard armed"
    if fact_type == "guard_kill":
        return "settled guard reaped process"
    if fact_type == "malformed_line":
        return f"malformed stream line ({fact.get('count', 1)})"
    if fact_type == "oversized_line":
        return f"oversized stream line dropped ({fact.get('count', 1)})"
    if fact_type == "exit":
        return f"exit: {fact.get('code', '?')}"
    if fact_type == "timeout":
        return "timeout"
    if fact_type == "reply_extracted":
        return "reply extracted"
    return None


def journal_status(path: str | None) -> tuple[str, str]:
    """Return the last renderable fact and latest discovered session file."""
    if not path:
        return "", ""
    summary = ""
    session_file = ""
    try:
        with Path(path).open() as stream:
            for line in stream:
                try:
                    fact = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(fact, dict) or fact.get("v") != 1:
                    continue
                if fact.get("type") == "session_discovered" and fact.get("session_file"):
                    session_file = str(fact["session_file"])
                rendered = summarize_fact(fact)
                if rendered:
                    summary = rendered
    except OSError:
        return "", ""
    return summary, session_file


def summarize_line(line: str) -> str | None:
    """claude/pi stream events are JSONL; render a short human summary so a row is
    legible. Both providers emit pure JSON per line, so a parse failure means a
    partial/truncated line (the log is being written live) — skip it and let the
    caller scan back to the last complete event, rather than surfacing raw bytes
    (e.g. base64 from a mid-write thinkingSignature). Noise events return None."""
    line = line.strip()
    try:
        ev = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(ev, dict):
        return None
    etype = ev.get("type")
    # claude stream-json events
    if etype == "assistant":
        for block in (ev.get("message") or {}).get("content", []):
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                return "assistant: " + block["text"].strip().replace("\n", " ")[:180]
            if isinstance(block, dict) and block.get("type") == "tool_use":
                return "tool: " + str(block.get("name", "?"))
        return "assistant: …"
    if etype == "result":
        return f"result: {ev.get('subtype', 'done')}"
    if etype == "system":
        return f"system: {ev.get('subtype', '')}".strip()
    # pi --mode json events
    if etype == "message_update":
        ame = ev.get("assistantMessageEvent") or {}
        at = ame.get("type")
        if at == "text_delta" and ame.get("delta"):
            return "assistant: " + str(ame["delta"]).strip().replace("\n", " ")[:180]
        if at == "thinking_start":
            return "· thinking…"
        # toolcall_* deltas stream arg JSON char-by-char — noise; the resolved call
        # is the top-level tool_execution_start event below.
        return None
    if etype == "tool_execution_start":
        name = ev.get("toolName") or "?"
        args = ev.get("args") if isinstance(ev.get("args"), dict) else {}
        detail = (args.get("command") or args.get("file_path") or args.get("path")
                  or args.get("file") or args.get("pattern") or args.get("query") or "")
        return f"tool: {name} {str(detail).replace(chr(10), ' ')[:120]}".rstrip()
    if etype == "message_end":
        msg = ev.get("message") or {}
        if msg.get("role") == "assistant":
            texts = [b.get("text", "") for b in (msg.get("content") or [])
                     if isinstance(b, dict) and b.get("type") == "text"]
            if texts:
                return "assistant: " + "".join(texts).strip().replace("\n", " ")[:180]
        return None
    if etype == "agent_end":
        return "── done"
    return None


def derived_state(conv: dict, run: dict | None, tmux_alive: bool) -> str:
    if run:
        result_path = run.get("result_path")
        if result_path:
            try:
                if Path(result_path).is_file():
                    return "finishing"
            except OSError:
                pass
        if run.get("launch_state") == "launching" and not run.get("tmux_session"):
            return "queued"
        if run.get("tmux_session"):
            return "working" if tmux_alive else "crashed"
        # Fallback (no tmux) run: fall back to wrapper pid liveness.
        return "working" if pid_alive(run.get("wrapper_pid")) else "crashed"
    if conv.get("status") == "parked":
        return "parked-review" if conv.get("anchor") == "mr" else "parked-input"
    if conv.get("status") == "done" and conv.get("last_run"):
        return "finished"
    return conv.get("status") or "idle"


TERMINAL_DERIVED_STATES = frozenset({"finished", "failed", "killed"})


def is_terminal_conversation(conv: dict) -> bool:
    """Return whether a retained run is in a fleet-terminal state."""
    return bool(
        not conv.get("current_run")
        and isinstance(conv.get("last_run"), dict)
        and derived_state(conv, None, False) in TERMINAL_DERIVED_STATES
    )


def session_basename_name(conv: dict) -> str | None:
    """Mirror watcher.tmux_session_name for convs without a recorded session."""
    session_dir = conv.get("session_dir")
    if not session_dir:
        return None
    import re

    safe = re.sub(r"[^A-Za-z0-9_-]", "-", Path(session_dir).name)
    return f"task-{safe}" if safe else None


def model_label(conv: dict) -> str:
    parts = [conv.get("provider") or "?", conv.get("model") or "?"]
    label = ":".join(parts)
    if conv.get("effort"):
        label += f":{conv['effort']}"
    return label


def resume_handle(conv: dict) -> str:
    """Return the provider-specific handle accepted by its interactive CLI."""
    if conv.get("provider") == "pi":
        return conv.get("session_file") or conv.get("session_id") or ""
    return conv.get("session_id") or conv.get("session_file") or ""


def run_completed_at(run: dict) -> float | None:
    completed_at = run.get("completed_at")
    if completed_at:
        try:
            return float(completed_at)
        except (TypeError, ValueError):
            pass
    result_path = run.get("result_path")
    if not result_path:
        return None
    path = Path(result_path)
    try:
        result = json.loads(path.read_text())
        if result.get("completed_at"):
            return float(result["completed_at"])
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def is_active(conv: dict) -> bool:
    return (
        bool(conv.get("current_run"))
        or conv.get("status") in ("working", "parked")
        or (conv.get("status") == "done" and bool(conv.get("last_run")))
    )


def build_rows() -> list[dict]:
    state = load_state()
    live = live_tmux_sessions()
    running = running_tmux_sessions()
    rows: list[dict] = []
    for project_key, proj in state.get("projects", {}).items():
        for conv_key, conv in proj.get("conversations", {}).items():
            if not is_active(conv):
                continue
            run = conv.get("current_run") or None
            observed_run = run or conv.get("last_run") or {}
            name = observed_run.get("tmux_session") or session_basename_name(conv)
            tmux_alive = bool(name and name in live)
            derived = derived_state(
                conv,
                run,
                bool(name and name in running),
            )
            journal_path = observed_run.get("journal_path") or ""
            journal_line, discovered_session = journal_status(journal_path)
            legacy_stream = observed_run.get("stream_path") or ""
            pi_session = conv.get("session_file") or discovered_session or ""
            preview_path = pi_session if conv.get("provider") == "pi" else legacy_stream
            rows.append({
                "identity": f"{project_key}:{conv_key}",
                "key": name or "task-?",
                "surface": "gitlab",
                "status": conv.get("status") or "unknown",
                "derived": derived,
                "model": model_label(conv),
                "provider": conv.get("provider") or "",
                "model_id": conv.get("model") or "",
                "effort": conv.get("effort") or "",
                "session": resume_handle(conv) or pi_session,
                "tmux_alive": tmux_alive,
                "log": preview_path,
                "journal": journal_path,
                "last_line": journal_line or tail_last_line(legacy_stream),
                "url": conv.get("issue_url") or conv.get("mr_url") or "",
                "cwd": conv.get("cwd") or "",
                "started_at": observed_run.get("started_at"),
                "finished_at": (
                    run_completed_at(observed_run)
                    if derived == "finished"
                    else None
                ),
                "run_id": observed_run.get("run_id") or "",
            })
    # Running first, then parked; stable within group.
    order = {
        "working": 0,
        "finishing": 1,
        "queued": 2,
        "crashed": 3,
        "finished": 4,
    }
    rows.sort(key=lambda r: order.get(r["derived"], 5))
    return rows


DOT = {
    "working": "●",
    "finishing": "●",
    "finished": "●",
    "queued": "●",
    "crashed": "●",
    "parked-input": "●",
    "parked-review": "●",
}


def print_table(rows: list[dict]) -> None:
    if not rows:
        print("no active conversations")
        return
    for r in rows:
        dot = DOT.get(r["derived"], "○")
        head = f"{dot} {r['key']:<34} {r['derived']:<13} {r['model']:<22}"
        print(head)
        if r["last_line"]:
            print(f"    {r['last_line']}")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    rows = build_rows()
    if "--json" in args:
        json.dump(rows, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print_table(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
