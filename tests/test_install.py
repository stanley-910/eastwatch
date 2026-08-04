"""Tests for launchd installation and bootstrap race recovery."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.support import REPOSITORY_ROOT

INSTALL = REPOSITORY_ROOT / "install.sh"


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        (self.home / "Library" / "LaunchAgents").mkdir(parents=True)
        self.bin.mkdir()
        self.calls = self.root / "launchctl.calls"
        self.count = self.root / "bootstrap.count"
        launchctl = self.bin / "launchctl"
        launchctl.write_text(
            "#!/bin/sh\n"
            'echo "$*" >> "$FAKE_LAUNCHCTL_CALLS"\n'
            'case "$1" in\n'
            "  bootout) exit 0 ;;\n"
            "  bootstrap)\n"
            "    count=0\n"
            '    [ ! -f "$FAKE_BOOTSTRAP_COUNT" ] || count=$(cat "$FAKE_BOOTSTRAP_COUNT")\n'
            "    count=$((count + 1))\n"
            '    echo "$count" > "$FAKE_BOOTSTRAP_COUNT"\n'
            '    if [ "$count" -le "$FAKE_BOOTSTRAP_FAILURES" ]; then\n'
            "      echo 'Bootstrap failed: 5: Input/output error' >&2\n"
            "      exit 5\n"
            "    fi\n"
            "    exit 0\n"
            "    ;;\n"
            '  load) [ "$FAKE_LOAD_FAILURE" != 1 ] ;;\n'
            "  print) echo 'state = running'; echo 'pid = 123'; exit 0 ;;\n"
            "esac\n"
        )
        launchctl.chmod(0o755)
        sleep = self.bin / "sleep"
        sleep.write_text("#!/bin/sh\nexit 0\n")
        sleep.chmod(0o755)
        mv = self.bin / "mv"
        mv.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            "  */.local/state/board-watcher)\n"
            '    [ "$FAKE_FAIL_STATE_MOVE" != 1 ] || exit 9\n'
            "    ;;\n"
            "esac\n"
            'exec /bin/mv "$@"\n'
        )
        mv.chmod(0o755)

    def tearDown(self):
        self.temp.cleanup()

    def run_install(
        self,
        bootstrap_failures: int,
        *,
        fail_state_move: bool = False,
        fail_load: bool = False,
    ) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.bin}:{env['PATH']}",
                "FAKE_LAUNCHCTL_CALLS": str(self.calls),
                "FAKE_BOOTSTRAP_COUNT": str(self.count),
                "FAKE_BOOTSTRAP_FAILURES": str(bootstrap_failures),
                "FAKE_FAIL_STATE_MOVE": "1" if fail_state_move else "0",
                "FAKE_LOAD_FAILURE": "1" if fail_load else "0",
            }
        )
        return subprocess.run(
            [str(INSTALL)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=env,
        )

    def test_migrates_legacy_directories_to_compatibility_symlinks(self):
        legacy_config = self.home / ".config" / "board-watcher"
        legacy_state = self.home / ".local" / "state" / "board-watcher"
        legacy_config.mkdir(parents=True)
        legacy_state.mkdir(parents=True)
        (legacy_config / "config.yaml").write_text("projects: []\n")
        (legacy_state / "state.json").write_text('{"projects": {}}\n')

        result = self.run_install(bootstrap_failures=0)

        self.assertEqual(result.returncode, 0, result.stderr)
        current_config = self.home / ".config" / "eastwatch"
        current_state = self.home / ".local" / "state" / "eastwatch"
        self.assertEqual((current_config / "config.yaml").read_text(), "projects: []\n")
        self.assertEqual(
            (current_state / "state.json").read_text(), '{"projects": {}}\n'
        )
        self.assertTrue(legacy_config.is_symlink())
        self.assertTrue(legacy_state.is_symlink())
        self.assertEqual(legacy_config.resolve(), current_config.resolve())
        self.assertEqual(legacy_state.resolve(), current_state.resolve())

    def test_refuses_to_merge_divergent_legacy_and_current_directories(self):
        legacy_config = self.home / ".config" / "board-watcher"
        current_config = self.home / ".config" / "eastwatch"
        legacy_config.mkdir(parents=True)
        current_config.mkdir(parents=True)
        (legacy_config / "config.yaml").write_text("legacy\n")
        (current_config / "config.yaml").write_text("current\n")

        result = self.run_install(bootstrap_failures=0)

        self.assertEqual(result.returncode, 1)
        self.assertIn("refusing to merge divergent directories", result.stderr)
        self.assertEqual((legacy_config / "config.yaml").read_text(), "legacy\n")
        self.assertEqual((current_config / "config.yaml").read_text(), "current\n")
        self.assertFalse(
            self.calls.exists(), "validation must happen before stopping jobs"
        )

    def test_accepts_relative_compatibility_symlinks(self):
        current_config = self.home / ".config" / "eastwatch"
        current_state = self.home / ".local" / "state" / "eastwatch"
        current_config.mkdir(parents=True)
        current_state.mkdir(parents=True)
        (current_config / "config.yaml").write_text("projects: []\n")
        legacy_config = current_config.with_name("board-watcher")
        legacy_state = current_state.with_name("board-watcher")
        legacy_config.symlink_to("eastwatch", target_is_directory=True)
        legacy_state.symlink_to("eastwatch", target_is_directory=True)

        result = self.run_install(bootstrap_failures=0)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(legacy_config.resolve(), current_config.resolve())
        self.assertEqual(legacy_state.resolve(), current_state.resolve())

    def test_restores_legacy_job_when_directory_migration_fails(self):
        legacy_config = self.home / ".config" / "board-watcher"
        legacy_state = self.home / ".local" / "state" / "board-watcher"
        legacy_config.mkdir(parents=True)
        legacy_state.mkdir(parents=True)
        (legacy_config / "config.yaml").write_text("projects: []\n")
        legacy_plist = (
            self.home / "Library" / "LaunchAgents" / "com.stanwang.board-watcher.plist"
        )
        legacy_plist.write_text("legacy")

        result = self.run_install(bootstrap_failures=0, fail_state_move=True)

        self.assertEqual(result.returncode, 1)
        self.assertIn("restoring the previously installed launchd job", result.stderr)
        self.assertTrue(legacy_config.is_symlink())
        self.assertTrue(legacy_state.is_dir())
        self.assertTrue(legacy_plist.exists())
        calls = self.calls.read_text().splitlines()
        self.assertTrue(
            any(
                call.endswith(str(legacy_plist)) and call.startswith("bootstrap ")
                for call in calls
            ),
            calls,
        )

    def test_refuses_state_move_while_worker_run_is_active(self):
        legacy_config = self.home / ".config" / "board-watcher"
        legacy_state = self.home / ".local" / "state" / "board-watcher"
        legacy_config.mkdir(parents=True)
        legacy_state.mkdir(parents=True)
        (legacy_config / "config.yaml").write_text("projects: []\n")
        (legacy_state / "state.json").write_text(
            '{"projects":{"example/repo":{"conversations":{"7":'
            '{"current_run":{"run_id":"run-1"}}}}}}\n'
        )
        legacy_plist = (
            self.home / "Library" / "LaunchAgents" / "com.stanwang.board-watcher.plist"
        )
        legacy_plist.write_text("legacy")

        result = self.run_install(bootstrap_failures=0)

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "refusing to move state while worker runs are active", result.stderr
        )
        self.assertTrue(legacy_config.is_dir())
        self.assertTrue(legacy_state.is_dir())
        self.assertTrue(legacy_plist.exists())
        calls = self.calls.read_text().splitlines()
        self.assertTrue(
            any(
                call.endswith(str(legacy_plist)) and call.startswith("bootstrap ")
                for call in calls
            ),
            calls,
        )

    def test_failed_new_job_start_removes_stranded_new_plist(self):
        legacy_config = self.home / ".config" / "board-watcher"
        legacy_state = self.home / ".local" / "state" / "board-watcher"
        legacy_config.mkdir(parents=True)
        legacy_state.mkdir(parents=True)
        (legacy_config / "config.yaml").write_text("projects: []\n")
        (legacy_state / "state.json").write_text('{"projects": {}}\n')
        legacy_plist = (
            self.home / "Library" / "LaunchAgents" / "com.stanwang.board-watcher.plist"
        )
        legacy_plist.write_text("legacy")

        result = self.run_install(bootstrap_failures=2, fail_load=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restoring the previously installed launchd job", result.stderr)
        self.assertTrue(legacy_plist.exists())
        new_plist = (
            self.home / "Library" / "LaunchAgents" / "com.stanwang.eastwatch.plist"
        )
        self.assertFalse(new_plist.exists())
        calls = self.calls.read_text().splitlines()
        self.assertTrue(
            any(
                call.endswith(str(legacy_plist)) and call.startswith("bootstrap ")
                for call in calls
            ),
            calls,
        )

    def test_stops_and_removes_legacy_launchd_job(self):
        legacy_plist = (
            self.home / "Library" / "LaunchAgents" / "com.stanwang.board-watcher.plist"
        )
        legacy_plist.write_text("legacy")

        result = self.run_install(bootstrap_failures=0)

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text().splitlines()
        self.assertTrue(
            any(call.endswith("/com.stanwang.board-watcher") for call in calls),
            calls,
        )
        self.assertFalse(legacy_plist.exists())

    def test_renders_plist_example_for_current_checkout(self):
        result = self.run_install(bootstrap_failures=0)
        self.assertEqual(result.returncode, 0, result.stderr)

        plist = self.home / "Library" / "LaunchAgents" / "com.stanwang.eastwatch.plist"
        rendered = plist.read_text()
        self.assertIn(str(REPOSITORY_ROOT), rendered)
        self.assertIn(str(self.home), rendered)
        self.assertIn("<string>/usr/bin/env</string>", rendered)
        self.assertIn(f"<string>{REPOSITORY_ROOT}/eastwatch</string>", rendered)
        self.assertIn("<integer>15</integer>", rendered)
        self.assertNotIn("__REPO__", rendered)
        self.assertNotIn("__HOME__", rendered)

    def test_retries_bootstrap_after_bootout_race_without_legacy_fallback(self):
        result = self.run_install(bootstrap_failures=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("bootstrapped com.stanwang.eastwatch after retry", result.stdout)
        self.assertNotIn("falling back", result.stdout)
        calls = self.calls.read_text().splitlines()
        self.assertEqual(sum(call.startswith("bootstrap ") for call in calls), 2)
        self.assertFalse(any(call.startswith("load ") for call in calls))

    def test_reports_retry_error_before_legacy_fallback(self):
        result = self.run_install(bootstrap_failures=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("bootstrap failed after retry", result.stderr)
        self.assertIn("Bootstrap failed: 5: Input/output error", result.stderr)
        self.assertIn("falling back to load -w", result.stdout)
        calls = self.calls.read_text().splitlines()
        self.assertEqual(sum(call.startswith("bootstrap ") for call in calls), 2)
        self.assertTrue(any(call.startswith("load -w ") for call in calls))


if __name__ == "__main__":
    unittest.main()
