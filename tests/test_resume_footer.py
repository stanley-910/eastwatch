import atexit
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.support import load_watcher

TMP_ROOT = Path(tempfile.mkdtemp(prefix="eastwatch-footer-test-"))
watcher = load_watcher(TMP_ROOT)

atexit.register(lambda: shutil.rmtree(TMP_ROOT, ignore_errors=True))


class ResumeFooterTest(unittest.TestCase):
    def test_failure_body_retry_wording_matches_resume_semantics(self):
        body = watcher.failure_body("exit")

        self.assertIn("Reply in the agent's thread (or use @agent) to retry.", body)
        self.assertNotIn(
            "A fresh owner comment on this issue will retry the session", body
        )

    def test_pi_footer_shows_model_and_home_relative_resume_command(self):
        session_file = Path.home() / ".local/state/eastwatch/convos/issue/session.jsonl"
        footer = watcher.resume_footer(
            {
                "provider": "pi",
                "model": "gpt-5.5",
                "effort": "high",
                "session_file": str(session_file),
            }
        )

        self.assertEqual(
            footer,
            "\n\n---\nmodel: [pi:gpt-5.5:high]\n"
            "```\npi --session ~/.local/state/eastwatch/convos/issue/session.jsonl\n```",
        )

    def test_pi_footer_preserves_session_path_outside_home(self):
        footer = watcher.resume_footer(
            {
                "provider": "pi",
                "model": "gpt-5.5",
                "session_file": "/tmp/session.jsonl",
            }
        )

        self.assertIn("```\npi --session /tmp/session.jsonl\n```", footer)

    def test_claude_footer_identifies_bot_and_model(self):
        footer = watcher.resume_footer(
            {
                "provider": "claude",
                "model": "opus",
                "effort": "max",
                "session_id": "abc123",
                "cwd": "/repo/worktree",
            }
        )

        self.assertEqual(
            footer,
            "\n\n---\nmodel: [claude:opus:max]\n"
            "```\nclaude --resume abc123\n```\n"
            "cwd: `/repo/worktree`",
        )

    def test_none_or_missing_effort_is_omitted(self):
        footer = watcher.resume_footer(
            {
                "provider": "pi",
                "model": "gpt-5.5",
                "effort": None,
                "session_file": "/tmp/session.jsonl",
            }
        )
        legacy_footer = watcher.resume_footer(
            {
                "provider": "pi",
                "model": "gpt-5.5",
                "session_file": "/tmp/session.jsonl",
            }
        )
        missing_model_footer = watcher.resume_footer(
            {
                "provider": "pi",
                "session_file": "/tmp/session.jsonl",
            }
        )

        self.assertIn("model: [pi:gpt-5.5]", footer)
        self.assertNotIn(":None", footer)
        self.assertIn("model: [pi:gpt-5.5]", legacy_footer)
        self.assertNotIn(":None", legacy_footer)
        self.assertIn("model: [pi:unknown]", missing_model_footer)


if __name__ == "__main__":
    unittest.main()
