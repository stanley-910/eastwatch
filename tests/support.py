"""Shared paths and import helpers for tests."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from types import ModuleType

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"


def load_watcher(temp_root: Path) -> ModuleType:
    """Import watcher with isolated state paths derived from ``temp_root``."""
    os.environ["EASTWATCH_STATE_DIR"] = str(temp_root / "state")
    os.environ["EASTWATCH_LOG_DIR"] = str(temp_root / "logs")
    os.environ["EASTWATCH_CONFIG_DIR"] = str(temp_root / "config")
    sys.modules.pop("eastwatch.watcher", None)
    return importlib.import_module("eastwatch.watcher")
