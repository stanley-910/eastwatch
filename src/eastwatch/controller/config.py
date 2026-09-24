from __future__ import annotations

import os

import yaml

from eastwatch import watcher


def project_tokens(cfg: dict, controller: dict) -> dict[str, str]:
    default_env = controller.get("bot_token_env")
    tokens: dict[str, str] = {}
    for project in cfg.get("projects") or ():
        project_key = watcher.project_config_key(project)
        token_env = project.get("bot_token_env") or default_env
        if not token_env:
            raise RuntimeError(f"project {project['path']} requires bot_token_env")
        token = os.environ.get(str(token_env))
        if not token:
            raise RuntimeError(
                f"missing board-bot token environment variable {token_env} "
                f"for project {project['path']}"
            )
        tokens[project_key] = token
    return tokens


def controller_config() -> tuple[dict, dict]:
    if not watcher.CONFIG_PATH.is_file():
        raise RuntimeError(f"missing config: {watcher.CONFIG_PATH}")
    cfg = yaml.safe_load(watcher.CONFIG_PATH.read_text())
    execution = cfg.get("execution") or {}
    if execution.get("mode") != "hosted":
        raise RuntimeError("bw-controller requires execution.mode: hosted")
    controller = execution.get("controller") or {}
    required = ("database", "listen_host", "listen_port", "admins")
    missing = [field for field in required if controller.get(field) in (None, "", [])]
    if missing:
        raise RuntimeError(f"missing hosted controller settings: {', '.join(missing)}")
    default_token_env = controller.get("bot_token_env")
    if default_token_env is not None and not (
        isinstance(default_token_env, str) and default_token_env
    ):
        raise RuntimeError(
            "execution.controller.bot_token_env must be a non-empty string"
        )
    for project in cfg.get("projects") or ():
        token_env = project.get("bot_token_env")
        if token_env is not None and not (isinstance(token_env, str) and token_env):
            raise RuntimeError(
                f"project {project.get('path')} bot_token_env must be a non-empty string"
            )
        if not token_env and not default_token_env:
            raise RuntimeError(f"project {project.get('path')} requires bot_token_env")
    admins = controller["admins"]
    approved_bots = controller.get("approved_bots", [])
    if not isinstance(admins, list) or not all(
        isinstance(item, str) and item for item in admins
    ):
        raise RuntimeError(
            "execution.controller.admins must be a non-empty username list"
        )
    if not isinstance(approved_bots, list) or not all(
        isinstance(item, str) and item for item in approved_bots
    ):
        raise RuntimeError("execution.controller.approved_bots must be a username list")
    try:
        port = int(controller["listen_port"])
        lease = float(controller.get("lease_seconds", 90))
        heartbeat = float(controller.get("heartbeat_seconds", 20))
        long_poll = float(controller.get("long_poll_seconds", 20))
    except (TypeError, ValueError) as error:
        raise RuntimeError(
            "controller port and timing settings must be numeric"
        ) from error
    if not 1 <= port <= 65535:
        raise RuntimeError(
            "execution.controller.listen_port must be between 1 and 65535"
        )
    if not 30 <= lease <= 600:
        raise RuntimeError(
            "execution.controller.lease_seconds must be between 30 and 600"
        )
    if not 5 <= heartbeat < lease / 2:
        raise RuntimeError(
            "heartbeat_seconds must be at least 5 and less than half the lease"
        )
    if not 0 <= long_poll <= 25:
        raise RuntimeError(
            "execution.controller.long_poll_seconds must be between 0 and 25"
        )
    controller = dict(controller)
    controller.update(
        listen_port=port,
        lease_seconds=lease,
        heartbeat_seconds=heartbeat,
        long_poll_seconds=long_poll,
    )
    return cfg, controller
