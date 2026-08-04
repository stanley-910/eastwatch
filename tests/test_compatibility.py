"""One-release compatibility contracts for the Eastwatch rename."""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import os
import tomllib
import unittest
from unittest import mock

from eastwatch.env import getenv
from tests.support import REPOSITORY_ROOT


class EnvironmentCompatibilityTest(unittest.TestCase):
    def test_legacy_environment_variable_is_fallback(self):
        with mock.patch.dict(
            os.environ,
            {"BOARD_WATCHER_STATE_DIR": "/legacy/state"},
            clear=True,
        ):
            self.assertEqual(getenv("EASTWATCH_STATE_DIR"), "/legacy/state")

    def test_eastwatch_environment_variable_takes_precedence(self):
        with mock.patch.dict(
            os.environ,
            {
                "EASTWATCH_STATE_DIR": "/current/state",
                "BOARD_WATCHER_STATE_DIR": "/legacy/state",
            },
            clear=True,
        ):
            self.assertEqual(getenv("EASTWATCH_STATE_DIR"), "/current/state")


class WrapperCompatibilityTest(unittest.TestCase):
    def test_source_wrappers_do_not_mask_legacy_repo_root(self):
        wrappers = ("eastwatch", "watcher.py", "fleet_tui.py")
        with mock.patch.dict(
            os.environ,
            {"BOARD_WATCHER_REPO_ROOT": "/legacy/repository"},
            clear=False,
        ):
            os.environ.pop("EASTWATCH_REPO_ROOT", None)
            for index, wrapper in enumerate(wrappers):
                with self.subTest(wrapper=wrapper):
                    loader = importlib.machinery.SourceFileLoader(
                        f"_eastwatch_wrapper_{index}",
                        str(REPOSITORY_ROOT / wrapper),
                    )
                    spec = importlib.util.spec_from_loader(loader.name, loader)
                    self.assertIsNotNone(spec)
                    module = importlib.util.module_from_spec(spec)
                    loader.exec_module(module)
                    self.assertNotIn("EASTWATCH_REPO_ROOT", os.environ)


class PackageCompatibilityTest(unittest.TestCase):
    def test_legacy_package_namespace_resolves_modules(self):
        legacy = importlib.import_module("board_watcher.paths")
        current = importlib.import_module("eastwatch.paths")

        self.assertEqual(legacy.repository_root(), current.repository_root())

    def test_legacy_console_script_targets_eastwatch(self):
        project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text())[
            "project"
        ]

        self.assertEqual(project["scripts"]["eastwatch"], "eastwatch.watcher:cli")
        self.assertEqual(project["scripts"]["board-watcher"], "eastwatch.watcher:cli")


if __name__ == "__main__":
    unittest.main()
