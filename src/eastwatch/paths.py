"""Repository paths shared by source modules and compatibility entrypoints."""

from __future__ import annotations

import sys
from pathlib import Path

from eastwatch.env import getenv


def repository_root() -> Path:
    """Return the source checkout containing scripts and compatibility wrappers."""
    override = getenv("EASTWATCH_REPO_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


REPOSITORY_ROOT = repository_root()
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"


def entrypoint_command(module: str, script_name: str) -> tuple[str, ...]:
    """Use a source wrapper when present, otherwise run the installed module."""
    wrapper = SCRIPTS_DIR / script_name
    if wrapper.is_file():
        return sys.executable, str(wrapper)
    return sys.executable, "-m", module
