#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "textual>=6.6,<7",
#     "pyyaml>=6",
# ]
# ///
"""Compatibility entrypoint for the packaged fleet console."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if (
    "EASTWATCH_REPO_ROOT" not in os.environ
    and "BOARD_WATCHER_REPO_ROOT" not in os.environ
):
    os.environ["EASTWATCH_REPO_ROOT"] = str(ROOT)
sys.path.insert(0, str(SRC))

from eastwatch.fleet.tui import main  # noqa: E402


if __name__ == "__main__":
    main()
