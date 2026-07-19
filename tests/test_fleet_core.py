"""Tests for the non-visual fleet monitor core."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from eastwatch.fleet.core import (
    FleetLog,
    FleetRow,
    LogCursor,
    RepositoryIdentity,
    chat_name,
    fetch_snapshot,
    follow_log_file,
    fuzzy_filter_rows,
    fuzzy_match_rank,
    heartbeat_age,
    interactive_command,
    parse_rows,
    preserve_selection,
    resolve_repository,
    resume_eligible,
    tmux_chat_command,
)


def row(**overrides) -> FleetRow:
    values = {
        "identity": "gitlab.example/repo:62",
        "key": "task-repo-62",
        "surface": "gitlab",
        "status": "parked",
        "derived": "parked-input",
        "model": "claude:sonnet",
        "provider": "claude",
        "model_id": "sonnet",
        "session": "abc-123",
        "tmux_alive": False,
        "log": "/tmp/stream.log",
        "url": "https://gitlab.example/repo/-/issues/62",
        "cwd": "/tmp/repo with spaces",
    }
    values.update(overrides)
    return FleetRow(**values)


class FleetRowsTest(unittest.TestCase):
    def test_parse_sorts_attention_first_and_preserves_explicit_metadata(self):
        payload = [
            {
                "identity": "working",
                "key": "task-z",
                "derived": "working",
                "model": "pi:gpt",
                "provider": "pi",
                "model_id": "gpt",
                "tmux_alive": True,
                "finished_at": 456.0,
            },
            {
                "identity": "crashed",
                "key": "task-a",
                "derived": "crashed",
                "model": "claude:opus",
                "provider": "claude",
                "model_id": "opus",
            },
        ]
        rows = parse_rows(payload)
        self.assertEqual([item.identity for item in rows], ["crashed", "working"])
        self.assertEqual(rows[1].provider, "pi")
        self.assertEqual(rows[1].model_id, "gpt")
        self.assertEqual(rows[1].finished_at, 456.0)

    def test_old_contract_derives_provider_and_model_id(self):
        parsed = FleetRow.from_mapping({"key": "task-x", "model": "claude:opus:high"})
        self.assertEqual(parsed.provider, "claude")
        self.assertEqual(parsed.model_id, "opus")
        self.assertEqual(parsed.identity, "task-x")

    def test_selection_survives_refresh_or_falls_to_first(self):
        rows = (row(identity="a"), row(identity="b", key="task-b"))
        self.assertEqual(preserve_selection(rows, "b"), "b")
        self.assertEqual(preserve_selection(rows, "gone"), "a")
        self.assertIsNone(preserve_selection((), "a"))

    def test_parse_rejects_non_array_contract(self):
        with self.assertRaisesRegex(ValueError, "array"):
            parse_rows({"rows": []})

    def test_fuzzy_match_is_case_insensitive_and_prefers_substrings(self):
        self.assertEqual(fuzzy_match_rank("Task-Repo-62", "REPO"), (0, 5, 4))
        self.assertEqual(fuzzy_match_rank("a---b---c", "abc"), (1, 9, 0))
        self.assertIsNone(fuzzy_match_rank("repo", "worker"))

    def test_fuzzy_filter_ranks_direct_matches_before_subsequences(self):
        rows = (
            row(identity="subsequence", key="task-first", model="a-b-c"),
            row(identity="direct", key="task-second", model="ABC"),
        )
        self.assertEqual(
            [item.identity for item in fuzzy_filter_rows(rows, "abc")],
            ["direct", "subsequence"],
        )

    def test_fuzzy_filter_searches_operational_fields_and_preserves_ties(self):
        first = row(identity="first", key="task-alpha", cwd="/tmp/eastwatch")
        second = row(identity="second", key="task-beta", cwd="/tmp/eastwatch")
        self.assertEqual(fuzzy_filter_rows((first, second), "EAST"), (first, second))
        self.assertEqual(fuzzy_filter_rows((first, second), ""), (first, second))
        self.assertEqual(fuzzy_filter_rows((first, second), "no-such-row"), ())

    def test_fuzzy_filter_includes_derived_repository_labels(self):
        first = row(identity="first", key="task-alpha", cwd="/worktrees/slice-a")
        second = row(identity="second", key="task-beta", cwd="/worktrees/slice-b")
        self.assertEqual(
            fuzzy_filter_rows(
                (first, second),
                "cobalt",
                extra_values={"second": ("Cobalt",)},
            ),
            (second,),
        )

    @mock.patch("eastwatch.fleet.core.subprocess.run")
    def test_repository_identity_groups_linked_worktrees(self, run):
        run.return_value = mock.Mock(
            returncode=0,
            stdout="/repos/eastwatch/.git\n",
        )
        main = resolve_repository("/repos/eastwatch")
        worktree = resolve_repository("/worktrees/eastwatch/issue-29")
        self.assertEqual(
            main,
            RepositoryIdentity("/repos/eastwatch/.git", "eastwatch"),
        )
        self.assertEqual(worktree, main)
        self.assertEqual(run.call_count, 2)

    @mock.patch("eastwatch.fleet.core.subprocess.run")
    def test_repository_identity_has_unknown_fallback(self, run):
        run.return_value = mock.Mock(returncode=128, stdout="")
        self.assertEqual(resolve_repository("/missing/repo").label, "Unknown")
        self.assertEqual(resolve_repository("").label, "Unknown")
        run.assert_called_once()


class FleetLogTest(unittest.TestCase):
    def test_claude_text_thinking_tool_and_result(self):
        log = FleetLog("claude")
        events = [
            {"type": "assistant", "message": {"content": [{"type": "thinking"}]}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "git status"},
                        }
                    ]
                },
            },
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
            {"type": "result", "subtype": "success"},
        ]
        for event in events:
            self.assertTrue(log.feed_line(json.dumps(event)))
        self.assertEqual(
            log.lines,
            ("· thinking…", "→ Bash git status", "done", "── success"),
        )

    def test_pi_deltas_form_one_line_and_message_end_is_not_duplicated(self):
        log = FleetLog("pi")
        events = [
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": "HEL"},
            },
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": "LO"},
            },
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_end"},
            },
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "HELLO"}],
                },
            },
            {"type": "agent_end"},
        ]
        for event in events:
            log.feed_line(json.dumps(event))
        self.assertEqual(log.lines, ("HELLO", "── done"))

    def test_pi_message_end_renders_when_tail_started_after_deltas(self):
        log = FleetLog("pi")
        event = {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "complete answer"}],
            },
        }
        self.assertTrue(log.feed_line(json.dumps(event)))
        self.assertEqual(log.lines, ("complete answer",))

    def test_pi_session_file_messages_render_text_and_tool_calls(self):
        log = FleetLog("pi")
        event = {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "working"},
                    {"type": "toolCall", "name": "bash", "arguments": {"command": "git status"}},
                ],
            },
        }
        self.assertTrue(log.feed_line(json.dumps(event)))
        self.assertEqual(log.lines, ("working", "→ bash git status"))

    def test_bounds_completed_lines_and_partial_text(self):
        log = FleetLog("claude", max_lines=2, max_chars=12)
        for text in ("one", "two", "three"):
            event = {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
            log.feed_line(json.dumps(event))
        self.assertEqual(log.lines, ("two", "three"))

    def test_invalid_or_noise_event_is_ignored(self):
        log = FleetLog("claude")
        self.assertFalse(log.feed_line("not json"))
        self.assertFalse(log.feed_line('{"type":"system"}'))
        self.assertEqual(log.lines, ())


class ResumeCommandTest(unittest.TestCase):
    def test_claude_and_pi_commands_use_raw_model_and_argument_arrays(self):
        self.assertEqual(
            interactive_command(row()),
            ("claude", "--resume", "abc-123", "--model", "sonnet"),
        )
        self.assertEqual(
            interactive_command(row(effort="high")),
            (
                "claude",
                "--resume",
                "abc-123",
                "--model",
                "sonnet",
                "--effort",
                "high",
            ),
        )
        pi = row(provider="pi", model_id="gpt-5.5", session="/tmp/pi session.json")
        self.assertEqual(interactive_command(pi), ("pi", "--session", "/tmp/pi session.json"))

    def test_only_parked_or_crashed_nonlive_rows_resume(self):
        self.assertTrue(resume_eligible(row()))
        self.assertTrue(resume_eligible(row(derived="crashed")))
        self.assertTrue(
            resume_eligible(
                row(
                    status="done",
                    derived="finished",
                    tmux_alive=True,
                )
            )
        )
        self.assertFalse(resume_eligible(row(derived="working", tmux_alive=True)))
        self.assertFalse(resume_eligible(row(session="")))
        with self.assertRaisesRegex(ValueError, "not a resumable"):
            interactive_command(row(derived="working", tmux_alive=True))

    def test_tmux_command_quotes_only_nested_shell_command(self):
        command = tmux_chat_command(row())
        self.assertEqual(command[:5], ("tmux", "new-window", "-n", "chat-task-repo-62", "-c"))
        self.assertEqual(command[5], "/tmp/repo with spaces")
        self.assertEqual(command[6], "claude --resume abc-123 --model sonnet")
        pane = tmux_chat_command(row(), pane=True)
        self.assertEqual(pane[:4], ("tmux", "split-window", "-h", "-c"))

    def test_chat_name_is_sanitized_and_capped(self):
        name = chat_name(row(key="task:repo." + "x" * 80))
        self.assertNotIn(":", name)
        self.assertNotIn(".", name)
        self.assertLessEqual(len(name), 40)


class SnapshotAndTailTest(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_snapshot_success_and_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state.json"
            state.write_text("{}")
            script = root / "fleet-status"
            script.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "print(json.dumps([{'identity':'x','key':'task-x','derived':'working'}]))\n"
            )
            script.chmod(0o755)

            snapshot = await fetch_snapshot(script, state_path=state)
            self.assertIsNone(snapshot.error)
            self.assertEqual(snapshot.rows[0].identity, "x")

            script.write_text("#!/bin/sh\nsleep 5\n")
            script.chmod(0o755)
            started = time.monotonic()
            snapshot = await fetch_snapshot(script, timeout_s=0.05, state_path=state)
            self.assertIn("timed out", snapshot.error or "")
            self.assertLess(time.monotonic() - started, 2)

    async def test_fetch_snapshot_surfaces_exit_and_contract_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fleet-status"
            script.write_text("#!/bin/sh\necho broken >&2\nexit 7\n")
            script.chmod(0o755)
            snapshot = await fetch_snapshot(script)
            self.assertIn("exited 7: broken", snapshot.error or "")

            script.write_text("#!/bin/sh\nprintf '{\"rows\":[]}'\n")
            script.chmod(0o755)
            snapshot = await fetch_snapshot(script)
            self.assertIn("must be an array", snapshot.error or "")

    async def test_follow_log_reads_existing_and_appended_complete_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stream.log"
            path.write_text('{"n":1}\n{"n":')
            seen: list[str] = []
            enough = asyncio.Event()

            async def collect(line: str) -> None:
                seen.append(line)
                if len(seen) == 2:
                    enough.set()

            task = asyncio.create_task(
                follow_log_file(path, collect, poll_interval_s=0.01)
            )
            await asyncio.sleep(0.03)
            with path.open("a") as stream:
                stream.write("2}\n")
            await asyncio.wait_for(enough.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(seen, ['{"n":1}', '{"n":2}'])

    async def test_follow_log_cursor_resumes_without_replaying_cached_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stream.log"
            path.write_text("first\n")
            cursor = LogCursor()
            seen: list[str] = []

            async def collect(line: str) -> None:
                seen.append(line)

            first = asyncio.create_task(
                follow_log_file(
                    path,
                    collect,
                    poll_interval_s=0.01,
                    initial_bytes=None,
                    cursor=cursor,
                )
            )
            for _ in range(50):
                if seen == ["first"]:
                    break
                await asyncio.sleep(0.01)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first

            with path.open("a") as stream:
                stream.write("second\n")
            second = asyncio.create_task(
                follow_log_file(
                    path,
                    collect,
                    poll_interval_s=0.01,
                    initial_bytes=None,
                    cursor=cursor,
                )
            )
            for _ in range(50):
                if seen == ["first", "second"]:
                    break
                await asyncio.sleep(0.01)
            second.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await second

            self.assertEqual(seen, ["first", "second"])

    async def test_follow_log_handles_rotation_and_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stream.log"
            path.write_text("first\n")
            seen: list[str] = []
            enough = asyncio.Event()

            async def collect(line: str) -> None:
                seen.append(line)
                if len(seen) == 3:
                    enough.set()

            task = asyncio.create_task(
                follow_log_file(path, collect, poll_interval_s=0.01)
            )
            await asyncio.sleep(0.03)
            path.rename(path.with_suffix(".old"))
            path.write_text("second\n")
            await asyncio.sleep(0.03)
            path.write_text("third\n")
            await asyncio.wait_for(enough.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(seen, ["first", "second", "third"])

    async def test_follow_log_drops_cut_and_oversized_partial_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stream.log"
            path.write_text("x" * 40 + "\nsecond\nthird\n")
            seen: list[str] = []
            enough = asyncio.Event()

            async def collect(line: str) -> None:
                seen.append(line)
                if line == "valid":
                    enough.set()

            task = asyncio.create_task(
                follow_log_file(
                    path,
                    collect,
                    poll_interval_s=0.01,
                    initial_bytes=8,
                    max_pending_bytes=8,
                )
            )
            await asyncio.sleep(0.03)
            with path.open("a") as stream:
                stream.write("oversized-partial")
                stream.flush()
            await asyncio.sleep(0.03)
            with path.open("a") as stream:
                stream.write("\nvalid\n")
            await asyncio.wait_for(enough.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(seen, ["third", "valid"])

    def test_heartbeat_age_uses_state_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{}")
            os.utime(path, (100, 100))
            self.assertEqual(heartbeat_age(path, now=142), 42)
            self.assertIsNone(heartbeat_age(Path(tmp) / "missing", now=142))


if __name__ == "__main__":
    unittest.main()
