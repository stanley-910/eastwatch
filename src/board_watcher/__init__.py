"""Compatibility namespace for the former ``board_watcher`` package.

Remove after the one-release Eastwatch migration window.
"""

from __future__ import annotations

import eastwatch as _eastwatch

__path__ = _eastwatch.__path__
