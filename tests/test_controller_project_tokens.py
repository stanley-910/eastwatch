from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from eastwatch import watcher
from eastwatch.controller.config import controller_config, project_tokens
from eastwatch.controller.dispatch import HostedDispatcher
from eastwatch.controller.store import ControllerStore


def hosted_config(projects: list[dict], **controller_overrides: object) -> dict:
    controller = {
        "database": "/tmp/controller.db",
        "listen_host": "bw-controller-internal",
        "listen_port": 8765,
        "admins": ["admin"],
        **controller_overrides,
    }
    return {
        "execution": {"mode": "hosted", "controller": controller},
        "projects": projects,
    }


def project(path: str, token_env: str | None = None) -> dict:
    value = {
        "host": "gitlab.example.com",
        "path": path,
        "id": len(path),
        "bot_username": f"{path.replace('/', '-')}-bot",
        "bot_user_id": len(path) + 100,
        "triggers": ["agent::ready"],
    }
    if token_env is not None:
        value["bot_token_env"] = token_env
    return value


def test_controller_config_allows_per_project_token_envs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(hosted_config([project("group/app", "APP_TOKEN")]))
    )
    monkeypatch.setattr(watcher, "CONFIG_PATH", config_path)

    _, controller = controller_config()

    assert "bot_token_env" not in controller


def test_controller_config_requires_default_or_project_token_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(hosted_config([project("group/app")])))
    monkeypatch.setattr(watcher, "CONFIG_PATH", config_path)

    with pytest.raises(RuntimeError, match="group/app.*bot_token_env"):
        controller_config()


def test_project_tokens_use_override_and_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = hosted_config(
        [project("group/app", "APP_TOKEN"), project("group/docs")],
        bot_token_env="DEFAULT_TOKEN",
    )
    monkeypatch.setenv("APP_TOKEN", "app-secret")
    monkeypatch.setenv("DEFAULT_TOKEN", "default-secret")

    assert project_tokens(cfg, cfg["execution"]["controller"]) == {
        "gitlab.example.com/group/app": "app-secret",
        "gitlab.example.com/group/docs": "default-secret",
    }


def test_project_tokens_fail_closed_when_bound_env_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = hosted_config([project("group/app", "APP_TOKEN")])
    monkeypatch.delenv("APP_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="APP_TOKEN.*group/app"):
        project_tokens(cfg, cfg["execution"]["controller"])


def test_hosted_cycle_routes_each_project_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = hosted_config(
        [project("group/app", "APP_TOKEN"), project("group/docs", "DOCS_TOKEN")]
    )
    store = ControllerStore(tmp_path / "controller.db")
    store.migrate()
    dispatcher = HostedDispatcher(store, frozenset({"admin"}), frozenset())
    observed: list[tuple[str, str]] = []

    class FakeGitLab:
        def __init__(self, host: str, token: str) -> None:
            observed.append((host, token))

    monkeypatch.setattr(watcher, "GitLab", FakeGitLab)
    monkeypatch.setattr(
        watcher, "hosted_comment_events", lambda gl, proj, cursor: ([], cursor)
    )
    monkeypatch.setattr(watcher, "hosted_label_fires", lambda gl, proj, label: [])

    watcher.hosted_cycle(
        cfg,
        store,
        dispatcher,
        {
            "gitlab.example.com/group/app": "app-secret",
            "gitlab.example.com/group/docs": "docs-secret",
        },
    )

    assert observed == [
        ("gitlab.example.com", "app-secret"),
        ("gitlab.example.com", "docs-secret"),
    ]
