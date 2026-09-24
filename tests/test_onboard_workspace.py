"""Integration tests for the hosted teammate onboarding script."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tests.support import REPOSITORY_ROOT

ONBOARD_WORKSPACE = REPOSITORY_ROOT / "deploy" / "host" / "onboard-workspace.sh"


def write_executable(path: Path, contents: str) -> None:
    path.write_text(contents)
    path.chmod(0o755)


def test_onboarding_assigns_pi_config_directory_to_workspace_user(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    checkout = tmp_path / "checkout"
    users_root = tmp_path / "users"
    install_calls = tmp_path / "install.calls"
    fake_bin.mkdir()
    (checkout / ".venv" / "bin").mkdir(parents=True)
    (checkout / "deploy" / "workspace").mkdir(parents=True)
    (checkout / "deploy" / "workspace" / "pi-models.json").write_text("{}\n")

    write_executable(fake_bin / "id", "#!/bin/sh\necho 0\n")
    write_executable(fake_bin / "docker", "#!/bin/sh\nexit 0\n")
    write_executable(
        checkout / ".venv" / "bin" / "bw-admin",
        '#!/bin/sh\nset -eu\nmkdir -p "$BW_WORKSPACE_USERS_ROOT/$2/home"\n',
    )
    write_executable(
        fake_bin / "install",
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'printf "%s\\n" "$*" >> "$INSTALL_CALLS"\n'
        'destination="${@: -1}"\n'
        'if [[ " $* " == *" -d "* ]]; then\n'
        '  mkdir -p "$destination"\n'
        "else\n"
        '  source="${@: -2:1}"\n'
        '  [[ -d $(dirname "$destination") ]]\n'
        '  cp "$source" "$destination"\n'
        "fi\n",
    )

    env = os.environ.copy()
    env.update(
        PATH=f"{fake_bin}:{env['PATH']}",
        INSTALL_CALLS=str(install_calls),
    )
    result = subprocess.run(
        [
            str(ONBOARD_WORKSPACE),
            "rmodi",
            "rmodi",
            "42",
            "--projects",
            '[{"host":"gitlab.example.com","path":"group/project","id":123}]',
            "--users-root",
            str(users_root),
            "--checkout",
            str(checkout),
            "--database",
            str(tmp_path / "controller.db"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "inside the workspace shell from step 1" in result.stdout
    assert "python -m eastwatch.runner.doctor" in result.stdout
    assert "bw doctor is the laptop remote client" in result.stdout
    assert "\n   bw doctor\n" not in result.stdout
    config_dir = users_root / "rmodi" / "home" / ".config"
    pi_dir = config_dir / "pi"
    agent_dir = pi_dir / "agent"
    calls = install_calls.read_text().splitlines()
    assert calls[0] == (f"-d -o 1000 -g 1000 -m 0755 {config_dir} {pi_dir} {agent_dir}")
    assert calls[1] == (
        f"-o 1000 -g 1000 -m 0644 "
        f"{checkout / 'deploy' / 'workspace' / 'pi-models.json'} "
        f"{agent_dir / 'models.json'}"
    )
