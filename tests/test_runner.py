from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from subprocess import CompletedProcess

import pytest
import requests

from eastwatch import watcher
from eastwatch.runner import client as client_module
from eastwatch.runner import executor as executor_module
from eastwatch.runner.client import Lease, LeaseRejected, RunnerApiError, RunnerClient
from eastwatch.runner.daemon import RunnerDaemon
from eastwatch.runner.executor import (
    WorkspaceError,
    WorkspaceExecutor,
    canonical_checkout,
    load_conversation_meta,
    persist_conversation_meta,
    prepare_workspace,
    relative_to_root,
    session_mode,
)


class FakeClient:
    def __init__(self) -> None:
        self.started_calls = []
        self.completed = []
        self.failed = []
        self.heartbeats = []

    def started(self, job_id, generation, refs):
        # Snapshot refs: the executor mutates the same dict in place on the
        # mid-run session upgrade, so a stored reference would retroactively
        # change earlier recorded calls.
        self.started_calls.append((job_id, generation, dict(refs)))
        return True

    def complete(self, job_id, generation, state, *, result=None, error=None):
        self.completed.append((job_id, generation, state, result, error))
        return True

    def fail_before_start(self, job_id, generation, error):
        self.failed.append((job_id, generation, error))
        return True

    def heartbeat(self, active):
        self.heartbeats.append(active)

    def claim(self, free_slots, wait_seconds=20):
        return ()


def test_checkout_and_artifact_containment(tmp_path: Path) -> None:
    root = tmp_path / "home"
    checkout = root / "repos" / "gitlab.example.com" / "group" / "app"
    checkout.mkdir(parents=True)
    assert (
        canonical_checkout(root, "gitlab.example.com", "group/app")
        == checkout.resolve()
    )
    with pytest.raises(WorkspaceError):
        canonical_checkout(root, "gitlab.example.com", "../outside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "state").symlink_to(outside)
    with pytest.raises(WorkspaceError):
        relative_to_root(root / "state" / "run", root)


def test_qa_uses_forge_research_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "home"
    checkout = root / "repos" / "gitlab.example.com" / "group" / "app"
    worktree = root / "worktrees" / "group-app" / "issue-12"
    checkout.mkdir(parents=True)
    worktree.mkdir(parents=True)
    command = tmp_path / "glab-board"
    command.write_text("#!/bin/sh\n")
    monkeypatch.setattr(watcher, "forge_board_path", lambda: command)
    monkeypatch.setattr(
        "eastwatch.runner.executor.subprocess.run",
        lambda *args, **kwargs: CompletedProcess(
            args[0],
            0,
            json.dumps({"worktree": str(worktree), "branch": "issue-12"}),
            "",
        ),
    )
    conv = {
        "kind": "qa",
        "issue_iid": "12",
        "checkout": str(checkout),
        "host": "gitlab.example.com",
    }

    prepare_workspace(conv, root)

    assert conv["workspace_prepared"] is True
    assert conv["cwd"] == str(worktree.resolve())


def _envelope(**overrides) -> dict:
    envelope = {
        "host": "gitlab.example.com",
        "project_path": "group/app",
        "provider": "pi",
        "model": "gpt-5.6-sol",
        "effort": "high",
        "issue_iid": 12,
        "messages": [],
        "context": {},
        "trigger_kind": "agent::ready",
        "reply_target": {"kind": "issue"},
        "issue_url": "https://gitlab.example.com/group/app/-/issues/12",
    }
    envelope.update(overrides)
    return envelope


def _root_with_checkout(tmp_path: Path) -> Path:
    root = tmp_path / "home"
    (root / "repos" / "gitlab.example.com" / "group" / "app").mkdir(parents=True)
    return root


def _write_conversation_meta(root: Path, meta: dict) -> Path:
    session_dir = root / "state" / "conversations" / "group-app-12"
    session_dir.mkdir(parents=True)
    (session_dir / "conversation.json").write_text(json.dumps(meta))
    return session_dir


def test_conversation_adopts_persisted_session_meta(tmp_path: Path) -> None:
    root = _root_with_checkout(tmp_path)
    session_file = root / ".pi" / "agent" / "sessions" / "abc.jsonl"
    session_file.parent.mkdir(parents=True)
    session_file.write_text("{}")
    relpath = session_file.relative_to(root).as_posix()
    _write_conversation_meta(
        root,
        {
            "provider": "pi",
            "session_id": "sess-1",
            "session_file_relpath": relpath,
        },
    )
    executor = WorkspaceExecutor(root, FakeClient())

    conv = executor.conversation(_envelope())

    assert conv["session_id"] == "sess-1"
    assert conv["session_file"] == str(session_file.resolve())


def test_conversation_ignores_meta_on_provider_mismatch(tmp_path: Path) -> None:
    root = _root_with_checkout(tmp_path)
    session_file = root / ".pi" / "agent" / "sessions" / "abc.jsonl"
    session_file.parent.mkdir(parents=True)
    session_file.write_text("{}")
    relpath = session_file.relative_to(root).as_posix()
    _write_conversation_meta(
        root,
        {
            "provider": "codex",
            "session_id": "sess-1",
            "session_file_relpath": relpath,
        },
    )
    executor = WorkspaceExecutor(root, FakeClient())

    conv = executor.conversation(_envelope())

    assert conv["session_id"] is None
    assert conv["session_file"] is None


def test_conversation_ignores_meta_pointing_at_missing_file(tmp_path: Path) -> None:
    root = _root_with_checkout(tmp_path)
    _write_conversation_meta(
        root,
        {
            "provider": "pi",
            "session_id": "sess-1",
            "session_file_relpath": ".pi/agent/sessions/missing.jsonl",
        },
    )
    executor = WorkspaceExecutor(root, FakeClient())

    conv = executor.conversation(_envelope())

    assert conv["session_id"] is None
    assert conv["session_file"] is None


def test_conversation_ignores_corrupt_meta_without_raising(tmp_path: Path) -> None:
    root = _root_with_checkout(tmp_path)
    session_dir = root / "state" / "conversations" / "group-app-12"
    session_dir.mkdir(parents=True)
    (session_dir / "conversation.json").write_text("{not json")
    executor = WorkspaceExecutor(root, FakeClient())

    conv = executor.conversation(_envelope())

    assert conv["session_id"] is None
    assert conv["session_file"] is None


def test_session_mode_true_only_without_handles() -> None:
    assert session_mode({}) is True
    assert session_mode({"session_id": "sess-1"}) is False
    assert session_mode({"session_file": "/tmp/session.jsonl"}) is False


def test_persist_and_load_conversation_meta_round_trip(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    meta = {
        "provider": "pi",
        "session_id": "sess-2",
        "session_file_relpath": "a/b.jsonl",
    }

    persist_conversation_meta(session_dir, meta)

    assert load_conversation_meta(session_dir) == meta


def test_claim_does_not_retry_mutating_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def fail(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise requests.ConnectionError("lost response")

    monkeypatch.setattr(client_module.requests, "post", fail)
    with pytest.raises(RunnerApiError):
        RunnerClient("http://controller", "token").claim(1, wait_seconds=0)
    assert calls == 1


def test_daemon_keys_active_work_by_job_and_generation() -> None:
    release = threading.Event()

    class BlockingExecutor:
        def execute(self, lease, cancelled=None):
            release.wait(2)

    daemon = RunnerDaemon(
        FakeClient(), BlockingExecutor(), capacity=2, heartbeat_seconds=0.01
    )
    envelope = {"job_id": "same-job"}
    daemon.submit(Lease(envelope, 1, 10))
    daemon.submit(Lease(envelope, 2, 20))
    assert daemon.active_count() == 2
    assert {key for key, _target in daemon.heartbeat_targets()} == {
        ("same-job", 1),
        ("same-job", 2),
    }
    release.set()
    daemon.pool.shutdown(wait=True)


def test_stale_heartbeat_isolated_from_other_generation() -> None:
    release = threading.Event()

    class BlockingExecutor:
        def execute(self, lease, cancelled=None):
            release.wait(2)

    class HeartbeatClient(FakeClient):
        def heartbeat(self, active):
            super().heartbeat(active)
            if active[0]["lease_generation"] == 1:
                raise LeaseRejected("stale")

    daemon = RunnerDaemon(
        HeartbeatClient(), BlockingExecutor(), capacity=2, heartbeat_seconds=0.01
    )
    envelope = {"job_id": "job"}
    daemon.submit(Lease(envelope, 1, 10))
    daemon.submit(Lease(envelope, 2, 20))
    thread = threading.Thread(target=daemon.heartbeat_loop)
    thread.start()
    time.sleep(0.05)
    daemon.stop.set()
    thread.join(1)
    assert ("job", 1) in daemon.stale
    assert daemon.active[("job", 1)][2].is_set()
    assert any(call[0]["lease_generation"] == 2 for call in daemon.client.heartbeats)
    release.set()
    daemon.pool.shutdown(wait=True)


def _prepare_execute_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Wire up execute()'s side effects (forge, tmux) with fakes so the wait
    loop is driven entirely by our tmux_has_session side effects.

    Patches go through executor_module.watcher rather than the top-level
    `watcher` import: other test modules reload eastwatch.watcher via
    tests.support.load_watcher, which can leave executor.py holding a
    reference to a different module object than the one this file imports.
    """
    root = _root_with_checkout(tmp_path)
    worktree = root / "worktrees" / "group-app" / "issue-12"
    worktree.mkdir(parents=True)
    command = tmp_path / "glab-board"
    command.write_text("#!/bin/sh\n")
    monkeypatch.setattr(executor_module.watcher, "forge_board_path", lambda: command)
    monkeypatch.setattr(
        executor_module.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(
            args[0],
            0,
            json.dumps({"worktree": str(worktree), "branch": "issue-12"}),
            "",
        ),
    )
    monkeypatch.setattr(
        executor_module.watcher,
        "tmux_launch_worker",
        lambda *args, **kwargs: CompletedProcess(args, 0, "", ""),
    )
    monkeypatch.setattr(executor_module.time, "sleep", lambda _seconds: None)
    return root


def test_execute_reports_adopted_session_relpath_for_resumed_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _prepare_execute_env(tmp_path, monkeypatch)
    session_file = root / ".pi" / "agent" / "sessions" / "abc.jsonl"
    session_file.parent.mkdir(parents=True)
    session_file.write_text("{}")
    relpath = session_file.relative_to(root).as_posix()
    session_dir = _write_conversation_meta(
        root,
        {"provider": "pi", "session_id": "sess-1", "session_file_relpath": relpath},
    )

    def fake_tmux_has_session(_name: str) -> bool:
        run_dir = next((session_dir / "runs").iterdir())
        (run_dir / "result.json").write_text(
            json.dumps({"ok": True, "session_id": "sess-1"})
        )
        return True

    monkeypatch.setattr(
        executor_module.watcher, "tmux_has_session", fake_tmux_has_session
    )
    client = FakeClient()
    executor = WorkspaceExecutor(root, client)

    lease = Lease(
        _envelope(job_id="job-1", owner_username="alice"), 1, time.time() + 60
    )
    executor.execute(lease)

    assert len(client.started_calls) == 1
    assert client.started_calls[0][2]["session_file_relpath"] == relpath
    assert client.completed[0][2] == "succeeded"


def test_execute_upgrades_session_relpath_mid_run_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _prepare_execute_env(tmp_path, monkeypatch)
    session_dir = root / "state" / "conversations" / "group-app-12"
    calls = {"n": 0}

    def fake_tmux_has_session(_name: str) -> bool:
        calls["n"] += 1
        if calls["n"] == 2:
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "20260101_sess-new.jsonl").write_text("{}")
            run_dir = next((session_dir / "runs").iterdir())
            (run_dir / "result.json").write_text(
                json.dumps({"ok": True, "session_id": "sess-new"})
            )
        return True

    monkeypatch.setattr(
        executor_module.watcher, "tmux_has_session", fake_tmux_has_session
    )
    client = FakeClient()
    executor = WorkspaceExecutor(root, client)

    lease = Lease(
        _envelope(job_id="job-1", owner_username="alice"), 1, time.time() + 60
    )
    executor.execute(lease)

    assert len(client.started_calls) == 2
    assert client.started_calls[0][2]["session_file_relpath"] is None
    assert (
        client.started_calls[1][2]["session_file_relpath"]
        == "state/conversations/group-app-12/20260101_sess-new.jsonl"
    )
    assert client.completed[0][2] == "succeeded"


def test_execute_upgrade_client_error_does_not_fail_run_or_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _prepare_execute_env(tmp_path, monkeypatch)
    session_dir = root / "state" / "conversations" / "group-app-12"
    calls = {"n": 0}

    def fake_tmux_has_session(_name: str) -> bool:
        calls["n"] += 1
        if calls["n"] == 2:
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "20260101_sess-new.jsonl").write_text("{}")
        if calls["n"] == 3:
            run_dir = next((session_dir / "runs").iterdir())
            (run_dir / "result.json").write_text(
                json.dumps({"ok": True, "session_id": "sess-new"})
            )
        return True

    monkeypatch.setattr(
        executor_module.watcher, "tmux_has_session", fake_tmux_has_session
    )

    class ErrorOnUpgradeClient(FakeClient):
        def started(self, job_id, generation, refs):
            snapshot = dict(refs)
            self.started_calls.append((job_id, generation, snapshot))
            if len(self.started_calls) == 2:
                raise RunnerApiError("controller unreachable")
            return True

    client = ErrorOnUpgradeClient()
    executor = WorkspaceExecutor(root, client)

    lease = Lease(
        _envelope(job_id="job-1", owner_username="alice"), 1, time.time() + 60
    )
    executor.execute(lease)

    # Exactly one upgrade attempt, even though the run kept polling afterwards.
    assert len(client.started_calls) == 2
    assert (
        client.started_calls[1][2]["session_file_relpath"]
        == "state/conversations/group-app-12/20260101_sess-new.jsonl"
    )
    assert client.completed[0][2] == "succeeded"


def test_resume_trigger_messages_synthesizes_relabel_prompt() -> None:
    from eastwatch.runner.executor import resume_trigger_messages

    envelope = {"trigger_kind": "agent::ready"}
    synthesized = resume_trigger_messages(envelope, [], is_new=False)
    assert synthesized == [
        "The issue has been labeled `agent::ready` again. Pick the work back up per the issue."
    ]
    # New conversations, comment triggers, and explicit messages are untouched.
    assert resume_trigger_messages(envelope, [], is_new=True) == []
    assert resume_trigger_messages({"trigger_kind": "qa"}, [], is_new=False) == []
    assert resume_trigger_messages(envelope, ["hi"], is_new=False) == ["hi"]
