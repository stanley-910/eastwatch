from __future__ import annotations

import json
import shutil
from contextlib import closing
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from eastwatch import watcher
from eastwatch.controller.dispatch import Assignee, DispatchCandidate, HostedDispatcher
from eastwatch.controller.models import StaleLeaseError
from eastwatch.controller.retention import MERGE_RETENTION_SECONDS, RetentionManager
from eastwatch.controller.store import ControllerStore, token_digest
from eastwatch.remote import HostedFleet
from eastwatch.runner.cleanup import CleanupBusy, CleanupExecutor


def seeded_store(path: Path, merged_at: float) -> ControllerStore:
    store = ControllerStore(path)
    store.migrate()
    envelope = {
        "schema_version": 1,
        "job_id": "job-1",
        "conversation_key": "gitlab.example.com/group/app#12",
        "host": "gitlab.example.com",
        "project_path": "group/app",
        "project_id": 7,
        "issue_iid": 12,
        "issue_url": "https://gitlab.example.com/group/app/-/issues/12",
        "trigger_kind": "agent::ready",
        "trigger_event_key": "event-1",
        "owner_username": "alice",
        "provider": "pi",
        "model": "gpt-5.6-sol",
        "effort": "high",
        "messages": [],
        "reply_target": {"kind": "issue", "iid": 12},
    }
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO projects(project_key, host, project_path, project_id, bootstrapped) "
            "VALUES ('gitlab.example.com/group/app', 'gitlab.example.com', 'group/app', 7, 1)"
        )
        connection.execute(
            """
            INSERT INTO workspaces(
                workspace_id, owner_username, owner_user_id, token_sha256,
                capacity, default_spec, allowed_specs_json,
                container_name, host_root, created_at
            ) VALUES ('alice', 'alice', 42, ?, 10, 'pi:gpt-5.6-sol:high', '[]',
                      'bw-workspace-alice', '/srv/eastwatch/users/alice/home', 1)
            """,
            (token_digest("token"),),
        )
        connection.execute(
            """
            INSERT INTO conversations(
                conversation_key, project_key, issue_iid, workspace_id,
                status, provider, model, effort, current_job_id,
                session_refs_json, updated_at
            ) VALUES ('gitlab.example.com/group/app#12', 'gitlab.example.com/group/app', 12,
                      'alice', 'succeeded', 'pi', 'gpt-5.6-sol', 'high', 'job-1',
                      '{"session_file_relpath":".pi/agent/sessions/run-1.jsonl"}', 2)
            """
        )
        connection.execute(
            """
            INSERT INTO jobs(
                job_id, conversation_key, workspace_id, trigger_event_key,
                state, envelope_json, lease_generation, created_at, started_at, finished_at
            ) VALUES ('job-1', 'gitlab.example.com/group/app#12', 'alice', 'event-1',
                      'succeeded', ?, 1, 1, 1, 2)
            """,
            (json.dumps(envelope),),
        )
        connection.execute(
            """
            INSERT INTO runs(
                run_id, job_id, workspace_id, lease_generation, state,
                refs_json, result_json, started_at, finished_at
            ) VALUES ('run-1', 'job-1', 'alice', 1, 'succeeded', ?, ?, 1, 2)
            """,
            (
                json.dumps(
                    {
                        "run_id": "run-1",
                        "tmux_session": "bw-run-1",
                        "worktree_relpath": "worktrees/issue-12",
                        "run_dir_relpath": "state/issue-12/runs/run-1",
                        "session_file_relpath": None,
                    }
                ),
                json.dumps(
                    {
                        "reply": "done\nSTATUS: done",
                        "session_file_relpath": ".pi/agent/sessions/run-1.jsonl",
                    }
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO merge_requests(
                project_key, mr_iid, conversation_key, job_id, run_id,
                state, merged_at, updated_at
            ) VALUES ('gitlab.example.com/group/app', 5,
                      'gitlab.example.com/group/app#12', 'job-1', 'run-1',
                      'merged', ?, ?)
            """,
            (merged_at, merged_at),
        )
    return store


def test_merge_refresh_uses_token_for_mr_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = seeded_store(tmp_path / "controller.db", 1_000.0)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE merge_requests SET state = 'opened', merged_at = NULL"
        )
    observed: list[str] = []

    class FakeGitLab:
        def __init__(self, host: str, token: str) -> None:
            observed.append(token)

        def get(self, path: str) -> dict:
            return {"state": "merged", "merged_at": "2026-01-01T00:00:00Z"}

    monkeypatch.setattr(watcher, "GitLab", FakeGitLab)

    assert (
        RetentionManager(
            store,
            {"gitlab.example.com/group/app": "app-token"},
        ).refresh_merge_requests()
        == 1
    )
    assert observed == ["app-token"]


def test_merge_plus_seven_days_claim_and_compaction(tmp_path: Path) -> None:
    merged_at = 1_000.0
    store = seeded_store(tmp_path / "controller.db", merged_at)
    retention = RetentionManager(store, "bot-token")

    assert retention.schedule_due(merged_at + MERGE_RETENTION_SECONDS - 1) == 0
    assert (
        store.claim_workspace_actions(
            "alice", now=merged_at + MERGE_RETENTION_SECONDS - 1
        )
        == ()
    )
    assert retention.schedule_due(merged_at + MERGE_RETENTION_SECONDS) == 1
    action = store.claim_workspace_actions(
        "alice",
        now=merged_at + MERGE_RETENTION_SECONDS,
    )[0]
    with closing(store.connect()) as connection:
        assert (
            connection.execute(
                "SELECT status FROM conversations WHERE conversation_key = 'gitlab.example.com/group/app#12'"
            ).fetchone()[0]
            == "cleaning"
        )
    with store.transaction() as connection:
        connection.execute(
            "UPDATE workspaces SET allowed_specs_json = ?, ready = 1 WHERE workspace_id = 'alice'",
            (json.dumps(["pi:gpt-5.6-sol:high"]),),
        )
    blocked = HostedDispatcher(store, frozenset(), frozenset()).dispatch(
        DispatchCandidate(
            host="gitlab.example.com",
            project_path="group/app",
            project_id=7,
            issue_iid=12,
            issue_url="https://gitlab.example.com/group/app/-/issues/12",
            issue_title="Follow-up",
            issue_description="new work",
            event_key="event-2",
            event_kind="label",
            actor_username="alice",
            trigger_kind="agent::ready",
            assignees=(Assignee("alice", 42),),
            hint_texts=(),
            trigger_messages=(),
            thread_context="",
            worker_briefing=None,
            jira_context="",
        )
    )
    assert not blocked.accepted
    assert "cleaning" in str(blocked.reason)

    finished_at = merged_at + MERGE_RETENTION_SECONDS + 10
    with pytest.raises(StaleLeaseError):
        store.finish_workspace_action(
            "alice",
            str(action["action_id"]),
            int(action["lease_generation"]) + 1,
            success=True,
            error=None,
            now=finished_at,
        )
    assert store.finish_workspace_action(
        "alice",
        str(action["action_id"]),
        int(action["lease_generation"]),
        success=True,
        error=None,
        now=finished_at,
    )
    assert not store.finish_workspace_action(
        "alice",
        str(action["action_id"]),
        int(action["lease_generation"]),
        success=True,
        error=None,
        now=finished_at + 1,
    )
    with closing(store.connect()) as connection:
        conversation = connection.execute(
            "SELECT status, session_refs_json FROM conversations"
        ).fetchone()
        run = connection.execute(
            "SELECT refs_json, result_json, error_json FROM runs"
        ).fetchone()
        compact_envelope = json.loads(
            connection.execute("SELECT envelope_json FROM jobs").fetchone()[0]
        )
        retention_row = connection.execute(
            "SELECT compacted_at, audit_json FROM retention_records WHERE run_id = 'run-1'"
        ).fetchone()
        assert tuple(conversation) == ("archived", None)
        assert tuple(run) == ("{}", None, None)
        assert compact_envelope["messages"] == []
        assert compact_envelope["context"] == {}
        assert retention_row["compacted_at"] == finished_at
        assert json.loads(retention_row["audit_json"])["owner_username"] == "alice"
    archived = HostedFleet(tmp_path / "controller.db").rows("alice")[0]
    assert archived.status == "archived"
    assert archived.capabilities == ("audit",)
    assert archived.host_path == ""


def test_failed_cleanup_sets_manual_recovery_state(tmp_path: Path) -> None:
    merged_at = 1_000.0
    store = seeded_store(tmp_path / "controller.db", merged_at)
    RetentionManager(store, "bot-token").schedule_due(
        merged_at + MERGE_RETENTION_SECONDS
    )
    action = store.claim_workspace_actions(
        "alice",
        now=merged_at + MERGE_RETENTION_SECONDS,
    )[0]

    assert store.finish_workspace_action(
        "alice",
        str(action["action_id"]),
        int(action["lease_generation"]),
        success=False,
        error="git worktree remove failed",
        now=merged_at + MERGE_RETENTION_SECONDS + 10,
    )
    with closing(store.connect()) as connection:
        assert (
            connection.execute("SELECT status FROM conversations").fetchone()[0]
            == "cleanup-failed"
        )
        assert (
            connection.execute("SELECT state FROM workspace_actions").fetchone()[0]
            == "failed"
        )


@pytest.mark.parametrize("case", ["failed", "parked", "research", "qa", "no-mr"])
def test_excluded_runs_never_schedule_cleanup(tmp_path: Path, case: str) -> None:
    merged_at = 1_000.0
    store = seeded_store(tmp_path / f"{case}.db", merged_at)
    with store.transaction() as connection:
        if case == "failed":
            connection.execute("UPDATE jobs SET state = 'failed'")
            connection.execute("UPDATE runs SET state = 'failed'")
            connection.execute("UPDATE conversations SET status = 'failed'")
        elif case == "parked":
            connection.execute("UPDATE conversations SET status = 'parked'")
        elif case in ("research", "qa"):
            row = connection.execute("SELECT envelope_json FROM jobs").fetchone()
            envelope = json.loads(row["envelope_json"])
            envelope["trigger_kind"] = (
                "agent::ready-research" if case == "research" else "qa"
            )
            connection.execute(
                "UPDATE jobs SET envelope_json = ?",
                (json.dumps(envelope, sort_keys=True),),
            )
        elif case == "no-mr":
            connection.execute("DELETE FROM merge_requests")

    assert (
        RetentionManager(store, "bot-token").schedule_due(
            merged_at + MERGE_RETENTION_SECONDS + 1
        )
        == 0
    )
    with closing(store.connect()) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM workspace_actions").fetchone()[0]
            == 0
        )


def test_old_conversation_mr_does_not_authorize_current_run_cleanup(
    tmp_path: Path,
) -> None:
    merged_at = 1_000.0
    store = seeded_store(tmp_path / "new-run.db", merged_at)
    with store.transaction() as connection:
        envelope = json.loads(
            connection.execute("SELECT envelope_json FROM jobs").fetchone()[0]
        )
        envelope.update(job_id="job-2", trigger_event_key="event-2")
        connection.execute(
            """
            INSERT INTO jobs(
                job_id, conversation_key, workspace_id, trigger_event_key,
                state, envelope_json, lease_generation, created_at, started_at, finished_at
            ) VALUES ('job-2', 'gitlab.example.com/group/app#12', 'alice', 'event-2',
                      'succeeded', ?, 1, 3, 3, 4)
            """,
            (json.dumps(envelope, sort_keys=True),),
        )
        connection.execute(
            """
            INSERT INTO runs(
                run_id, job_id, workspace_id, lease_generation, state,
                refs_json, result_json, started_at, finished_at
            ) VALUES ('run-2', 'job-2', 'alice', 1, 'succeeded', '{}', '{}', 3, 4)
            """
        )
        connection.execute(
            "UPDATE conversations SET current_job_id = 'job-2', status = 'succeeded'"
        )

    assert (
        RetentionManager(store, "bot-token").schedule_due(
            merged_at + MERGE_RETENTION_SECONDS + 1
        )
        == 0
    )
    with closing(store.connect()) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM workspace_actions").fetchone()[0]
            == 0
        )


def test_cleanup_executor_is_contained_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "home"
    checkout = root / "repos" / "gitlab.example.com" / "group" / "app"
    worktree = root / "worktrees" / "issue-12"
    run_dir = root / "state" / "issue-12" / "runs" / "run-1"
    session = root / ".pi" / "agent" / "sessions" / "run-1.jsonl"
    (checkout / ".git").mkdir(parents=True)
    worktree.mkdir(parents=True)
    run_dir.mkdir(parents=True)
    session.parent.mkdir(parents=True)
    session.write_text("session")

    def git_remove(argv, **kwargs):
        shutil.rmtree(worktree)
        return CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("eastwatch.runner.cleanup.subprocess.run", git_remove)
    action = {
        "kind": "cleanup-run",
        "payload": {
            "host": "gitlab.example.com",
            "project_path": "group/app",
            "worktree_relpath": "worktrees/issue-12",
            "run_dir_relpath": "state/issue-12/runs/run-1",
            "session_file_relpath": ".pi/agent/sessions/run-1.jsonl",
        },
    }
    cleanup = CleanupExecutor(root)
    active_resume = cleanup.acquire_session_lock(action["payload"])
    assert active_resume is not None
    with pytest.raises(CleanupBusy):
        cleanup.remove(action)
    assert worktree.exists() and run_dir.exists() and session.exists()
    active_resume.close()

    cleanup.remove(action)
    cleanup.remove(action)

    assert not worktree.exists()
    assert not run_dir.exists()
    assert not session.exists()
    escaped = json.loads(json.dumps(action))
    escaped["payload"]["run_dir_relpath"] = "../outside"
    with pytest.raises(RuntimeError, match="escapes workspace"):
        cleanup.remove(escaped)
