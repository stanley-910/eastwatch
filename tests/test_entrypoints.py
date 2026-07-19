"""Tests for source-checkout and installed-package entrypoint selection."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from eastwatch import paths, watcher
from tests.support import SCRIPTS_DIR


class EntrypointCommandTest(unittest.TestCase):
    def test_uses_source_wrapper_when_checkout_contains_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            scripts = Path(temporary)
            wrapper = scripts / "fleet-status"
            wrapper.touch()
            with mock.patch.object(paths, "SCRIPTS_DIR", scripts):
                command = paths.entrypoint_command(
                    "eastwatch.fleet.status", "fleet-status"
                )

        self.assertEqual(command, (sys.executable, str(wrapper)))

    def test_source_resume_wrapper_is_importable_from_another_cwd(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "fleet-resume"), "--help"],
                cwd=temporary,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: fleet-resume", result.stdout)

    def test_falls_back_to_installed_module_without_source_wrapper(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(paths, "SCRIPTS_DIR", Path(temporary)):
                command = paths.entrypoint_command(
                    "eastwatch.fleet.status", "fleet-status"
                )

        self.assertEqual(
            command,
            (sys.executable, "-m", "eastwatch.fleet.status"),
        )


class JiraEntrypointTest(unittest.TestCase):
    def test_enabled_jira_without_base_url_is_skipped(self):
        with mock.patch.object(watcher.subprocess, "run") as run:
            context = watcher.fetch_jira_context(
                {},
                {"title": "Investigate PROJ-123", "description": ""},
                {"enabled": True},
            )

        self.assertEqual(context, "")
        run.assert_not_called()

    def test_invalid_custom_jira_fields_skip_helper(self):
        with mock.patch.object(watcher.subprocess, "run") as run:
            context = watcher.fetch_jira_context(
                {},
                {"title": "Investigate PROJ-123", "description": ""},
                {
                    "enabled": True,
                    "base_url": "https://jira.example.com",
                    "custom_fields": {"Estimate`": "customfield_42"},
                },
            )

        self.assertEqual(context, "")
        run.assert_not_called()

    def test_default_helper_falls_back_to_packaged_module(self):
        completed = SimpleNamespace(returncode=0, stdout="Jira details", stderr="")
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(watcher, "REPOSITORY_ROOT", Path(temporary)),
            mock.patch.object(watcher.subprocess, "run", return_value=completed) as run,
        ):
            context = watcher.fetch_jira_context(
                {},
                {"title": "Investigate PROJ-123", "description": ""},
                {"enabled": True, "base_url": "https://jira.example.com"},
            )

        self.assertIn("Jira details", context)
        self.assertIn("--base-url https://jira.example.com", context)
        self.assertEqual(
            run.call_args.args[0][:3],
            [sys.executable, "-m", "eastwatch.jira.core"],
        )

    def test_custom_jira_fields_are_forwarded_to_helper(self):
        completed = SimpleNamespace(returncode=0, stdout="Jira details", stderr="")
        with mock.patch.object(watcher.subprocess, "run", return_value=completed) as run:
            watcher.fetch_jira_context(
                {},
                {"title": "Investigate PROJ-123", "description": ""},
                {
                    "enabled": True,
                    "base_url": "https://jira.example.com",
                    "custom_fields": {"Estimate": "customfield_42"},
                    "development_field": "customfield_43",
                },
            )

        command = run.call_args.args[0]
        custom_index = command.index("--custom-field")
        self.assertEqual(command[custom_index + 1], "Estimate=customfield_42")
        development_index = command.index("--development-field")
        self.assertEqual(command[development_index + 1], "customfield_43")


if __name__ == "__main__":
    unittest.main()
