import atexit
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.support import load_watcher

TMP_ROOT = Path(tempfile.mkdtemp(prefix="eastwatch-state-test-"))
watcher = load_watcher(TMP_ROOT)

atexit.register(lambda: shutil.rmtree(TMP_ROOT, ignore_errors=True))


class StatePersistenceTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(watcher.STATE_DIR, ignore_errors=True)
        self.assertTrue(watcher.STATE_DIR.is_relative_to(TMP_ROOT))

    def read_state(self):
        return json.loads(watcher.STATE_PATH.read_text())

    def write_state(self, state):
        watcher.STATE_DIR.mkdir(parents=True, exist_ok=True)
        watcher.STATE_PATH.write_text(json.dumps(state))

    def test_load_state_accepts_missing_and_non_negative_project_cursors(self):
        project_key = "gitlab.example.com/group/project"
        for project_state in ({}, {"last_event_id": 0}, {"last_event_id": 42}):
            with self.subTest(project_state=project_state):
                state = {"projects": {project_key: project_state}}
                self.write_state(state)
                self.assertEqual(watcher.load_state(), state)

    def test_load_state_rejects_malformed_project_cursors_with_recovery_guidance(self):
        project_key = "gitlab.example.com/group/project"
        for value in (False, "", "42", 1.5, None, -1):
            with self.subTest(value=value):
                self.write_state({"projects": {project_key: {"last_event_id": value}}})

                with self.assertRaises(watcher.StatePersistenceError) as raised:
                    watcher.load_state()

                message = str(raised.exception)
                self.assertIn(f"projects[{project_key!r}].last_event_id", message)
                self.assertIn("non-negative integer", message)
                self.assertIn("refusing to poll", message)
                self.assertIn(str(watcher.STATE_PATH), message)
                self.assertIn(str(watcher.STATE_BAK_PATH), message)

    def test_save_guard_and_backup_rotation(self):
        zero = {"projects": {}}
        watcher.save_state(zero)
        watcher.save_state(zero)
        self.assertEqual(zero, self.read_state())

        non_empty = {"projects": {"example": {"bootstrapped": True}}}
        watcher.save_state(non_empty)
        with self.assertRaises(watcher.StatePersistenceError):
            watcher.save_state(zero)
        self.assertEqual(non_empty, self.read_state())

        replacement = {"projects": {"example": {"bootstrapped": False}}}
        watcher.save_state(replacement)
        self.assertEqual(replacement, self.read_state())
        self.assertEqual(non_empty, json.loads(watcher.STATE_BAK_PATH.read_text()))


if __name__ == "__main__":
    unittest.main()
