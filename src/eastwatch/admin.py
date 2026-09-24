from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests
import yaml

from eastwatch.controller.store import ControllerStore, token_digest


def create_workspace(args: argparse.Namespace) -> int:
    if os.geteuid() != 0:
        raise RuntimeError("create-workspace must run as root")
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,31}", args.workspace_id) is None:
        raise RuntimeError("workspace_id must be a lowercase filesystem-safe slug")
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", args.username) is None:
        raise RuntimeError("username is not a valid GitLab username")
    if args.user_id < 1:
        raise RuntimeError("user_id must be positive")
    if (
        not isinstance(args.projects, list)
        or not args.projects
        or any(
            not isinstance(project, dict)
            or re.fullmatch(
                r"[A-Za-z0-9.-]+(?::[0-9]+)?", str(project.get("host") or "")
            )
            is None
            or re.fullmatch(
                r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", str(project.get("path") or "")
            )
            is None
            or ".." in str(project.get("path") or "").split("/")
            for project in args.projects
        )
    ):
        raise RuntimeError(
            "--projects must be a non-empty JSON list of safe host/path mappings"
        )
    if not 1 <= args.capacity <= 10:
        raise RuntimeError("capacity must be between 1 and 10")
    if not 5 <= args.heartbeat_seconds <= 300:
        raise RuntimeError("heartbeat seconds must be between 5 and 300")
    spec_pattern = re.compile(r"pi:[A-Za-z0-9._-]+(?::[a-z]+)?\Z")
    allowed_specs = tuple(dict.fromkeys(args.allowed_spec))
    if any(spec_pattern.fullmatch(spec) is None for spec in allowed_specs):
        raise RuntimeError("model specs must match provider:model[:effort]")
    if args.default_spec not in allowed_specs:
        raise RuntimeError("default spec must appear in --allowed-spec")
    if any(character in args.controller_url for character in ("\n", "\r", "\x00")):
        raise RuntimeError("controller URL contains an unsafe env-file character")

    root = Path(args.users_root) / args.workspace_id
    home = root / "home"
    env_file = root / "runner.env"
    if root.exists() or env_file.exists():
        raise RuntimeError(f"workspace filesystem already exists: {root}")
    token = secrets.token_urlsafe(32)
    store = ControllerStore(Path(args.database))
    store.migrate()
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO workspaces(
                workspace_id, owner_username, owner_user_id, token_sha256,
                enabled, ready, capacity, default_spec, allowed_specs_json,
                container_name, host_root, created_at
            ) VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?)
            """,
            (
                args.workspace_id,
                args.username,
                args.user_id,
                token_digest(token),
                args.capacity,
                args.default_spec,
                json.dumps(allowed_specs, sort_keys=True),
                f"bw-workspace-{args.workspace_id}",
                str(home.resolve()),
                time.time(),
            ),
        )
    temporary_env = root / f".runner.env.{os.getpid()}.tmp"
    try:
        home.mkdir(parents=True, exist_ok=False)
        os.chown(home, 1000, 1000)
        home.chmod(0o700)
        env_payload = (
            "\n".join(
                (
                    f"BW_CONTROLLER_URL={args.controller_url}",
                    f"BW_RUNNER_TOKEN={token}",
                    f"BW_RUNNER_CAPACITY={args.capacity}",
                    f"BW_HEARTBEAT_SECONDS={args.heartbeat_seconds}",
                    f"BW_PROJECTS_JSON={json.dumps(args.projects, separators=(',', ':'))}",
                    f"BW_REQUIRED_MODEL={args.default_spec.split(':', 1)[1].split(':', 1)[0]}",
                )
            )
            + "\n"
        )
        descriptor = os.open(
            temporary_env,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w") as stream:
            stream.write(env_payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_env, env_file)
        env_file.chmod(0o600)
        subprocess.run([args.workspace_run_script, args.workspace_id], check=True)
        with store.transaction() as connection:
            connection.execute(
                "UPDATE workspaces SET enabled = 1 WHERE workspace_id = ?",
                (args.workspace_id,),
            )
    except BaseException:
        subprocess.run(
            ["docker", "rm", "-f", f"bw-workspace-{args.workspace_id}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        temporary_env.unlink(missing_ok=True)
        shutil.rmtree(root, ignore_errors=True)
        with store.transaction() as connection:
            connection.execute(
                "DELETE FROM workspaces WHERE workspace_id = ?", (args.workspace_id,)
            )
        raise
    print(f"workspace created for @{args.username}; enter it with:")
    print(f"sudo docker exec -it bw-workspace-{args.workspace_id} zsh -l")
    return 0


def atomic_replace(path: Path, payload: str) -> None:
    current = path.stat()
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, current.st_mode & 0o777
    )
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(temporary, current.st_uid, current.st_gid)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def replace_env_value(payload: str, name: str, value: str) -> str:
    replacement = f"{name}={value}"
    lines = payload.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(f"{name}="):
            lines[index] = replacement
            break
    else:
        lines.append(replacement)
    return "\n".join(lines) + "\n"


def add_hosted_project(args: argparse.Namespace) -> int:
    if os.geteuid() != 0:
        raise RuntimeError("add-hosted-project must run as root")
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,31}", args.workspace_id) is None:
        raise RuntimeError("workspace_id must be a lowercase filesystem-safe slug")
    if re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]+)?", args.host) is None:
        raise RuntimeError("host is not valid")
    if re.fullmatch(
        r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", args.project_path
    ) is None or ".." in args.project_path.split("/"):
        raise RuntimeError("project path is not valid")
    if args.project_id < 1:
        raise RuntimeError("project id must be positive")
    if not args.token_stdin:
        raise RuntimeError("project token must be supplied with --token-stdin")
    token = sys.stdin.read().strip()
    if not token or any(character in token for character in ("\n", "\r", "\x00")):
        raise RuntimeError("project token is empty or contains an unsafe character")

    headers = {"PRIVATE-TOKEN": token}
    api_root = f"https://{args.host}/api/v4"
    user_response = requests.get(f"{api_root}/user", headers=headers, timeout=15)
    user_response.raise_for_status()
    project_response = requests.get(
        f"{api_root}/projects/{args.project_id}",
        headers=headers,
        timeout=15,
    )
    project_response.raise_for_status()
    bot = user_response.json()
    project_identity = project_response.json()
    bot_username = str(bot.get("username") or "")
    bot_user_id = int(bot.get("id") or 0)
    actual_path = str(project_identity.get("path_with_namespace") or "")
    if not bot_username or bot_user_id < 1:
        raise RuntimeError("project token returned an invalid bot identity")
    if re.fullmatch(rf"project_{args.project_id}_bot(?:_.+)?", bot_username) is None:
        raise RuntimeError("token must be a project access token owned by this project")
    if (
        int(project_identity.get("id") or 0) != args.project_id
        or actual_path != args.project_path
    ):
        raise RuntimeError(
            f"project token resolves to {actual_path or 'unknown'}, expected {args.project_path}"
        )
    boards_response = requests.get(
        f"{api_root}/projects/{args.project_id}/boards",
        headers=headers,
        timeout=15,
    )
    boards_response.raise_for_status()
    if not boards_response.json():
        created_board = requests.post(
            f"{api_root}/projects/{args.project_id}/boards",
            headers=headers,
            data={"name": "Agent Board"},
            timeout=15,
        )
        created_board.raise_for_status()

    controller_root = Path(args.controller_root)
    config_path = controller_root / "config.yaml"
    controller_env_path = controller_root / "controller.env"
    runner_env_path = Path(args.users_root) / args.workspace_id / "runner.env"
    for path in (config_path, controller_env_path, runner_env_path):
        if not path.is_file():
            raise RuntimeError(f"missing hosted deployment file: {path}")

    original = {
        config_path: config_path.read_text(),
        controller_env_path: controller_env_path.read_text(),
        runner_env_path: runner_env_path.read_text(),
    }
    config = yaml.safe_load(original[config_path]) or {}
    execution = config.get("execution") or {}
    controller = execution.get("controller") or {}
    if execution.get("mode") != "hosted" or not isinstance(controller, dict):
        raise RuntimeError("controller config must use execution.mode: hosted")

    token_env = f"EASTWATCH_GITLAB_TOKEN_PROJECT_{args.project_id}"
    project_payload = {
        "host": args.host,
        "path": args.project_path,
        "id": args.project_id,
        "bot_username": bot_username,
        "bot_user_id": bot_user_id,
        "bot_token_env": token_env,
        "triggers": ["agent::ready", "agent::ready-research", "mention", "emoji"],
    }
    projects = config.setdefault("projects", [])
    if not isinstance(projects, list):
        raise RuntimeError("controller projects must be a list")
    configured = next(
        (
            item
            for item in projects
            if isinstance(item, dict)
            and item.get("host") == args.host
            and item.get("path") == args.project_path
        ),
        None,
    )
    if configured is None:
        projects.append(project_payload)
    else:
        configured.update(
            {key: value for key, value in project_payload.items() if key != "triggers"}
        )
        configured.setdefault("triggers", project_payload["triggers"])
    approved_bots = controller.setdefault("approved_bots", [])
    if not isinstance(approved_bots, list):
        raise RuntimeError("execution.controller.approved_bots must be a list")
    if bot_username not in approved_bots:
        approved_bots.append(bot_username)

    controller_env = replace_env_value(original[controller_env_path], token_env, token)
    runner_lines = original[runner_env_path].splitlines()
    project_line = next(
        (
            index
            for index, line in enumerate(runner_lines)
            if line.startswith("BW_PROJECTS_JSON=")
        ),
        None,
    )
    if project_line is None:
        raise RuntimeError(f"missing BW_PROJECTS_JSON in {runner_env_path}")
    runner_projects = json.loads(runner_lines[project_line].split("=", 1)[1])
    if not isinstance(runner_projects, list):
        raise RuntimeError("BW_PROJECTS_JSON must contain a list")
    runner_project = next(
        (
            item
            for item in runner_projects
            if isinstance(item, dict)
            and item.get("host") == args.host
            and item.get("path") == args.project_path
        ),
        None,
    )
    runner_payload = {
        "host": args.host,
        "path": args.project_path,
        "id": args.project_id,
    }
    if runner_project is None:
        runner_projects.append(runner_payload)
    else:
        runner_project.update(runner_payload)
    runner_lines[project_line] = (
        f"BW_PROJECTS_JSON={json.dumps(runner_projects, separators=(',', ':'))}"
    )
    runner_env = "\n".join(runner_lines) + "\n"
    config_payload = yaml.safe_dump(config, sort_keys=False)

    changed = {
        config_path: config_payload != original[config_path],
        controller_env_path: controller_env != original[controller_env_path],
        runner_env_path: runner_env != original[runner_env_path],
    }
    if not any(changed.values()):
        print(
            f"project {args.project_path} already routes to workspace {args.workspace_id}"
        )
        return 0

    try:
        if changed[config_path]:
            atomic_replace(config_path, config_payload)
        if changed[controller_env_path]:
            atomic_replace(controller_env_path, controller_env)
        if changed[runner_env_path]:
            atomic_replace(runner_env_path, runner_env)
        if changed[config_path] or changed[controller_env_path]:
            subprocess.run(
                [args.controller_run_script],
                check=True,
                env={**os.environ, "BW_CONTROLLER_ROOT": str(controller_root)},
            )
        if changed[runner_env_path]:
            subprocess.run(
                [args.workspace_run_script, args.workspace_id],
                check=True,
                env={**os.environ, "BW_WORKSPACE_USERS_ROOT": args.users_root},
            )
    except BaseException:
        for path, payload in original.items():
            atomic_replace(path, payload)
        if changed[config_path] or changed[controller_env_path]:
            subprocess.run(
                [args.controller_run_script],
                check=False,
                env={**os.environ, "BW_CONTROLLER_ROOT": str(controller_root)},
            )
        if changed[runner_env_path]:
            subprocess.run(
                [args.workspace_run_script, args.workspace_id],
                check=False,
                env={**os.environ, "BW_WORKSPACE_USERS_ROOT": args.users_root},
            )
        raise

    print(
        f"project {args.project_path} routes through {token_env} "
        f"to workspace {args.workspace_id}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bw-admin")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-workspace")
    create.add_argument("workspace_id")
    create.add_argument("username")
    create.add_argument("user_id", type=int)
    create.add_argument(
        "--database",
        default="/srv/eastwatch/controller/data/controller.db",
    )
    create.add_argument("--users-root", default="/srv/eastwatch/users")
    create.add_argument("--controller-url", default="http://bw-controller:8765")
    create.add_argument("--capacity", type=int, default=10)
    create.add_argument("--heartbeat-seconds", type=float, default=20)
    create.add_argument("--default-spec", required=True)
    create.add_argument("--allowed-spec", action="append", required=True)
    create.add_argument("--projects", type=json.loads, required=True)
    create.add_argument(
        "--workspace-run-script",
        default="/opt/eastwatch/deploy/workspace/run.sh",
    )
    add_project = sub.add_parser("add-hosted-project")
    add_project.add_argument("workspace_id")
    add_project.add_argument("host")
    add_project.add_argument("project_path")
    add_project.add_argument("project_id", type=int)
    add_project.add_argument("--controller-root", default="/srv/eastwatch/controller")
    add_project.add_argument("--users-root", default="/srv/eastwatch/users")
    add_project.add_argument(
        "--controller-run-script",
        default="/opt/eastwatch/deploy/controller/run.sh",
    )
    add_project.add_argument(
        "--workspace-run-script",
        default="/opt/eastwatch/deploy/workspace/run.sh",
    )
    add_project.add_argument("--token-stdin", action="store_true", required=True)
    args = parser.parse_args(argv)
    if args.command == "create-workspace":
        return create_workspace(args)
    if args.command == "add-hosted-project":
        return add_hosted_project(args)
    raise RuntimeError(f"unsupported admin command: {args.command}")
