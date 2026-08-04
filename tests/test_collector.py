import gzip
import json
import tracemalloc
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from eastwatch.collector import (
    DELTA_TAIL_BYTES,
    LINE_BUFFER_BYTES,
    PROCESS_TAIL_BYTES,
    PiStreamCollector,
    RunJournal,
)


def event_line(event: dict) -> bytes:
    return json.dumps(event, separators=(",", ":")).encode() + b"\n"


def assistant_message(text: str, *, stop_reason: str = "stop") -> dict:
    return {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "stopReason": stop_reason,
            "content": [{"type": "text", "text": text}],
        },
    }


def legacy_projection(lines) -> tuple[str, bool]:
    """The deleted full-stream parser's projection, kept as a replay oracle."""
    assistant_messages = []
    deltas = []
    tool_started = False
    for raw_line in lines:
        try:
            event = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "tool_execution_start":
            tool_started = True
        if event.get("type") == "message_update":
            update = event.get("assistantMessageEvent") or {}
            if update.get("type") == "text_delta" and update.get("delta"):
                deltas.append(update["delta"])
        elif event.get("type") == "message_end":
            message = event.get("message") or {}
            if message.get("role") == "assistant":
                texts = [
                    block.get("text", "")
                    for block in message.get("content", []) or []
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                if any(texts):
                    assistant_messages.append("".join(texts))
    if not assistant_messages:
        return "".join(deltas).strip(), tool_started

    import re

    status_re = re.compile(r"(?im)^[ \t]*STATUS:[ \t]*(?:done|parked)\b.*$")
    reply = assistant_messages[-1]
    last_body = status_re.sub("", reply).strip()
    if len(last_body) <= 200 and len(assistant_messages) > 1:
        best = max(
            assistant_messages[:-1],
            key=lambda message: len(status_re.sub("", message).strip()),
        )
        best_body = status_re.sub("", best).strip()
        if len(best_body) > 200 and len(best_body) >= 3 * max(len(last_body), 1):
            best_status = status_re.search(best)
            reply_status = status_re.search(reply)
            status = best_status or reply_status
            reply = f"{best_body}\n\n{status.group(0).strip()}" if status else best_body
    return reply.strip(), tool_started


class PiStreamCollectorTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal_path = Path(self.temp.name) / "run.jsonl"
        self.collector = PiStreamCollector(
            RunJournal(self.journal_path, "run-1"),
            grace_seconds=0.1,
        )

    def feed_events(self, *events: dict) -> None:
        self.collector.feed(b"".join(event_line(event) for event in events))

    def facts(self) -> list[dict]:
        return [json.loads(line) for line in self.journal_path.read_text().splitlines()]

    def test_clean_success_lifecycle_and_journal_version(self):
        self.feed_events(
            assistant_message("complete\nSTATUS: done"),
            {"type": "agent_end", "willRetry": False},
            {"type": "agent_settled"},
        )
        self.assertEqual(self.collector.outcome, "success")
        self.assertIsNotNone(self.collector.deadline)
        self.assertEqual(self.collector.extract_reply(), "complete\nSTATUS: done")
        self.collector.mark_exit(0)
        facts = self.facts()
        self.assertTrue(
            all(fact["v"] == 1 and fact["run_id"] == "run-1" for fact in facts)
        )
        self.assertEqual(
            [fact["type"] for fact in facts],
            ["agent_end", "agent_settled", "guard_armed", "reply_extracted", "exit"],
        )

    def test_assistant_error_and_late_activity_disarms_guard(self):
        self.feed_events(
            assistant_message("failed", stop_reason="error"),
            {"type": "agent_end", "willRetry": False},
            {"type": "agent_settled"},
        )
        self.assertEqual(self.collector.outcome, "failure")
        self.assertIsNotNone(self.collector.deadline)
        self.feed_events({"type": "agent_start"})
        self.assertIsNone(self.collector.deadline)
        self.assertIsNone(self.collector.outcome)

    def test_fragmented_thin_tail_preserves_substantive_answer_and_status(self):
        substantive = "substantive " * 40
        self.feed_events(
            assistant_message(substantive),
            assistant_message("already covered\nSTATUS: done"),
        )
        self.assertEqual(self.collector.reply, substantive.strip() + "\n\nSTATUS: done")

    def test_genuinely_short_final_answer_is_not_displaced(self):
        self.feed_events(
            assistant_message("earlier answer"),
            assistant_message("final\nSTATUS: done"),
        )
        self.assertEqual(self.collector.reply, "final\nSTATUS: done")

    def test_new_provider_attempt_resets_reply_but_keeps_global_tool_evidence(self):
        self.collector.start_attempt("headroom-copilot", 1, 1)
        self.feed_events(
            assistant_message("failed attempt", stop_reason="error"),
            {"type": "tool_execution_start", "toolName": "bash"},
        )
        self.collector.mark_exit(1)
        self.collector.start_attempt("github-copilot", 1, 1)
        self.feed_events(
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "delta": "direct answer",
                },
            }
        )
        self.assertEqual(self.collector.reply, "direct answer")
        self.assertTrue(self.collector.tool_started)
        self.assertFalse(self.collector.attempt_tool_started)

    def test_delta_ring_overflow_is_bounded_and_marked(self):
        delta = "x" * (DELTA_TAIL_BYTES + 100)
        self.feed_events(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": delta},
            }
        )
        self.assertEqual(len(self.collector.delta_tail.data), DELTA_TAIL_BYTES)
        self.assertTrue(self.collector.delta_tail.truncated)
        self.collector.extract_reply()
        reply_fact = self.facts()[-1]
        self.assertTrue(reply_fact["delta_truncated"])

    def test_malformed_and_oversized_lines_are_dropped(self):
        self.collector.feed(b"not-json\n")
        self.collector.feed(b"x" * (LINE_BUFFER_BYTES + 1) + b"\n")
        self.feed_events(assistant_message("still works"))
        self.assertEqual(self.collector.reply, "still works")
        self.assertEqual(self.collector.malformed_lines, 1)
        self.assertEqual(self.collector.oversized_lines, 1)
        self.assertEqual(self.collector.line_buffer_size, 0)
        self.assertEqual(
            [fact["type"] for fact in self.facts()[:2]],
            ["malformed_line", "oversized_line"],
        )

    def test_repeated_malformed_lines_emit_bounded_diagnostic_facts(self):
        self.collector.feed(b"bad\n" * 10_000)
        self.assertEqual(len(self.facts()), 1)
        self.collector.close()
        facts = self.facts()
        self.assertEqual(len(facts), 2)
        self.assertEqual(facts[-1]["count"], 10_000)
        self.assertTrue(self.collector.attempt_safety_unknown)

    def test_tool_and_auth_flags_include_fragmented_chunks(self):
        tool = event_line({"type": "tool_execution_start", "toolName": "bash"})
        self.collector.feed(tool[:8])
        self.collector.feed(tool[8:])
        self.collector.feed_stderr(b"No API ")
        self.collector.feed_stderr(b"key configured\n")
        self.assertTrue(self.collector.tool_started)
        self.assertTrue(self.collector.attempt_tool_started)
        self.assertTrue(self.collector.auth_error_seen)
        self.assertIn("tool_first_started", [fact["type"] for fact in self.facts()])

    def test_process_tails_are_hard_capped(self):
        self.collector.feed(b"a" * (PROCESS_TAIL_BYTES * 2))
        self.collector.feed_stderr(b"b" * (PROCESS_TAIL_BYTES * 2))
        self.assertEqual(len(self.collector.stdout_tail.data), PROCESS_TAIL_BYTES)
        self.assertEqual(len(self.collector.stderr_tail.data), PROCESS_TAIL_BYTES)

    def test_raw_capture_is_gzipped_on_close(self):
        raw_path = Path(self.temp.name) / "stream.log"
        collector = PiStreamCollector(
            RunJournal(None, "raw"),
            grace_seconds=1,
            raw_capture_path=raw_path,
        )
        collector.feed(event_line(assistant_message("answer")))
        collector.close()
        self.assertFalse(raw_path.exists())
        self.assertTrue(Path(str(raw_path) + ".gz").is_file())
        with gzip.open(str(raw_path) + ".gz", "rt") as stream:
            self.assertIn("message_end", stream.read())

    def test_500_mb_spam_keeps_state_bounded(self):
        payload = event_line(
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "toolcall_delta",
                    "delta": "x" * (1024 * 1024),
                },
            }
        )
        repetitions = (500 * 1024 * 1024) // len(payload) + 1
        tracemalloc.start()
        try:
            for _ in range(repetitions):
                self.collector.feed(payload)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertGreaterEqual(repetitions * len(payload), 500 * 1024 * 1024)
        self.assertLessEqual(len(self.collector.stdout_tail.data), PROCESS_TAIL_BYTES)
        self.assertLessEqual(self.collector.line_buffer_size, LINE_BUFFER_BYTES)
        self.assertLess(peak, 16 * 1024 * 1024)


class CorpusDifferentialReplayTest(unittest.TestCase):
    def test_failure_corpus_matches_deleted_legacy_projection(self):
        corpus = Path.home() / ".local/state/eastwatch/corpus"
        paths = sorted(corpus.glob("*.stream.log.gz"))
        if not paths:
            self.skipTest(f"no differential corpus at {corpus}")
        self.assertEqual(len(paths), 19)
        for path in paths:
            with self.subTest(path=path.name):
                collector = PiStreamCollector(
                    RunJournal(None, "replay"), grace_seconds=20
                )
                with gzip.open(path, "rb") as stream:
                    for chunk in iter(lambda: stream.read(64 * 1024), b""):
                        collector.feed(chunk)
                collector.finish_stdout()
                with gzip.open(path, "rb") as stream:
                    expected_reply, expected_tool = legacy_projection(stream)
                self.assertEqual(collector.reply, expected_reply)
                self.assertEqual(collector.tool_started, expected_tool)


if __name__ == "__main__":
    unittest.main()
