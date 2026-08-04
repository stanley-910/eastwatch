"""Shared, non-visual core for eastwatch fleet tools."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Sequence

from eastwatch.env import getenv


Command = str | Path | Sequence[str]


def command_argv(command: Command) -> list[str]:
    if isinstance(command, (str, Path)):
        return [str(command)]
    return [str(part) for part in command]


STATE_ORDER = {
    "crashed": 0,
    "parked-input": 1,
    "working": 2,
    "finishing": 3,
    "finished": 4,
    "queued": 5,
    "parked-review": 6,
}
RESUMABLE_STATES = frozenset({"crashed", "finished", "parked-input", "parked-review"})


@dataclass(frozen=True, slots=True)
class FleetRow:
    identity: str
    key: str
    surface: str
    status: str
    derived: str
    model: str
    provider: str
    model_id: str
    session: str
    tmux_alive: bool
    log: str
    url: str
    cwd: str
    started_at: float | None = None
    finished_at: float | None = None
    last_line: str = ""
    effort: str = ""
    run_id: str = ""
    journal: str = ""
    trace_source: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> FleetRow:
        display_model = str(raw.get("model") or "?")
        model_parts = display_model.split(":")
        provider = str(raw.get("provider") or (model_parts[0] if model_parts else "?"))
        model_id = str(
            raw.get("model_id") or (model_parts[1] if len(model_parts) > 1 else "?")
        )
        key = str(raw.get("key") or "task-?")
        return cls(
            identity=str(raw.get("identity") or key),
            key=key,
            surface=str(raw.get("surface") or "unknown"),
            status=str(raw.get("status") or "unknown"),
            derived=str(raw.get("derived") or "unknown"),
            model=display_model,
            provider=provider,
            model_id=model_id,
            session=str(raw.get("session") or ""),
            tmux_alive=bool(raw.get("tmux_alive")),
            log=str(raw.get("log") or ""),
            url=str(raw.get("url") or ""),
            cwd=str(raw.get("cwd") or ""),
            started_at=float(raw["started_at"]) if raw.get("started_at") else None,
            finished_at=float(raw["finished_at"]) if raw.get("finished_at") else None,
            last_line=str(raw.get("last_line") or ""),
            effort=str(raw.get("effort") or ""),
            run_id=str(raw.get("run_id") or ""),
            journal=str(raw.get("journal") or ""),
            trace_source=str(raw.get("trace_source") or ""),
        )


@dataclass(frozen=True, slots=True)
class FleetSnapshot:
    rows: tuple[FleetRow, ...]
    refreshed_at: float
    heartbeat_age_s: float | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    key: str
    label: str


UNKNOWN_REPOSITORY = RepositoryIdentity("unknown", "Unknown")


def resolve_repository(cwd: str) -> RepositoryIdentity:
    """Resolve linked worktrees to one cached Git common-directory identity."""
    if not cwd:
        return UNKNOWN_REPOSITORY
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                cwd,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return UNKNOWN_REPOSITORY
    common_dir_text = result.stdout.strip()
    if result.returncode or not common_dir_text:
        return UNKNOWN_REPOSITORY
    common_dir = Path(common_dir_text)
    if not common_dir.is_absolute():
        common_dir = Path(cwd) / common_dir
    try:
        common_dir = common_dir.resolve()
    except OSError:
        return UNKNOWN_REPOSITORY
    root = common_dir.parent if common_dir.name == ".git" else common_dir
    label = root.name.removesuffix(".git") or "Unknown"
    return RepositoryIdentity(str(common_dir), label)


def sort_rows(rows: Sequence[FleetRow]) -> tuple[FleetRow, ...]:
    return tuple(
        sorted(
            rows,
            key=lambda row: (STATE_ORDER.get(row.derived, 9), row.key.casefold()),
        )
    )


def parse_rows(payload: object) -> tuple[FleetRow, ...]:
    if not isinstance(payload, list):
        raise ValueError("fleet-status JSON must be an array")
    if not all(isinstance(item, dict) for item in payload):
        raise ValueError("fleet-status rows must be objects")
    return sort_rows([FleetRow.from_mapping(item) for item in payload])


def preserve_selection(
    rows: Sequence[FleetRow], selected_identity: str | None
) -> str | None:
    if not rows:
        return None
    if selected_identity and any(row.identity == selected_identity for row in rows):
        return selected_identity
    return rows[0].identity


def fuzzy_match_rank(value: str, query: str) -> tuple[int, int, int] | None:
    """Rank a case-insensitive substring or subsequence match."""
    value = value.casefold()
    query = query.casefold().strip()
    if not query:
        return (0, 0, 0)
    substring_at = value.find(query)
    if substring_at >= 0:
        return (0, substring_at, len(query))

    positions: list[int] = []
    search_from = 0
    for character in query:
        position = value.find(character, search_from)
        if position < 0:
            return None
        positions.append(position)
        search_from = position + 1
    return (1, positions[-1] - positions[0] + 1, positions[0])


def fuzzy_filter_rows(
    rows: Sequence[FleetRow],
    query: str,
    *,
    extra_values: Mapping[str, Sequence[str]] | None = None,
) -> tuple[FleetRow, ...]:
    """Filter fleet rows by operational text, with direct matches first."""
    query = query.strip()
    if not query:
        return tuple(rows)

    ranked: list[tuple[tuple[int, int, int], int, FleetRow]] = []
    for index, row in enumerate(rows):
        search_values = (
            row.identity,
            row.key,
            row.key.removeprefix("task-"),
            row.derived,
            row.status,
            row.model,
            row.provider,
            row.model_id,
            row.cwd,
            Path(row.cwd).name,
            row.url,
            row.last_line,
            *(extra_values or {}).get(row.identity, ()),
        )
        matches = [
            rank
            for value in search_values
            if (rank := fuzzy_match_rank(value, query)) is not None
        ]
        if matches:
            ranked.append((min(matches), index, row))
    ranked.sort(key=lambda match: (match[0], match[1]))
    return tuple(match[2] for match in ranked)


def state_dir() -> Path:
    configured = getenv("EASTWATCH_STATE_DIR")
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".local/state/eastwatch"
    )


def heartbeat_age(
    path: Path | None = None, *, now: float | None = None
) -> float | None:
    state_path = path or state_dir() / "state.json"
    try:
        modified = state_path.stat().st_mtime
    except OSError:
        return None
    return max(0.0, (time.time() if now is None else now) - modified)


async def fetch_snapshot(
    fleet_status: Command,
    *,
    timeout_s: float = 6.0,
    state_path: Path | None = None,
) -> FleetSnapshot:
    refreshed_at = time.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            *command_argv(fleet_status),
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return FleetSnapshot(
            (), refreshed_at, heartbeat_age(state_path), f"fleet-status: {exc}"
        )

    communicate = asyncio.create_task(proc.communicate())
    try:
        done, _ = await asyncio.wait({communicate}, timeout=timeout_s)
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        await communicate
        raise
    if not done:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        await communicate
        return FleetSnapshot(
            (),
            refreshed_at,
            heartbeat_age(state_path),
            f"fleet-status timed out after {timeout_s:g}s",
        )
    stdout, stderr = communicate.result()

    if proc.returncode:
        detail = stderr.decode(errors="replace").strip()
        message = f"fleet-status exited {proc.returncode}"
        if detail:
            message += f": {detail[-240:]}"
        return FleetSnapshot((), refreshed_at, heartbeat_age(state_path), message)

    try:
        rows = parse_rows(json.loads(stdout))
    except (json.JSONDecodeError, ValueError) as exc:
        return FleetSnapshot(
            (), refreshed_at, heartbeat_age(state_path), f"fleet-status: {exc}"
        )
    return FleetSnapshot(rows, refreshed_at, heartbeat_age(state_path))


@dataclass(frozen=True, slots=True)
class RenderChunk:
    text: str
    end_line: bool = True


def _tool_detail(args: object) -> str:
    if not isinstance(args, dict):
        return ""
    for key in ("command", "file_path", "path", "file", "pattern", "query", "action"):
        if args.get(key):
            return str(args[key]).replace("\n", " ")[:120]
    return ""


class FleetLog:
    """Parsed display model for Claude and pi JSONL streams, optionally bounded."""

    def __init__(
        self,
        provider: str,
        *,
        max_lines: int | None = 500,
        max_chars: int | None = 200_000,
    ):
        self.provider = provider
        self.max_lines = max_lines
        self.max_chars = max_chars
        self._lines: deque[str] = deque()
        self._partial = ""
        self._chars = 0
        self._pi_saw_delta = False

    @property
    def lines(self) -> tuple[str, ...]:
        if self._partial:
            return (*self._lines, self._partial)
        return tuple(self._lines)

    def clear(self) -> None:
        self._lines.clear()
        self._partial = ""
        self._chars = 0
        self._pi_saw_delta = False

    def set_limits(
        self,
        *,
        max_lines: int | None,
        max_chars: int | None,
    ) -> None:
        self.max_lines = max_lines
        self.max_chars = max_chars
        self._trim()

    def feed_line(self, raw_line: str) -> bool:
        try:
            event = json.loads(raw_line)
        except (json.JSONDecodeError, ValueError):
            return False
        if not isinstance(event, dict):
            return False
        chunks = (
            self._render_pi(event)
            if self.provider == "pi"
            else self._render_claude(event)
        )
        for chunk in chunks:
            self._feed_chunk(chunk)
        return bool(chunks)

    def _render_claude(self, event: dict) -> list[RenderChunk]:
        event_type = event.get("type")
        if event_type == "assistant":
            chunks: list[RenderChunk] = []
            for block in (event.get("message") or {}).get("content", []):
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text" and block.get("text"):
                    chunks.append(RenderChunk(str(block["text"])))
                elif block_type == "thinking":
                    chunks.append(RenderChunk("· thinking…"))
                elif block_type == "tool_use":
                    detail = _tool_detail(block.get("input"))
                    chunks.append(
                        RenderChunk(f"→ {block.get('name', '?')} {detail}".rstrip())
                    )
            return chunks
        if event_type == "result":
            return [RenderChunk(f"── {event.get('subtype', 'done')}")]
        return []

    def _render_pi(self, event: dict) -> list[RenderChunk]:
        event_type = event.get("type")
        if event_type == "message":
            message = event.get("message") or {}
            if message.get("role") != "assistant":
                return []
            chunks: list[RenderChunk] = []
            for block in message.get("content", []) or []:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text" and block.get("text"):
                    chunks.append(RenderChunk(str(block["text"])))
                elif block_type == "toolCall":
                    detail = _tool_detail(block.get("arguments"))
                    chunks.append(
                        RenderChunk(f"→ {block.get('name', '?')} {detail}".rstrip())
                    )
            return chunks
        if event_type == "message_update":
            update = event.get("assistantMessageEvent") or {}
            update_type = update.get("type")
            if update_type == "text_delta" and update.get("delta"):
                self._pi_saw_delta = True
                return [RenderChunk(str(update["delta"]), end_line=False)]
            if update_type == "text_end":
                return [RenderChunk("", end_line=True)]
            if update_type == "thinking_start":
                return [RenderChunk("· thinking…")]
            return []
        if event_type == "tool_execution_start":
            detail = _tool_detail(event.get("args"))
            return [RenderChunk(f"→ {event.get('toolName', '?')} {detail}".rstrip())]
        if event_type == "message_end":
            message = event.get("message") or {}
            if message.get("role") != "assistant" or self._pi_saw_delta:
                self._pi_saw_delta = False
                return []
            texts = [
                str(block.get("text") or "")
                for block in message.get("content", [])
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            text = "".join(texts)
            return [RenderChunk(text)] if text else []
        if event_type == "agent_end":
            return [RenderChunk("── done")]
        return []

    def _feed_chunk(self, chunk: RenderChunk) -> None:
        parts = chunk.text.split("\n")
        for index, part in enumerate(parts):
            self._partial += part
            if index < len(parts) - 1:
                self._commit_partial()
        if chunk.end_line:
            self._commit_partial()
        self._trim()

    def _commit_partial(self) -> None:
        self._lines.append(self._partial)
        self._chars += len(self._partial)
        self._partial = ""

    def _trim(self) -> None:
        while self._lines and (
            (self.max_lines is not None and len(self._lines) > self.max_lines)
            or (
                self.max_chars is not None
                and self._chars + len(self._partial) > self.max_chars
            )
        ):
            self._chars -= len(self._lines.popleft())
        if self.max_chars is not None and len(self._partial) > self.max_chars:
            self._partial = self._partial[-self.max_chars :]


@dataclass
class LogCursor:
    """Persistent byte-level progress for pausing and resuming a log follower."""

    inode: int | None = None
    position: int = 0
    pending: bytes = b""
    discard_first: bool = False


async def follow_log_file(
    path: Path,
    on_line: Callable[[str], Awaitable[None]],
    *,
    poll_interval_s: float = 0.15,
    initial_bytes: int | None = 262_144,
    max_pending_bytes: int = 2_097_152,
    cursor: LogCursor | None = None,
) -> None:
    """Follow a possibly-created or rotated JSONL file without child processes."""

    cursor = cursor or LogCursor()

    while True:
        try:
            stat = path.stat()
        except OSError:
            await asyncio.sleep(poll_interval_s)
            continue

        if cursor.inode != stat.st_ino or stat.st_size < cursor.position:
            cursor.inode = stat.st_ino
            cursor.position = (
                0 if initial_bytes is None else max(0, stat.st_size - initial_bytes)
            )
            cursor.pending = b""
            cursor.discard_first = cursor.position > 0

        if stat.st_size > cursor.position:
            try:
                with path.open("rb") as stream:
                    stream.seek(cursor.position)
                    data = stream.read(stat.st_size - cursor.position)
            except OSError:
                await asyncio.sleep(poll_interval_s)
                continue
            cursor.position += len(data)
            cursor.pending += data
            complete = cursor.pending.split(b"\n")
            cursor.pending = complete.pop()
            if cursor.discard_first and complete:
                complete.pop(0)
                cursor.discard_first = False
            for raw in complete:
                if raw.strip():
                    await on_line(raw.decode(errors="replace"))
            if len(cursor.pending) > max_pending_bytes:
                cursor.pending = b""
                cursor.discard_first = True

        await asyncio.sleep(poll_interval_s)


def resume_eligible(row: FleetRow) -> bool:
    return bool(
        row.session
        and row.derived in RESUMABLE_STATES
        and (row.derived == "finished" or not row.tmux_alive)
    )


def interactive_command(row: FleetRow) -> tuple[str, ...]:
    if not resume_eligible(row):
        raise ValueError(f"{row.key} is not a resumable conversation")
    if row.provider == "claude":
        command = ["claude", "--resume", row.session, "--model", row.model_id]
        if row.effort:
            command.extend(("--effort", row.effort))
        return tuple(command)
    if row.provider == "pi":
        return ("pi", "--session", row.session)
    raise ValueError(f"unsupported provider: {row.provider}")


def chat_name(row: FleetRow) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", row.key)
    return f"chat-{safe}".rstrip("-")[:40]


def tmux_chat_command(
    row: FleetRow,
    *,
    pane: bool = False,
    runner: Sequence[str] | None = None,
) -> tuple[str, ...]:
    command = tuple(runner or interactive_command(row))
    cwd = row.cwd or str(Path.cwd())
    if pane:
        return ("tmux", "split-window", "-h", "-c", cwd, shlex.join(command))
    return ("tmux", "new-window", "-n", chat_name(row), "-c", cwd, shlex.join(command))


def chat_lock_path(session: str, root: Path | None = None) -> Path:
    digest = hashlib.sha256(session.encode()).hexdigest()[:24]
    return (root or state_dir()) / "chats" / f"{digest}.lock"
