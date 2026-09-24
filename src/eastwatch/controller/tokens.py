from __future__ import annotations

from collections.abc import Mapping
from typing import TypeAlias

ProjectTokens: TypeAlias = str | Mapping[str, str]


def token_for_project(tokens: ProjectTokens, project_key: str) -> str:
    """Return the GitLab token for a project, preserving the legacy single-token API."""
    if isinstance(tokens, str):
        return tokens
    try:
        token = tokens[project_key]
    except KeyError as error:
        raise RuntimeError(
            f"missing GitLab token for project: {project_key}"
        ) from error
    if not token:
        raise RuntimeError(f"empty GitLab token for project: {project_key}")
    return token
