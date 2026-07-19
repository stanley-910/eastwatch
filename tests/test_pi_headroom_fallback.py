import atexit
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import load_watcher

TMP_ROOT = Path(tempfile.mkdtemp(prefix="eastwatch-pi-headroom-test-"))
watcher = load_watcher(TMP_ROOT)

atexit.register(lambda: shutil.rmtree(TMP_ROOT, ignore_errors=True))


def provider_from(cmd: list[str]) -> str:
    return cmd[cmd.index("--provider") + 1]


def option_from(cmd: list[str], option: str) -> str:
    return cmd[cmd.index(option) + 1]


def reply_stream(text: str = "finished\nSTATUS: done") -> str:
    return json.dumps({
        "type": "message_end",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    })


class PiHeadroomFallbackTest(unittest.TestCase):
    def make_req(
        self,
        *,
        model: str = "gpt-5.3-codex",
        effort: str = "high",
        is_new: bool = True,
        session_id: str | None = None,
    ) -> tuple[dict, Path]:
        run_dir = Path(tempfile.mkdtemp(dir=TMP_ROOT))
        self.addCleanup(lambda: shutil.rmtree(run_dir, ignore_errors=True))
        session_file = None
        if not is_new:
            session_file = run_dir / "existing.jsonl"
            session_file.write_text('{"existing": true}\n')
        req = {
            "provider": "pi",
            "cwd": str(run_dir),
            "model": model,
            "effort": effort,
            "is_new": is_new,
            "text": "complete the requested work",
            "session_dir": str(run_dir),
            "session_id": session_id,
            "session_file": str(session_file) if session_file else None,
            "planned_session_id": "headroom-session" if is_new else None,
            "timeout_seconds": 120,
            "request_path": str(run_dir / "request.json"),
            "result_path": str(run_dir / "result.json"),
            "error_path": str(run_dir / "error.json"),
            "wrapper_pid_path": str(run_dir / "wrapper.pid"),
            "child_pid_path": str(run_dir / "child.pid"),
            "stdout_path": str(run_dir / "stdout.log"),
            "stderr_path": str(run_dir / "stderr.log"),
            "stream_path": str(run_dir / "stream.log"),
        }
        return req, run_dir

    def create_session_for_command(self, run_dir: Path, cmd: list[str]) -> Path:
        sid = option_from(cmd, "--session-id")
        session_file = run_dir / f"conversation_{sid}.jsonl"
        session_file.write_text('{"session": true}\n')
        return session_file

    def test_new_gpt_uses_headroom_with_unchanged_model_and_effort(self):
        req, run_dir = self.make_req()
        calls = []

        def run(_req, cmd, _timeout, _sid, _collector):
            calls.append(cmd)
            self.create_session_for_command(run_dir, cmd)
            return 0, reply_stream(), ""

        with mock.patch.object(watcher, "run_pi_provider_command", side_effect=run):
            result = watcher.run_pi_request(req)

        self.assertEqual(len(calls), 1)
        self.assertEqual(provider_from(calls[0]), "headroom-copilot")
        self.assertEqual(option_from(calls[0], "--model"), "gpt-5.3-codex:high")
        self.assertEqual(result["pi_provider"], "headroom-copilot")
        self.assertIn("[pi-provider] headroom-copilot success", Path(req["stderr_path"]).read_text())

    def test_resumed_gpt_uses_headroom_and_same_session(self):
        req, _ = self.make_req(is_new=False, session_id="existing-session")
        calls = []

        def run(_req, cmd, _timeout, _sid, _collector):
            calls.append(cmd)
            return 0, reply_stream(), ""

        with mock.patch.object(watcher, "run_pi_provider_command", side_effect=run):
            result = watcher.run_pi_request(req)

        self.assertEqual(provider_from(calls[0]), "headroom-copilot")
        self.assertEqual(option_from(calls[0], "--session"), req["session_file"])
        self.assertEqual(result["session_file"], req["session_file"])

    def test_non_gpt_pi_models_use_direct_copilot_only(self):
        for model in ("gemini-3-pro-preview", "claude-sonnet-4.5"):
            with self.subTest(model=model):
                req, run_dir = self.make_req(model=model, effort="medium")
                calls = []

                def run(_req, cmd, _timeout, _sid, _collector):
                    calls.append(cmd)
                    self.create_session_for_command(run_dir, cmd)
                    return 0, reply_stream(), ""

                with mock.patch.object(watcher, "run_pi_provider_command", side_effect=run):
                    result = watcher.run_pi_request(req)

                self.assertEqual([provider_from(cmd) for cmd in calls], ["github-copilot"])
                self.assertEqual(result["pi_provider"], "github-copilot")

    def test_pre_tool_headroom_failure_retries_original_prompt_directly(self):
        req, run_dir = self.make_req()
        calls = []

        def run(_req, cmd, _timeout, _sid, _collector):
            calls.append(cmd)
            if provider_from(cmd) == "headroom-copilot":
                return 1, "", "headroom unavailable"
            self.create_session_for_command(run_dir, cmd)
            return 0, reply_stream(), ""

        with mock.patch.object(watcher, "run_pi_provider_command", side_effect=run):
            result = watcher.run_pi_request(req)

        self.assertEqual([provider_from(cmd) for cmd in calls], ["headroom-copilot", "github-copilot"])
        self.assertEqual(calls[1][-1], req["text"])
        self.assertNotEqual(option_from(calls[0], "--session-id"), option_from(calls[1], "--session-id"))
        self.assertEqual(result["pi_provider"], "github-copilot")
        self.assertIn("retry original prompt", Path(req["stderr_path"]).read_text())

    def test_changed_existing_session_resumes_direct_without_original_prompt(self):
        req, _ = self.make_req(is_new=False, session_id="existing-session")
        calls = []

        def run(_req, cmd, _timeout, _sid, _collector):
            calls.append(cmd)
            if provider_from(cmd) == "headroom-copilot":
                with Path(req["session_file"]).open("a") as f:
                    f.write('{"headroom": "progress"}\n')
                return 1, "", "proxy failed"
            return 0, reply_stream(), ""

        with mock.patch.object(watcher, "run_pi_provider_command", side_effect=run):
            result = watcher.run_pi_request(req)

        direct = calls[1]
        self.assertEqual(option_from(direct, "--session"), req["session_file"])
        self.assertEqual(direct[-1], watcher.PI_FALLBACK_CONTINUATION)
        self.assertNotIn(req["text"], direct[-1])
        self.assertEqual(result["session_file"], req["session_file"])

    def test_tool_start_with_new_recoverable_session_resumes_direct(self):
        req, run_dir = self.make_req()
        calls = []
        recovered = None

        def run(_req, cmd, _timeout, _sid, _collector):
            nonlocal recovered
            calls.append(cmd)
            if provider_from(cmd) == "headroom-copilot":
                recovered = self.create_session_for_command(run_dir, cmd)
                tool_event = json.dumps({"type": "tool_execution_start", "toolName": "bash"})
                return 1, tool_event, "proxy failed"
            return 0, reply_stream(), ""

        with mock.patch.object(watcher, "run_pi_provider_command", side_effect=run):
            result = watcher.run_pi_request(req)

        self.assertEqual(option_from(calls[1], "--session"), str(recovered))
        self.assertEqual(calls[1][-1], watcher.PI_FALLBACK_CONTINUATION)
        self.assertEqual(result["session_id"], "headroom-session")
        self.assertEqual(result["session_file"], str(recovered))

    def test_tool_start_without_recoverable_session_fails_without_replay(self):
        req, _ = self.make_req()
        calls = []
        tool_event = json.dumps({"type": "tool_execution_start", "toolName": "bash"})

        def run(_req, cmd, _timeout, _sid, _collector):
            calls.append(cmd)
            return 1, tool_event, "headroom failed after tool"

        with mock.patch.object(watcher, "run_pi_provider_command", side_effect=run):
            with self.assertRaisesRegex(watcher.WorkerCommandError, "direct replay blocked"):
                watcher.run_pi_request(req)

        self.assertEqual([provider_from(cmd) for cmd in calls], ["headroom-copilot"])
        self.assertIn("blocked: tool started", Path(req["stderr_path"]).read_text())

    def test_malformed_failed_attempt_blocks_unsafe_original_prompt_replay(self):
        req, _ = self.make_req()
        calls = []

        def run(_req, cmd, _timeout, _sid, _collector):
            calls.append(cmd)
            return 1, "not-json\n", "headroom failed"

        with mock.patch.object(watcher, "run_pi_provider_command", side_effect=run):
            with self.assertRaisesRegex(watcher.WorkerCommandError, "tool activity uncertain"):
                watcher.run_pi_request(req)

        self.assertEqual([provider_from(cmd) for cmd in calls], ["headroom-copilot"])
        self.assertIn("malformed or oversized", Path(req["stderr_path"]).read_text())

    def test_direct_failure_is_terminal_and_retains_both_provider_errors(self):
        req, _ = self.make_req()
        calls = []

        def run(_req, cmd, _timeout, _sid, _collector):
            calls.append(cmd)
            if provider_from(cmd) == "headroom-copilot":
                return (
                    1,
                    json.dumps({"type": "provider_error", "message": "HEADROOM_PROVIDER_ERROR: proxy refused"}),
                    "headroom startup warning",
                )
            return (
                2,
                json.dumps({"type": "provider_error", "message": "DIRECT_PROVIDER_ERROR: quota exhausted"}),
                "direct startup warning",
            )

        Path(req["request_path"]).write_text(json.dumps(req))
        with mock.patch.object(watcher, "run_pi_provider_command", side_effect=run):
            exit_code = watcher.worker_main(req["request_path"])

        self.assertEqual(exit_code, 1)
        self.assertEqual([provider_from(cmd) for cmd in calls], ["headroom-copilot", "github-copilot"])
        error = json.loads(Path(req["error_path"]).read_text())
        self.assertIn("DIRECT_PROVIDER_ERROR: quota exhausted", error["message"])
        self.assertIn("direct startup warning", error["message"])
        self.assertIn("HEADROOM_PROVIDER_ERROR: proxy refused", error["message"])
        self.assertIn("headroom startup warning", error["message"])
        self.assertGreaterEqual(error["message"].count("stdout:"), 2)
        self.assertGreaterEqual(error["message"].count("stderr:"), 2)
        self.assertLessEqual(len(error["message"]), 1000)
        markers = Path(req["stderr_path"]).read_text()
        self.assertIn("headroom-copilot -> github-copilot", markers)
        self.assertIn("github-copilot failed", markers)

    def test_direct_result_parse_failure_retains_headroom_error(self):
        req, _ = self.make_req()
        calls = []

        def run(_req, cmd, _timeout, _sid, _collector):
            calls.append(cmd)
            if provider_from(cmd) == "headroom-copilot":
                return 1, "", "headroom parse precursor"
            return 0, reply_stream(), ""

        with mock.patch.object(watcher, "run_pi_provider_command", side_effect=run):
            with self.assertRaises(watcher.WorkerCommandError) as raised:
                watcher.run_pi_request(req)

        self.assertEqual(raised.exception.kind, "parse")
        self.assertIn("session file not found", str(raised.exception))
        self.assertIn("headroom parse precursor", str(raised.exception))

    def test_fallback_does_not_extend_overall_timeout(self):
        req, _ = self.make_req()
        req["timeout_seconds"] = 10
        clock = [100.0]
        timeouts = []

        def run(_req, _cmd, timeout, _sid, _collector):
            timeouts.append(timeout)
            clock[0] += timeout
            return 1, "", "headroom consumed deadline"

        with (
            mock.patch.object(watcher.time, "time", side_effect=lambda: clock[0]),
            mock.patch.object(watcher, "run_pi_provider_command", side_effect=run),
        ):
            with self.assertRaisesRegex(watcher.WorkerCommandError, "exceeded timeout"):
                watcher.run_pi_request(req)

        self.assertEqual(timeouts, [10])

    def test_fallback_no_api_key_race_retries_direct_without_another_transition(self):
        req, run_dir = self.make_req()
        calls = []

        def run(_req, cmd, _timeout, _sid, _collector):
            calls.append(cmd)
            if provider_from(cmd) == "headroom-copilot":
                return 1, "", "headroom unavailable"
            if len(calls) == 2:
                return 1, "", "No API key available yet"
            self.create_session_for_command(run_dir, cmd)
            return 0, reply_stream(), ""

        with (
            mock.patch.object(watcher, "run_pi_provider_command", side_effect=run),
            mock.patch.object(watcher.time, "sleep"),
        ):
            result = watcher.run_pi_request(req)

        self.assertEqual(
            [provider_from(cmd) for cmd in calls],
            ["headroom-copilot", "github-copilot", "github-copilot"],
        )
        self.assertEqual(calls[1], calls[2])
        self.assertEqual(result["pi_provider"], "github-copilot")
        self.assertIn("pi copilot auth race", Path(req["stderr_path"]).read_text())


if __name__ == "__main__":
    unittest.main()
