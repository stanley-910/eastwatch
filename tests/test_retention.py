import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import load_watcher


class RetentionSweepTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="eastwatch-retention-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.watcher = load_watcher(self.root)
        self.now = 2_000_000_000.0

    def run_dir(self, conversation: str, run_id: str) -> Path:
        path = self.watcher.CONVOS_DIR / conversation / "runs" / run_id
        path.mkdir(parents=True)
        return path

    def terminal_run(self, directory: Path, *, success: bool, age_days: int) -> dict:
        completed_at = self.now - age_days * 24 * 60 * 60
        result_path = directory / "result.json"
        error_path = directory / "error.json"
        artifact = result_path if success else error_path
        artifact.write_text(json.dumps({"ok": success, "completed_at": completed_at}))
        (directory / "request.json").write_text("{}")
        (directory / "run.jsonl").write_text('{"v":1,"type":"exit"}\n')
        (directory / "stdout.log").write_text("legacy stdout")
        (directory / "stderr.log").write_text("bounded stderr")
        return {
            "run_dir": str(directory),
            "result_path": str(result_path),
            "error_path": str(error_path),
            "stdout_path": str(directory / "stdout.log"),
            "stderr_path": str(directory / "stderr.log"),
            "completed_at": completed_at,
        }

    def test_retains_trio_and_journal_but_expires_success_and_failure_tails(self):
        success_dir = self.run_dir("success", "run-1")
        failure_dir = self.run_dir("failure", "run-2")
        recent_failure_dir = self.run_dir("recent-failure", "run-3")
        success = self.terminal_run(success_dir, success=True, age_days=8)
        failure = self.terminal_run(failure_dir, success=False, age_days=31)
        recent_failure = self.terminal_run(recent_failure_dir, success=False, age_days=29)
        state = {
            "projects": {
                "project": {
                    "conversations": {
                        "1": {"last_run": success},
                        "2": {"last_run": failure},
                        "3": {"last_run": recent_failure},
                    }
                }
            }
        }

        counts = self.watcher.sweep_artifacts(state, now=self.now)

        self.assertEqual(counts["tails"], 4)
        for directory in (success_dir, failure_dir):
            self.assertFalse((directory / "stdout.log").exists())
            self.assertFalse((directory / "stderr.log").exists())
            self.assertTrue((directory / "request.json").exists())
            self.assertTrue((directory / "run.jsonl").exists())
        self.assertTrue((recent_failure_dir / "stderr.log").exists())

    def test_corrupt_result_keeps_failure_tail_for_30_days(self):
        directory = self.run_dir("corrupt", "run-corrupt")
        run = self.terminal_run(directory, success=True, age_days=8)
        Path(run["result_path"]).write_text("not-json")
        state = {
            "projects": {
                "project": {
                    "conversations": {"1": {"last_run": run}}
                }
            }
        }

        self.watcher.sweep_artifacts(state, now=self.now)

        self.assertTrue((directory / "stderr.log").exists())

    def test_active_runs_are_never_swept_and_old_raw_capture_expires(self):
        active_dir = self.run_dir("active", "run-active")
        retained_dir = self.run_dir("retained", "run-retained")
        active = self.terminal_run(active_dir, success=True, age_days=60)
        retained = self.terminal_run(retained_dir, success=True, age_days=1)
        active_raw = active_dir / "stream.log"
        retained_raw = retained_dir / "stream.log.gz"
        active_raw.write_text("active")
        retained_raw.write_text("raw")
        old = self.now - 15 * 24 * 60 * 60
        os.utime(active_raw, (old, old))
        os.utime(retained_raw, (old, old))
        state = {
            "projects": {
                "project": {
                    "conversations": {
                        "1": {"current_run": active},
                        "2": {"last_run": retained},
                    }
                }
            }
        }

        counts = self.watcher.sweep_artifacts(state, now=self.now)

        self.assertTrue(active_raw.exists())
        self.assertFalse(retained_raw.exists())
        self.assertEqual(counts["raw_captures"], 1)

    def test_orphans_expire_after_30_days(self):
        old_dir = self.run_dir("orphan", "old")
        recent_dir = self.run_dir("orphan", "recent")
        (old_dir / "request.json").write_text("{}")
        (recent_dir / "request.json").write_text("{}")
        old = self.now - 31 * 24 * 60 * 60
        recent = self.now - 29 * 24 * 60 * 60
        os.utime(old_dir, (old, old))
        os.utime(recent_dir, (recent, recent))

        counts = self.watcher.sweep_artifacts({"projects": {}}, now=self.now)

        self.assertFalse(old_dir.exists())
        self.assertTrue(recent_dir.exists())
        self.assertEqual(counts["orphans"], 1)

    def test_automatic_sweep_does_not_reclaim_legacy_bulk_artifacts(self):
        legacy_dir = self.run_dir("legacy", "run-old")
        legacy = self.terminal_run(legacy_dir, success=True, age_days=60)
        (legacy_dir / "run.jsonl").unlink()
        state = {
            "projects": {
                "project": {
                    "conversations": {"1": {"last_run": legacy}}
                }
            }
        }

        self.watcher.sweep_artifacts(state, now=self.now, include_legacy=False)

        self.assertTrue((legacy_dir / "stdout.log").exists())
        self.assertTrue((legacy_dir / "stderr.log").exists())

    def test_daily_sweep_runs_once_per_interval(self):
        state = {"projects": {}}
        with mock.patch.object(self.watcher, "sweep_artifacts", return_value={
            "tails": 0,
            "raw_captures": 0,
            "orphans": 0,
        }) as sweep:
            self.assertTrue(self.watcher.maybe_sweep_artifacts(state, now=self.now))
            self.assertFalse(self.watcher.maybe_sweep_artifacts(state, now=self.now + 60))
        sweep.assert_called_once_with(state, now=self.now, include_legacy=False)

    def test_new_run_layout_has_journal_and_no_duplicate_stdout_or_stream(self):
        directory = self.run_dir("layout", "run-layout")
        conv = {
            "provider": "pi",
            "model": "gpt-5.5",
            "effort": "high",
            "cwd": str(self.root),
            "session_dir": str(directory.parent.parent),
        }
        with mock.patch.dict(os.environ, {"EASTWATCH_RAW_CAPTURE": ""}):
            request = self.watcher.make_run_request(
                conv,
                [],
                True,
                "prompt",
                directory,
                "run-layout",
            )
        self.assertIn("journal_path", request)
        self.assertNotIn("stdout_path", request)
        self.assertNotIn("stream_path", request)
        self.assertIsNone(request["raw_capture_path"])

    def test_cli_dispatches_explicit_sweep(self):
        with mock.patch.object(self.watcher, "sweep_main", return_value=0) as sweep:
            self.assertEqual(self.watcher.cli(["sweep"]), 0)
        sweep.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
