"""Tests for the standalone fleet-resume command."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eastwatch.fleet import resume
from eastwatch.fleet.core import FleetRow


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
        "log": "",
        "url": "https://gitlab.example/repo/-/issues/62",
        "cwd": str(Path.cwd()),
    }
    values.update(overrides)
    return FleetRow(**values)


class FleetResumeTest(unittest.TestCase):
    def setUp(self):
        self.resume = resume

    def test_resumable_rows_include_finished_but_exclude_live_workers(self):
        rows = [
            row(identity="parked"),
            row(
                identity="finished",
                status="done",
                derived="finished",
                tmux_alive=True,
            ),
            row(identity="live", tmux_alive=True, derived="working"),
            row(identity="idle", derived="idle"),
        ]
        self.assertEqual(
            [item.identity for item in self.resume.resumable_rows(rows)],
            ["parked", "finished"],
        )

    def test_match_is_case_insensitive_across_key_identity_and_url(self):
        rows = [row()]
        self.assertEqual(self.resume.match_row(rows, "REPO-62"), rows[0])
        self.assertEqual(self.resume.match_row(rows, "issues/62"), rows[0])
        with self.assertRaisesRegex(LookupError, "no resumable row"):
            self.resume.match_row(rows, "missing")

    def test_ambiguous_fragment_requires_narrower_query(self):
        rows = [row(identity="a"), row(identity="b", key="task-repo-620")]
        with self.assertRaisesRegex(LookupError, "ambiguous"):
            self.resume.match_row(rows, "repo")

    def test_numbered_fallback_picker(self):
        rows = [row(key="task-a"), row(key="task-b", identity="b")]
        output = io.StringIO()
        with mock.patch.object(self.resume.shutil, "which", return_value=None):
            selected = self.resume.choose_row(
                rows,
                stdin=io.StringIO("2\n"),
                stdout=output,
            )
        self.assertEqual(selected.key, "task-b")
        self.assertIn("task-a", output.getvalue())

    def test_numbered_picker_rejects_zero_and_negative_indices(self):
        rows = [row(key="task-a"), row(key="task-b", identity="b")]
        for choice in ("0\n", "-1\n", "3\n"):
            with (
                self.subTest(choice=choice.strip()),
                mock.patch.object(self.resume.shutil, "which", return_value=None),
                self.assertRaisesRegex(LookupError, "no such row"),
            ):
                self.resume.choose_row(
                    rows,
                    stdin=io.StringIO(choice),
                    stdout=io.StringIO(),
                )

    def test_payload_round_trip(self):
        original = row(cwd="/tmp/with spaces")
        restored = self.resume.row_from_payload(self.resume.row_payload(original))
        self.assertEqual(restored, original)

    def test_lock_refuses_duplicate_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"EASTWATCH_STATE_DIR": tmp}):
                first = self.resume.acquire_chat_lock(row())
                try:
                    with self.assertRaisesRegex(RuntimeError, "already active"):
                        self.resume.acquire_chat_lock(row())
                finally:
                    first.close()

    def test_dry_run_has_stable_testable_shape(self):
        output = io.StringIO()
        with (
            mock.patch.object(self.resume, "load_rows", return_value=(row(),)),
            mock.patch("sys.stdout", output),
        ):
            result = self.resume.main(["--dry-run", "62"])
        self.assertEqual(result, 0)
        self.assertEqual(
            output.getvalue(),
            "[window] chat-task-repo-62: claude --resume abc-123 --model sonnet\n",
        )

    def test_live_window_blocks_launch(self):
        error = io.StringIO()
        with (
            mock.patch.object(self.resume, "load_rows", return_value=(row(),)),
            mock.patch.object(self.resume, "chat_lock_active", return_value=False),
            mock.patch.object(self.resume, "tmux_window_exists", return_value=True),
            mock.patch("sys.stderr", error),
        ):
            result = self.resume.main(["62"])
        self.assertEqual(result, 1)
        self.assertIn("already active", error.getvalue())

    def test_resumed_chat_points_tmux_pane_at_saved_worktree(self):
        completed = mock.Mock(returncode=0)
        with (
            mock.patch.object(
                self.resume.shutil, "which", return_value="/usr/bin/tmux"
            ),
            mock.patch.object(
                self.resume.subprocess, "run", return_value=completed
            ) as run,
            mock.patch.dict(os.environ, {"TMUX_PANE": "%42"}),
        ):
            self.resume.point_current_tmux_pane("/worktrees/repo/issue-102")

        run.assert_called_once_with(
            [
                "/usr/bin/tmux",
                "set-option",
                "-p",
                "-t",
                "%42",
                "@agent_worktree",
                "/worktrees/repo/issue-102",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )

    def test_lock_holding_runner_points_pane_before_resuming_in_saved_cwd(self):
        selected = row(cwd="/worktrees/repo/issue-102")
        lock = mock.Mock()
        with (
            mock.patch.object(self.resume, "acquire_chat_lock", return_value=lock),
            mock.patch.object(
                self.resume,
                "interactive_command",
                return_value=("pi", "--session", "abc"),
            ),
            mock.patch.object(self.resume, "point_current_tmux_pane") as point,
            mock.patch.object(self.resume.os, "chdir") as chdir,
            mock.patch.object(self.resume.os, "execvp", side_effect=OSError("stop")),
            self.assertRaisesRegex(OSError, "stop"),
        ):
            self.resume.hold_lock_and_exec(selected)

        point.assert_called_once_with(selected.cwd)
        chdir.assert_called_once_with(selected.cwd)
        lock.close.assert_called_once_with()

    def test_inside_tmux_launches_lock_holding_runner(self):
        completed = mock.Mock(returncode=0)
        error = io.StringIO()
        with (
            mock.patch.object(self.resume, "load_rows", return_value=(row(),)),
            mock.patch.object(self.resume, "chat_lock_active", return_value=False),
            mock.patch.object(self.resume, "tmux_window_exists", return_value=False),
            mock.patch.object(
                self.resume.subprocess, "run", return_value=completed
            ) as run,
            mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux"}),
            mock.patch("sys.stderr", error),
        ):
            result = self.resume.main(["62"])
        self.assertEqual(result, 0)
        command = run.call_args.args[0]
        self.assertEqual(
            command[:5], ("tmux", "new-window", "-n", "chat-task-repo-62", "-c")
        )
        self.assertIn("--hold-lock", command[-1])


if __name__ == "__main__":
    unittest.main()
