from __future__ import annotations

import argparse
import io
import json
import sqlite3
from pathlib import Path
from subprocess import CalledProcessError, CompletedProcess
from types import SimpleNamespace

import pytest
import yaml

from eastwatch import admin
from eastwatch.controller.store import token_digest


def arguments(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        workspace_id="alice",
        username="alice",
        user_id=42,
        database=str(tmp_path / "controller.db"),
        users_root=str(tmp_path / "users"),
        controller_url="http://bw-controller:8765",
        capacity=10,
        heartbeat_seconds=20,
        default_spec="pi:gpt-5.6-sol:high",
        allowed_spec=["pi:gpt-5.6-sol:high"],
        projects=[{"host": "gitlab.example.com", "path": "group/app"}],
        workspace_run_script="/opt/run-workspace",
    )


def test_create_workspace_writes_digest_and_mode_0600_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin.os, "geteuid", lambda: 0)
    monkeypatch.setattr(admin.os, "chown", lambda *args: None)
    monkeypatch.setattr(admin.secrets, "token_urlsafe", lambda size: "raw-runner-token")

    def run(argv, **kwargs):
        if argv[0] == "/opt/run-workspace":
            with sqlite3.connect(tmp_path / "controller.db") as connection:
                assert (
                    connection.execute(
                        "SELECT enabled FROM workspaces WHERE workspace_id = 'alice'"
                    ).fetchone()[0]
                    == 0
                )
        return CompletedProcess(argv, 0)

    monkeypatch.setattr(admin.subprocess, "run", run)

    assert admin.create_workspace(arguments(tmp_path)) == 0

    env_file = tmp_path / "users" / "alice" / "runner.env"
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert "BW_RUNNER_TOKEN=raw-runner-token" in env_file.read_text()
    with sqlite3.connect(tmp_path / "controller.db") as connection:
        row = connection.execute(
            "SELECT token_sha256, enabled, ready, host_root FROM workspaces WHERE workspace_id = 'alice'"
        ).fetchone()
    assert row == (
        token_digest("raw-runner-token"),
        1,
        0,
        str((tmp_path / "users" / "alice" / "home").resolve()),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("workspace_id", "../escape"),
        ("allowed_spec", ["pi:gpt-5.6-sol:high\nINJECTED=value"]),
        ("projects", [{"host": "gitlab.example.com", "path": "../outside"}]),
    ],
)
def test_invalid_workspace_input_is_rejected_before_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    monkeypatch.setattr(admin.os, "geteuid", lambda: 0)
    args = arguments(tmp_path)
    setattr(args, field, value)
    with pytest.raises(RuntimeError):
        admin.create_workspace(args)
    assert not (tmp_path / "users").exists()
    assert not (tmp_path / "controller.db").exists()


def test_container_failure_removes_registration_and_secret_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin.os, "geteuid", lambda: 0)
    monkeypatch.setattr(admin.os, "chown", lambda *args: None)
    monkeypatch.setattr(admin.secrets, "token_urlsafe", lambda size: "raw-runner-token")

    def run(argv, **kwargs):
        if argv[0] == "/opt/run-workspace":
            raise CalledProcessError(1, argv)
        return CompletedProcess(argv, 0)

    monkeypatch.setattr(admin.subprocess, "run", run)
    with pytest.raises(CalledProcessError):
        admin.create_workspace(arguments(tmp_path))

    assert not (tmp_path / "users" / "alice").exists()
    with sqlite3.connect(tmp_path / "controller.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] == 0


def add_project_arguments(tmp_path: Path) -> argparse.Namespace:
    controller_root = tmp_path / "controller"
    controller_root.mkdir()
    (controller_root / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "execution": {
                    "mode": "hosted",
                    "controller": {
                        "database": "/var/lib/eastwatch/controller.db",
                        "listen_host": "bw-controller-internal",
                        "listen_port": 8765,
                        "admins": ["admin"],
                        "approved_bots": [],
                    },
                },
                "projects": [],
            }
        )
    )
    (controller_root / "controller.env").write_text("EXISTING=value\n")
    workspace_root = tmp_path / "users" / "alice"
    workspace_root.mkdir(parents=True)
    (workspace_root / "runner.env").write_text(
        "BW_CONTROLLER_URL=http://bw-controller:8765\n"
        "BW_RUNNER_TOKEN=runner-secret\n"
        "BW_PROJECTS_JSON=[]\n"
    )
    return argparse.Namespace(
        workspace_id="alice",
        host="gitlab.example.com",
        project_path="group/app",
        project_id=77,
        controller_root=str(controller_root),
        users_root=str(tmp_path / "users"),
        controller_run_script="/opt/run-controller",
        workspace_run_script="/opt/run-workspace",
        token_stdin=True,
    )


def gitlab_response(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(json=lambda: payload, raise_for_status=lambda: None)


def test_add_hosted_project_routes_token_and_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = add_project_arguments(tmp_path)
    monkeypatch.setattr(admin.os, "geteuid", lambda: 0)
    monkeypatch.setattr(admin.sys, "stdin", io.StringIO("project-secret\n"))
    responses = iter(
        [
            gitlab_response({"id": 40846, "username": "project_77_bot"}),
            gitlab_response({"id": 77, "path_with_namespace": "group/app"}),
            gitlab_response([]),
        ]
    )
    monkeypatch.setattr(admin.requests, "get", lambda *args, **kwargs: next(responses))
    board_creates: list[dict] = []
    monkeypatch.setattr(
        admin.requests,
        "post",
        lambda *args, **kwargs: (
            board_creates.append(kwargs["data"])
            or gitlab_response({"id": 9, "name": "Agent Board"})
        ),
    )
    calls: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(
        admin.subprocess,
        "run",
        lambda argv, **kwargs: (
            calls.append((argv, kwargs)) or CompletedProcess(argv, 0)
        ),
    )

    assert admin.add_hosted_project(args) == 0

    root = Path(args.controller_root)
    config = yaml.safe_load((root / "config.yaml").read_text())
    assert config["projects"] == [
        {
            "host": "gitlab.example.com",
            "path": "group/app",
            "id": 77,
            "bot_username": "project_77_bot",
            "bot_user_id": 40846,
            "bot_token_env": "EASTWATCH_GITLAB_TOKEN_PROJECT_77",
            "triggers": ["agent::ready", "agent::ready-research", "mention", "emoji"],
        }
    ]
    assert config["execution"]["controller"]["approved_bots"] == ["project_77_bot"]
    assert (root / "controller.env").read_text() == (
        "EXISTING=value\nEASTWATCH_GITLAB_TOKEN_PROJECT_77=project-secret\n"
    )
    runner = (Path(args.users_root) / "alice" / "runner.env").read_text().splitlines()
    projects = json.loads(
        next(
            line.split("=", 1)[1]
            for line in runner
            if line.startswith("BW_PROJECTS_JSON=")
        )
    )
    assert projects == [{"host": "gitlab.example.com", "path": "group/app", "id": 77}]
    assert [call[0] for call in calls] == [
        ["/opt/run-controller"],
        ["/opt/run-workspace", "alice"],
    ]
    assert all("project-secret" not in repr(call) for call in calls)
    assert board_creates == [{"name": "Agent Board"}]


def test_add_hosted_project_rejects_personal_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = add_project_arguments(tmp_path)
    monkeypatch.setattr(admin.os, "geteuid", lambda: 0)
    monkeypatch.setattr(admin.sys, "stdin", io.StringIO("personal-secret\n"))
    responses = iter(
        [
            gitlab_response({"id": 42, "username": "alice"}),
            gitlab_response({"id": 77, "path_with_namespace": "group/app"}),
        ]
    )
    monkeypatch.setattr(admin.requests, "get", lambda *args, **kwargs: next(responses))

    with pytest.raises(RuntimeError, match="project access token"):
        admin.add_hosted_project(args)


def test_add_hosted_project_exact_rerun_does_not_recreate_containers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = add_project_arguments(tmp_path)
    monkeypatch.setattr(admin.os, "geteuid", lambda: 0)
    payloads = [
        {"id": 40846, "username": "project_77_bot"},
        {"id": 77, "path_with_namespace": "group/app"},
        [{"id": 9, "name": "Agent Board"}],
    ]
    monkeypatch.setattr(
        admin.requests,
        "get",
        lambda *args, **kwargs: gitlab_response(payloads.pop(0)),
    )
    monkeypatch.setattr(
        admin.requests, "post", lambda *args, **kwargs: pytest.fail("board exists")
    )
    monkeypatch.setattr(admin.sys, "stdin", io.StringIO("project-secret\n"))
    calls: list[list[str]] = []
    monkeypatch.setattr(
        admin.subprocess,
        "run",
        lambda argv, **kwargs: calls.append(argv) or CompletedProcess(argv, 0),
    )
    admin.add_hosted_project(args)
    calls.clear()
    payloads.extend(
        [
            {"id": 40846, "username": "project_77_bot"},
            {"id": 77, "path_with_namespace": "group/app"},
            [{"id": 9, "name": "Agent Board"}],
        ]
    )
    monkeypatch.setattr(admin.sys, "stdin", io.StringIO("project-secret\n"))

    assert admin.add_hosted_project(args) == 0
    assert calls == []
