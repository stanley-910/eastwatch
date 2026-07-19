"""Environment-variable compatibility for the Eastwatch rename."""

from __future__ import annotations

import os

_CURRENT_PREFIX = "EASTWATCH_"
_LEGACY_PREFIX = "BOARD_WATCHER_"


def legacy_name(name: str) -> str | None:
    if not name.startswith(_CURRENT_PREFIX):
        return None
    return _LEGACY_PREFIX + name.removeprefix(_CURRENT_PREFIX)


def getenv(name: str) -> str | None:
    """Return the Eastwatch value, falling back to its one-release legacy alias."""
    value = os.environ.get(name)
    if value is not None:
        return value
    legacy = legacy_name(name)
    return os.environ.get(legacy) if legacy else None
