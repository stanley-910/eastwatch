from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from eastwatch import watcher
from eastwatch.runner.client import RunnerClient
from eastwatch.runner.executor import WorkspaceError, canonical_checkout


def command_output(argv: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, ""
    return result.returncode == 0, result.stdout.strip()


def main() -> int:
    projects = json.loads(os.environ.get("BW_PROJECTS_JSON", "[]"))
    required_model = os.environ.get("BW_REQUIRED_MODEL", "")
    git_name_ok, git_name = command_output(["git", "config", "--global", "user.name"])
    git_email_ok, git_email = command_output(
        ["git", "config", "--global", "user.email"]
    )
    models_ok, models = command_output(["pi", "--list-models"])
    auth_ok = bool(projects) and all(
        command_output(["glab", "auth", "status", "--hostname", str(project["host"])])[
            0
        ]
        for project in projects
    )
    try:
        repositories_ok = bool(projects) and all(
            (
                canonical_checkout(
                    Path.home(), str(project["host"]), str(project["path"])
                )
                / ".git"
            ).exists()
            for project in projects
        )
    except (KeyError, TypeError, WorkspaceError):
        repositories_ok = False
    checks = {
        "git": shutil.which("git") is not None,
        "glab": shutil.which("glab") is not None,
        "pi": shutil.which("pi") is not None,
        "tmux": shutil.which("tmux") is not None,
        "zsh": shutil.which("zsh") is not None,
        "forge": watcher.forge_board_path().is_file(),
        "git-name": git_name_ok and bool(git_name),
        "git-email": git_email_ok and "@" in git_email,
        "gitlab-auth": auth_ok,
        "repositories": repositories_ok,
        "pi-models": models_ok
        and bool(models)
        and (not required_model or required_model in models),
    }
    try:
        with tempfile.NamedTemporaryFile(
            dir=Path.home(), prefix=".bw-doctor-", delete=True
        ):
            checks["home-write"] = True
    except OSError:
        checks["home-write"] = False
    client = RunnerClient(
        os.environ.get("BW_CONTROLLER_URL", ""),
        os.environ.get("BW_RUNNER_TOKEN", ""),
    )
    try:
        client.heartbeat([])
        checks["controller"] = True
    except Exception:
        checks["controller"] = False
    if all(checks.values()):
        try:
            client.mark_ready()
            checks["dispatch-ready"] = True
        except Exception:
            checks["dispatch-ready"] = False
    else:
        checks["dispatch-ready"] = False
    for name, passed in checks.items():
        print(f"{'OK' if passed else 'FAIL'} {name}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
