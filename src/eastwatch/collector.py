"""Bounded provider stream collectors and versioned run journals."""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Callable

JOURNAL_VERSION = 1
DELTA_TAIL_BYTES = 256 * 1024
PROCESS_TAIL_BYTES = 64 * 1024
LINE_BUFFER_BYTES = 32 * 1024 * 1024
PI_THIN_TAIL_CHARS = 200
PI_FRAGMENT_RATIO = 3
STATUS_LINE_RE = re.compile(r"(?im)^[ \t]*STATUS:[ \t]*(?:done|parked)\b.*$")
_AUTH_ERROR = b"No API key"


class RunJournal:
    """Append small, harness-neutral lifecycle facts to ``run.jsonl``."""

    def __init__(
        self,
        path: str | Path | None,
        run_id: str,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.path = Path(path) if path else None
        self.run_id = run_id
        self.clock = clock

    def emit(self, fact: str, **fields: object) -> None:
        if self.path is None:
            return
        payload = {
            "v": JOURNAL_VERSION,
            "ts": self.clock(),
            "run_id": self.run_id,
            "type": fact,
            **fields,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
                    + "\n"
                )
        except OSError:
            # Journaling is diagnostic. It must never take down the worker.
            return


class _ByteTail:
    def __init__(self, capacity: int):
        self.capacity = capacity
        self.data = bytearray()
        self.truncated = False

    def append(self, data: bytes) -> None:
        if not data:
            return
        if len(data) >= self.capacity:
            self.data[:] = data[-self.capacity :]
            self.truncated = True
            return
        overflow = len(self.data) + len(data) - self.capacity
        if overflow > 0:
            del self.data[:overflow]
            self.truncated = True
        self.data.extend(data)

    def text(self) -> str:
        return bytes(self.data).decode(errors="replace")


class _RawCapture:
    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self.stream = None
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.stream = self.path.open("ab")
            except OSError:
                self.stream = None

    def append(self, data: bytes) -> None:
        if self.stream is None or not data:
            return
        try:
            self.stream.write(data)
            self.stream.flush()
        except OSError:
            pass

    def close(self) -> None:
        if self.stream is not None:
            try:
                self.stream.close()
            except OSError:
                pass
            self.stream = None
        if self.path is None or not self.path.is_file():
            return
        destination = self.path.with_suffix(self.path.suffix + ".gz")
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        try:
            with (
                self.path.open("rb") as source,
                gzip.open(temporary, "wb") as compressed,
            ):
                shutil.copyfileobj(source, compressed, length=1024 * 1024)
            os.replace(temporary, destination)
            self.path.unlink()
        except OSError:
            try:
                temporary.unlink()
            except OSError:
                pass


class StreamCollector:
    """Common bounded byte tails, raw capture, and incremental line framing."""

    def __init__(
        self,
        journal: RunJournal,
        *,
        raw_capture_path: str | Path | None = None,
        line_buffer_bytes: int = LINE_BUFFER_BYTES,
    ):
        self.journal = journal
        self.stdout_tail = _ByteTail(PROCESS_TAIL_BYTES)
        self.stderr_tail = _ByteTail(PROCESS_TAIL_BYTES)
        self.line_buffer_bytes = line_buffer_bytes
        self._line = bytearray()
        self._discarding_line = False
        self._raw_capture = _RawCapture(raw_capture_path)
        self.malformed_lines = 0
        self.oversized_lines = 0
        self._reported_malformed_lines = 0
        self._reported_oversized_lines = 0
        self._closed = False
        self._terminal_fact: str | None = None

    @property
    def stdout_text(self) -> str:
        return self.stdout_tail.text()

    @property
    def stderr_text(self) -> str:
        return self.stderr_tail.text()

    @property
    def line_buffer_size(self) -> int:
        return len(self._line)

    def feed(self, data: bytes) -> None:
        if not data:
            return
        self.stdout_tail.append(data)
        self._raw_capture.append(data)
        self._feed_lines(data)

    def feed_stderr(self, data: bytes) -> None:
        if not data:
            return
        self.stderr_tail.append(data)
        self._raw_capture.append(data)

    def _feed_lines(self, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            if self._discarding_line:
                newline = data.find(b"\n", offset)
                if newline < 0:
                    return
                self._discarding_line = False
                offset = newline + 1
                continue

            newline = data.find(b"\n", offset)
            end = len(data) if newline < 0 else newline
            segment = data[offset:end]
            if len(self._line) + len(segment) > self.line_buffer_bytes:
                self._line.clear()
                self._discarding_line = newline < 0
                self.oversized_lines += 1
                self.invalid_line("oversized")
                self._report_diagnostic("oversized_line", self.oversized_lines)
            else:
                self._line.extend(segment)
                if newline >= 0:
                    self._consume_line(bytes(self._line))
                    self._line.clear()
            if newline < 0:
                return
            offset = newline + 1

    def _consume_line(self, line: bytes) -> None:
        if not line.strip():
            return
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self._record_malformed_line()
            return
        if not isinstance(event, dict):
            self._record_malformed_line()
            return
        self.consume_event(event)

    def _record_malformed_line(self) -> None:
        self.malformed_lines += 1
        self.invalid_line("malformed")
        self._report_diagnostic("malformed_line", self.malformed_lines)

    def _report_diagnostic(self, fact: str, count: int, *, final: bool = False) -> None:
        reported_name = f"_reported_{fact}s"
        reported = getattr(self, reported_name)
        if count == 1 or (final and count != reported):
            self.journal.emit(fact, count=count)
            setattr(self, reported_name, count)

    def invalid_line(self, reason: str) -> None:
        pass

    def consume_event(self, event: dict) -> None:
        raise NotImplementedError

    def finish_stdout(self) -> None:
        if self._line and not self._discarding_line:
            self._consume_line(bytes(self._line))
        self._line.clear()
        self._discarding_line = False

    def mark_exit(self, code: int) -> None:
        self.finish_stdout()
        self._emit_terminal("exit", code=code)

    def mark_timeout(self, timeout_seconds: int) -> None:
        self.finish_stdout()
        self._emit_terminal("timeout", timeout_seconds=timeout_seconds)

    def _emit_terminal(self, fact: str, **fields: object) -> None:
        if self._terminal_fact is not None:
            return
        self._terminal_fact = fact
        self.journal.emit(fact, **fields)

    def prepare_next_process(self) -> None:
        self.finish_stdout()
        self._terminal_fact = None

    def close(self) -> None:
        if self._closed:
            return
        self.finish_stdout()
        self._report_diagnostic("malformed_line", self.malformed_lines, final=True)
        self._report_diagnostic("oversized_line", self.oversized_lines, final=True)
        self._raw_capture.close()
        self._closed = True


class PiStreamCollector(StreamCollector):
    """Bounded projection of Pi ``--mode json`` stdout and stderr."""

    _ACTIVITY_EVENTS = {
        "agent_start",
        "turn_start",
        "message_start",
        "message_update",
        "tool_execution_start",
    }

    def __init__(
        self,
        journal: RunJournal,
        *,
        grace_seconds: float,
        raw_capture_path: str | Path | None = None,
        line_buffer_bytes: int = LINE_BUFFER_BYTES,
    ):
        super().__init__(
            journal,
            raw_capture_path=raw_capture_path,
            line_buffer_bytes=line_buffer_bytes,
        )
        self.grace_seconds = max(0.0, grace_seconds)
        self.last_text = ""
        self.best_earlier = ""
        self.assistant_text_count = 0
        self.delta_tail = _ByteTail(DELTA_TAIL_BYTES)
        self.tool_started = False
        self.attempt_tool_started = False
        self.attempt_safety_unknown = False
        self.auth_error_seen = False
        self.terminal_agent_end = False
        self.deadline: float | None = None
        self.outcome: str | None = None
        self.current_provider: str | None = None
        self.session_id: str | None = None
        self._auth_stdout_overlap = b""
        self._auth_stderr_overlap = b""
        self._reply_journaled = False
        self._session_files: set[str] = set()

    @staticmethod
    def assistant_outcome(message: object) -> str | None:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return None
        if message.get("errorMessage") or message.get("stopReason") in {
            "error",
            "aborted",
        }:
            return "failure"
        if message.get("stopReason") == "stop":
            return "success"
        return None

    @staticmethod
    def _body(text: str) -> str:
        return STATUS_LINE_RE.sub("", text).strip()

    @staticmethod
    def _status_line(text: str) -> str:
        match = STATUS_LINE_RE.search(text or "")
        return match.group(0).strip() if match else ""

    def start_attempt(self, provider: str, attempt: int, total: int) -> None:
        self.prepare_next_process()
        self.cancel_guard()
        self.current_provider = provider
        self.last_text = ""
        self.best_earlier = ""
        self.assistant_text_count = 0
        self.delta_tail = _ByteTail(DELTA_TAIL_BYTES)
        self.attempt_tool_started = False
        self.attempt_safety_unknown = False
        self.auth_error_seen = False
        self._auth_stdout_overlap = b""
        self._auth_stderr_overlap = b""
        self.journal.emit(
            "provider_attempt",
            provider=provider,
            attempt=attempt,
            total=total,
        )

    def discover_session(self, session_file: str | None) -> None:
        if not session_file or session_file in self._session_files:
            return
        self._session_files.add(session_file)
        self.journal.emit("session_discovered", session_file=session_file)

    def cancel_guard(self) -> None:
        self.terminal_agent_end = False
        self.deadline = None
        self.outcome = None

    def remaining(self, now: float) -> float | None:
        return None if self.deadline is None else self.deadline - now

    def feed(self, data: bytes) -> None:
        self._probe_auth(data, stdout=True)
        super().feed(data)

    def feed_stderr(self, data: bytes) -> None:
        self._probe_auth(data, stdout=False)
        super().feed_stderr(data)

    def _probe_auth(self, data: bytes, *, stdout: bool) -> None:
        overlap = self._auth_stdout_overlap if stdout else self._auth_stderr_overlap
        probe = overlap + data
        if _AUTH_ERROR in probe:
            self.auth_error_seen = True
        keep = len(_AUTH_ERROR) - 1
        overlap = probe[-keep:] if keep else b""
        if stdout:
            self._auth_stdout_overlap = overlap
        else:
            self._auth_stderr_overlap = overlap

    def invalid_line(self, reason: str) -> None:
        self.attempt_safety_unknown = True

    def consume_event(self, event: dict) -> None:
        event_type = event.get("type")
        if (
            self.terminal_agent_end or self.deadline is not None
        ) and event_type in self._ACTIVITY_EVENTS:
            self.cancel_guard()
        elif event_type in {"agent_start", "turn_start", "message_start"}:
            self.outcome = None

        if event_type == "message_update":
            update = event.get("assistantMessageEvent") or {}
            if update.get("type") == "text_delta" and update.get("delta"):
                self.delta_tail.append(str(update["delta"]).encode())
            return

        if event_type == "message_end":
            message = event.get("message") or {}
            if message.get("role") != "assistant":
                return
            self.outcome = self.assistant_outcome(message)
            texts = [
                str(block.get("text") or "")
                for block in message.get("content", []) or []
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            if not any(texts):
                return
            text = "".join(texts)
            if self.assistant_text_count:
                if len(self._body(self.last_text)) > len(self._body(self.best_earlier)):
                    self.best_earlier = self.last_text
            self.last_text = text
            self.assistant_text_count += 1
            return

        if event_type == "tool_execution_start":
            self.attempt_tool_started = True
            if not self.tool_started:
                self.tool_started = True
                self.journal.emit(
                    "tool_first_started", tool=str(event.get("toolName") or "")
                )
            return

        if event_type == "agent_end":
            will_retry = event.get("willRetry")
            if will_retry is not False:
                self.cancel_guard()
            else:
                self.terminal_agent_end = True
                for message in reversed(event.get("messages") or []):
                    if isinstance(message, dict) and message.get("role") == "assistant":
                        self.outcome = self.assistant_outcome(message)
                        break
            self.journal.emit("agent_end", willRetry=will_retry, outcome=self.outcome)
            return

        if event_type == "agent_settled":
            self.journal.emit("agent_settled")
            if self.terminal_agent_end:
                self.deadline = time.time() + self.grace_seconds
                self.journal.emit(
                    "guard_armed",
                    grace_seconds=self.grace_seconds,
                    outcome=self.outcome,
                )

    @property
    def reply(self) -> str:
        if not self.assistant_text_count:
            return self.delta_tail.text().strip()
        reply = self.last_text
        last_body = self._body(reply)
        if len(last_body) <= PI_THIN_TAIL_CHARS and self.assistant_text_count > 1:
            best_body = self._body(self.best_earlier)
            if len(best_body) > PI_THIN_TAIL_CHARS and len(
                best_body
            ) >= PI_FRAGMENT_RATIO * max(len(last_body), 1):
                status = self._status_line(self.best_earlier) or self._status_line(
                    reply
                )
                reply = f"{best_body}\n\n{status}" if status else best_body
        return reply.strip()

    def extract_reply(self) -> str:
        reply = self.reply
        if not self._reply_journaled:
            self.journal.emit(
                "reply_extracted",
                source="message_end"
                if self.assistant_text_count
                else "text_delta_tail",
                delta_truncated=self.delta_tail.truncated,
                chars=len(reply),
            )
            self._reply_journaled = True
        return reply

    def mark_guard_kill(self) -> None:
        self.journal.emit("guard_kill", outcome=self.outcome)


class ClaudeStreamCollector(StreamCollector):
    """Bounded Claude stream collector retaining only the final result event."""

    def __init__(
        self,
        journal: RunJournal,
        *,
        raw_capture_path: str | Path | None = None,
        line_buffer_bytes: int = LINE_BUFFER_BYTES,
    ):
        super().__init__(
            journal,
            raw_capture_path=raw_capture_path,
            line_buffer_bytes=line_buffer_bytes,
        )
        self.result_event: dict | None = None
        self._reply_journaled = False

    def consume_event(self, event: dict) -> None:
        if event.get("type") == "result":
            self.result_event = event

    def result(self) -> dict:
        if self.result_event is None:
            raise IndexError("claude stream did not contain a result event")
        if not self._reply_journaled:
            reply = str(self.result_event.get("result") or "")
            self.journal.emit("reply_extracted", source="result", chars=len(reply))
            self._reply_journaled = True
        return self.result_event
