"""Tests for deleting retained finished fleet rows."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eastwatch.fleet import dismiss


class FleetDismissTest(unittest.TestCase):
    def setUp(self):
        self.dismiss = dismiss
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run_dir = self.root / "convos" / "repo-62" / "runs" / "run-1"
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "stream.log").write_text("retained trace\n")
        self.state_path = self.root / "state.json"
        self.state_path.write_text(
            json.dumps(
                {
                    "projects": {
                        "gitlab.example/repo": {
                            "conversations": {
                                "62": {
                                    "status": "done",
                                    "current_run": None,
                                    "last_run": {
                                        "run_id": "run-1",
                                        "run_dir": str(self.run_dir),
                                        "tmux_session": "task-repo-62",
                                    },
                                }
                            }
                        }
                    }
                }
            )
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_dismiss_kills_tmux_and_clears_row_but_preserves_artifacts(self):
        with mock.patch.object(self.dismiss, "kill_session") as kill:
            self.dismiss.dismiss(
                "gitlab.example/repo:62",
                "run-1",
                root=self.root,
            )

        kill.assert_called_once_with("task-repo-62")
        state = json.loads(self.state_path.read_text())
        conversation = state["projects"]["gitlab.example/repo"]["conversations"]["62"]
        self.assertIsNone(conversation["last_run"])
        self.assertEqual((self.run_dir / "stream.log").read_text(), "retained trace\n")
        self.assertTrue(self.state_path.with_suffix(".json.bak").exists())

    def test_dismiss_refuses_active_or_replaced_run(self):
        state = json.loads(self.state_path.read_text())
        conversation = state["projects"]["gitlab.example/repo"]["conversations"]["62"]
        conversation["current_run"] = {"run_id": "run-2"}
        self.state_path.write_text(json.dumps(state))
        with self.assertRaisesRegex(RuntimeError, "no longer finished"):
            self.dismiss.dismiss("gitlab.example/repo:62", "run-1", root=self.root)

        conversation["current_run"] = None
        self.state_path.write_text(json.dumps(state))
        with self.assertRaisesRegex(RuntimeError, "was replaced"):
            self.dismiss.dismiss("gitlab.example/repo:62", "stale-run", root=self.root)

    def test_dismiss_all_finished_clears_only_terminal_rows(self):
        state = json.loads(self.state_path.read_text())
        conversations = state["projects"]["gitlab.example/repo"]["conversations"]
        conversations.update(
            {
                "63": {
                    "status": "failed",
                    "current_run": None,
                    "last_run": {"run_id": "run-2", "tmux_session": "task-repo-63"},
                },
                "64": {
                    "status": "killed",
                    "current_run": None,
                    "last_run": {"run_id": "run-3", "tmux_session": "task-repo-64"},
                },
                "65": {
                    "status": "working",
                    "current_run": {"run_id": "run-4"},
                    "last_run": None,
                },
                "66": {
                    "status": "parked",
                    "current_run": None,
                    "last_run": {"run_id": "run-5"},
                },
            }
        )
        self.state_path.write_text(json.dumps(state))

        with mock.patch.object(self.dismiss, "kill_session") as kill:
            result = self.dismiss.dismiss_all_finished(root=self.root)

        self.assertEqual(
            result.dismissed,
            (
                ("gitlab.example/repo:62", "run-1"),
                ("gitlab.example/repo:63", "run-2"),
                ("gitlab.example/repo:64", "run-3"),
            ),
        )
        self.assertEqual(result.failures, ())
        self.assertEqual(kill.call_count, 3)
        saved = json.loads(self.state_path.read_text())
        saved_conversations = saved["projects"]["gitlab.example/repo"]["conversations"]
        self.assertIsNone(saved_conversations["62"]["last_run"])
        self.assertIsNone(saved_conversations["63"]["last_run"])
        self.assertIsNone(saved_conversations["64"]["last_run"])
        self.assertEqual(saved_conversations["65"]["current_run"]["run_id"], "run-4")
        self.assertEqual(saved_conversations["66"]["last_run"]["run_id"], "run-5")

    def test_dismiss_all_finished_retries_lock_contention(self):
        real_flock = self.dismiss.fcntl.flock
        attempts = 0

        def contend_once(lock, operation):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise BlockingIOError
            return real_flock(lock, operation)

        with (
            mock.patch.object(self.dismiss.fcntl, "flock", side_effect=contend_once),
            mock.patch.object(self.dismiss.time, "sleep") as sleep,
            mock.patch.object(self.dismiss, "kill_session"),
        ):
            result = self.dismiss.dismiss_all_finished(root=self.root)

        self.assertEqual(len(result.dismissed), 1)
        self.assertEqual(attempts, 2)
        sleep.assert_called_once_with(self.dismiss.LOCK_RETRY_DELAY_S)

    def test_dismiss_all_finished_empty_store_is_noop(self):
        self.state_path.write_text(json.dumps({"projects": {}}))

        with (
            mock.patch.object(self.dismiss, "state_dir", return_value=self.root),
            mock.patch.object(self.dismiss, "kill_session") as kill,
            mock.patch("builtins.print") as output,
        ):
            exit_code = self.dismiss.main(["--all-finished"])

        self.assertEqual(exit_code, 0)
        output.assert_called_once_with("Dismissed 0 terminal fleet rows")
        kill.assert_not_called()

    def test_main_reports_each_dismissal_and_returns_nonzero_for_failures(self):
        state = json.loads(self.state_path.read_text())
        conversations = state["projects"]["gitlab.example/repo"]["conversations"]
        conversations["63"] = {
            "status": "failed",
            "current_run": None,
            "last_run": {"run_id": "run-2", "tmux_session": "task-repo-63"},
        }
        self.state_path.write_text(json.dumps(state))

        def kill(name):
            if name == "task-repo-63":
                raise RuntimeError("tmux unavailable")

        with (
            mock.patch.object(self.dismiss, "state_dir", return_value=self.root),
            mock.patch.object(self.dismiss, "kill_session", side_effect=kill),
            mock.patch("builtins.print") as output,
        ):
            exit_code = self.dismiss.main(["--all-finished"])

        self.assertEqual(exit_code, 1)
        output.assert_any_call("Dismissed gitlab.example/repo:62 run-1")
        output.assert_any_call("Dismissed 1 terminal fleet row; 1 failed")
        output.assert_any_call(
            "fleet-dismiss: gitlab.example/repo:63 run-2: tmux unavailable",
            file=self.dismiss.sys.stderr,
        )


if __name__ == "__main__":
    unittest.main()
