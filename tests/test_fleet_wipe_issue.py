"""Tests for completely wiping one inactive issue from local watcher state."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.support import SCRIPTS_DIR

SCRIPT = SCRIPTS_DIR / "fleet-wipe-issue"


class FleetWipeIssueTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.artifacts = self.root / "convos" / "example-org-example-repo-102"
        self.artifacts.mkdir(parents=True)
        (self.artifacts / "stream.log").write_text("trace\n")
        self.state = {
            "projects": {
                "gitlab.example/example-org/example-repo": {
                    "conversations": {
                        "102": {
                            "status": "failed",
                            "current_run": None,
                            "last_run": {"tmux_session": "task-example-repo-102"},
                            "session_dir": str(self.artifacts),
                        },
                        "103": {"status": "done", "current_run": None},
                    },
                    "mr_index": {
                        "80": {"issue_iid": "102", "conversation_key": "102"},
                        "81": {"issue_iid": "103", "conversation_key": "103"},
                    },
                }
            }
        }
        self.state_path = self.root / "state.json"
        self.backup_path = self.root / "state.json.bak"
        self.state_path.write_text(json.dumps(self.state))
        self.backup_path.write_text(json.dumps(self.state))

    def tearDown(self):
        self.temp.cleanup()

    def run_script(self, *args: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["EASTWATCH_STATE_DIR"] = str(self.root)
        return subprocess.run(
            [str(SCRIPT), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=env,
        )

    def test_wipes_state_backup_mr_mapping_and_artifacts(self):
        result = self.run_script("--yes", "example-repo", "102")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "Wiped gitlab.example/example-org/example-repo issue 102", result.stdout
        )

        for path in (self.state_path, self.backup_path):
            state = json.loads(path.read_text())
            project = state["projects"]["gitlab.example/example-org/example-repo"]
            self.assertNotIn("102", project["conversations"])
            self.assertIn("103", project["conversations"])
            self.assertNotIn("80", project["mr_index"])
            self.assertIn("81", project["mr_index"])
        self.assertFalse(self.artifacts.exists())

    def test_refuses_active_run_without_changing_anything(self):
        self.state["projects"]["gitlab.example/example-org/example-repo"][
            "conversations"
        ]["102"]["current_run"] = {"run_id": "active"}
        self.state_path.write_text(json.dumps(self.state))

        result = self.run_script("--yes", "example-repo", "102")
        self.assertEqual(result.returncode, 1)
        self.assertIn("active run", result.stderr)
        self.assertTrue(self.artifacts.exists())
        state = json.loads(self.state_path.read_text())
        self.assertIn(
            "102",
            state["projects"]["gitlab.example/example-org/example-repo"][
                "conversations"
            ],
        )

    def test_requires_unambiguous_project_slug(self):
        self.state["projects"]["gitlab.example/other/example-repo"] = {
            "conversations": {},
            "mr_index": {},
        }
        self.state_path.write_text(json.dumps(self.state))

        result = self.run_script("--yes", "example-repo", "102")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ambiguous", result.stderr)
        self.assertTrue(self.artifacts.exists())

    def test_refuses_artifact_path_outside_state_directory(self):
        outside = self.root.with_name(f"{self.root.name}-outside")
        outside.mkdir()
        try:
            conversation = self.state["projects"][
                "gitlab.example/example-org/example-repo"
            ]["conversations"]["102"]
            conversation["session_dir"] = str(outside)
            self.state_path.write_text(json.dumps(self.state))

            result = self.run_script("--yes", "example-repo", "102")
            self.assertEqual(result.returncode, 1)
            self.assertIn("outside", result.stderr)
            self.assertTrue(outside.exists())
        finally:
            outside.rmdir()


if __name__ == "__main__":
    unittest.main()
