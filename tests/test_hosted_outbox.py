from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from eastwatch import watcher
from eastwatch.controller.outbox import HostedOutbox
from eastwatch.controller.store import ControllerStore, token_digest


class FakeGitLab:
    notes = []
    mr_author = "alice"
    tokens = []

    def __init__(self, host: str, token: str) -> None:
        self.host = host
        self.tokens.append(token)

    def get(self, path: str, **params):
        if path.endswith("/notes"):
            return list(self.notes)
        if "/merge_requests/" in path:
            return {
                "iid": 5,
                "author": {"username": self.mr_author},
                "description": (
                    "<!-- eastwatch: source_project=group/app "
                    "source_issue_iid=12 conversation_key=12 -->"
                ),
            }
        raise AssertionError(path)

    def post(self, path: str, **payload):
        note = {"id": len(self.notes) + 1, "body": payload["body"]}
        self.notes.append(note)
        return note

    def put(self, path: str, **payload):
        return {}


def terminal_store(
    path: Path,
    *,
    stats: dict | None = None,
    reply_target: dict | None = None,
    trigger_kind: str = "agent::ready",
) -> ControllerStore:
    store = ControllerStore(path)
    store.migrate()
    result = {
        "reply": (
            "Implemented. https://gitlab.example.com/group/app/-/merge_requests/5\n"
            "STATUS: done"
        ),
        "session_file_relpath": ".pi/agent/sessions/run-1.jsonl",
    }
    if stats is not None:
        result["stats"] = stats
    envelope = {
        "schema_version": 1,
        "job_id": "job-1",
        "conversation_key": "gitlab.example.com/group/app#12",
        "host": "gitlab.example.com",
        "project_path": "group/app",
        "project_id": 7,
        "issue_iid": 12,
        "issue_url": "https://gitlab.example.com/group/app/-/issues/12",
        "trigger_kind": trigger_kind,
        "trigger_event_key": "event-1",
        "owner_username": "alice",
        "provider": "pi",
        "model": "gpt-5.6-sol",
        "effort": "high",
        "messages": [],
        "reply_target": reply_target or {"kind": "issue", "iid": 12},
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
                status, provider, model, effort, current_job_id, updated_at
            ) VALUES ('gitlab.example.com/group/app#12', 'gitlab.example.com/group/app', 12,
                      'alice', 'succeeded', 'pi', 'gpt-5.6-sol', 'high', 'job-1', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO jobs(
                job_id, conversation_key, workspace_id, trigger_event_key,
                state, envelope_json, lease_generation, created_at, finished_at
            ) VALUES ('job-1', 'gitlab.example.com/group/app#12', 'alice', 'event-1',
                      'succeeded', ?, 1, 1, 2)
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
                json.dumps(result),
            ),
        )
    return store


def test_note_marker_prevents_duplicate_and_records_mr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeGitLab.notes = []
    FakeGitLab.mr_author = "alice"
    labels = []
    monkeypatch.setattr(watcher, "GitLab", FakeGitLab)
    monkeypatch.setattr(
        watcher,
        "set_issue_agent_label",
        lambda gl, project, iid, label: labels.append(label),
    )
    store = terminal_store(tmp_path / "controller.db")
    outbox = HostedOutbox(store, "bot-token")
    item = {
        "outbox_id": 9,
        "lease_generation": 1,
        "kind": "job-completed",
        "payload": {"job_id": "job-1"},
        "attempt": 1,
    }

    outbox.deliver(item)
    outbox.deliver(item)

    assert len(FakeGitLab.notes) == 1
    assert "Run owner: @alice" in FakeGitLab.notes[0]["body"]
    assert labels == [watcher.MR_READY_LABEL, watcher.MR_READY_LABEL]
    with closing(store.connect()) as connection:
        assert tuple(
            connection.execute("SELECT job_id, run_id FROM merge_requests").fetchone()
        ) == ("job-1", "run-1")


def test_completion_uses_token_for_job_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeGitLab.notes = []
    FakeGitLab.mr_author = "alice"
    FakeGitLab.tokens = []
    monkeypatch.setattr(watcher, "GitLab", FakeGitLab)
    monkeypatch.setattr(watcher, "set_issue_agent_label", lambda *args: None)
    outbox = HostedOutbox(
        terminal_store(tmp_path / "controller.db"),
        {"gitlab.example.com/group/app": "app-token"},
    )

    outbox.deliver(
        {
            "outbox_id": 9,
            "lease_generation": 1,
            "kind": "job-completed",
            "payload": {"job_id": "job-1"},
            "attempt": 1,
        }
    )

    assert FakeGitLab.tokens == ["app-token"]


def test_wrong_mr_author_fails_completion_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeGitLab.notes = []
    FakeGitLab.mr_author = "mallory"
    labels = []
    monkeypatch.setattr(watcher, "GitLab", FakeGitLab)
    monkeypatch.setattr(
        watcher,
        "set_issue_agent_label",
        lambda gl, project, iid, label: labels.append(label),
    )
    outbox = HostedOutbox(terminal_store(tmp_path / "controller.db"), "bot-token")

    outbox.deliver(
        {
            "outbox_id": 10,
            "lease_generation": 1,
            "kind": "job-completed",
            "payload": {"job_id": "job-1"},
            "attempt": 1,
        }
    )

    assert labels == [watcher.FAILED_LABEL]
    assert "authored by @mallory" in FakeGitLab.notes[0]["body"]


def test_qa_reply_mentioning_foreign_mr_is_not_a_deliverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A qa follow-up that merely talks about another MR must not trip
    # deliverable validation (wrong author / wrong marker) or earn mr-ready.
    FakeGitLab.notes = []
    FakeGitLab.mr_author = "mallory"
    labels = []
    monkeypatch.setattr(watcher, "GitLab", FakeGitLab)
    monkeypatch.setattr(
        watcher,
        "set_issue_agent_label",
        lambda gl, project, iid, label: labels.append(label),
    )
    store = terminal_store(tmp_path / "controller.db", trigger_kind="qa")
    outbox = HostedOutbox(store, "bot-token")

    outbox.deliver(
        {
            "outbox_id": 10,
            "lease_generation": 1,
            "kind": "job-completed",
            "payload": {"job_id": "job-1"},
            "attempt": 1,
        }
    )

    assert labels == [watcher.FOR_HUMAN_LABEL]
    assert "failed completion validation" not in FakeGitLab.notes[0]["body"]
    with closing(store.connect()) as connection:
        recorded = connection.execute("SELECT COUNT(*) FROM merge_requests").fetchone()[
            0
        ]
    assert recorded == 0


def test_completion_body_appends_stats_footer_when_present(tmp_path: Path) -> None:
    stats = {
        "schema": 1,
        "duration_s": 12,
        "turns": 1,
        "input": 10,
        "output": 5,
        "cache_read": 0,
        "cache_write": 0,
        "reasoning": 0,
        "total_tokens": 15,
        "cost_usd": 0.001,
        "compactions": 0,
        "subagents": 0,
        "context_tokens": 10,
        "context_window": None,
    }
    store = terminal_store(tmp_path / "controller.db", stats=stats)
    outbox = HostedOutbox(store, "bot-token")
    row = outbox.job_row("job-1")

    body, _label, _mrs = outbox.completion_body(row)

    assert body.endswith("</details>")
    assert "<details>" in body
    assert "run stats" in body


def test_completion_body_omits_stats_footer_when_absent(tmp_path: Path) -> None:
    store = terminal_store(tmp_path / "controller.db")
    outbox = HostedOutbox(store, "bot-token")
    row = outbox.job_row("job-1")

    body, _label, _mrs = outbox.completion_body(row)

    assert "<details>" not in body
    assert "run stats" not in body


class DiscussionGitLab:
    """Fakes the notes/discussions surface ensure_note talks to directly."""

    def __init__(
        self, notes: list[dict] | None = None, *, discussion_fails: bool = False
    ) -> None:
        self.notes = list(notes or [])
        self.discussion_fails = discussion_fails
        self.discussion_posts: list[str] = []
        self.notes_posts: list[str] = []

    def get(self, path: str, **params):
        if path.endswith("/notes"):
            return list(self.notes)
        raise AssertionError(path)

    def post(self, path: str, **payload):
        note = {"id": len(self.notes) + 1, "body": payload["body"]}
        if "/discussions/" in path:
            if self.discussion_fails:
                raise requests.HTTPError(response=SimpleNamespace(status_code=404))
            self.discussion_posts.append(path)
        else:
            self.notes_posts.append(path)
        self.notes.append(note)
        return note


def test_ensure_note_posts_into_discussion_when_target_has_discussion_id(
    tmp_path: Path,
) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    store.migrate()
    outbox = HostedOutbox(store, "bot-token")
    gl = DiscussionGitLab()

    note_id = outbox.ensure_note(
        gl,
        {"id": 7},
        {"kind": "mr", "iid": 5, "discussion_id": "disc-1"},
        outbox_id=9,
        body="Answer text",
    )

    assert note_id == 1
    assert gl.discussion_posts == [
        "projects/7/merge_requests/5/discussions/disc-1/notes"
    ]
    assert gl.notes_posts == []


def test_ensure_note_falls_back_to_top_level_on_404_discussion(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    store.migrate()
    outbox = HostedOutbox(store, "bot-token")
    gl = DiscussionGitLab(discussion_fails=True)

    note_id = outbox.ensure_note(
        gl,
        {"id": 7},
        {"kind": "mr", "iid": 5, "discussion_id": "disc-dead"},
        outbox_id=9,
        body="Answer text",
    )

    assert note_id == 1
    assert gl.discussion_posts == []
    assert gl.notes_posts == ["projects/7/merge_requests/5/notes"]


@pytest.mark.parametrize(
    ("kind", "resource"),
    [("issue", "issues"), ("mr", "merge_requests")],
)
def test_ensure_note_marker_idempotency_both_kinds(
    tmp_path: Path, kind: str, resource: str
) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    store.migrate()
    outbox = HostedOutbox(store, "bot-token")
    marker = HostedOutbox.marker(9)
    gl = DiscussionGitLab(notes=[{"id": 3, "body": f"prior answer\n\n{marker}"}])

    note_id = outbox.ensure_note(
        gl,
        {"id": 7},
        {"kind": kind, "iid": 12},
        outbox_id=9,
        body="new body",
    )

    assert note_id == 3
    assert gl.discussion_posts == [] and gl.notes_posts == []


def test_deliver_job_with_mr_reply_target_posts_on_mr_but_labels_issue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeGitLab.notes = []
    FakeGitLab.mr_author = "alice"
    label_calls = []
    monkeypatch.setattr(watcher, "GitLab", FakeGitLab)
    monkeypatch.setattr(
        watcher,
        "set_issue_agent_label",
        lambda gl, project, iid, label: label_calls.append((iid, label)),
    )
    store = terminal_store(
        tmp_path / "controller.db",
        reply_target={"kind": "mr", "iid": 5},
    )
    outbox = HostedOutbox(store, "bot-token")

    outbox.deliver(
        {
            "outbox_id": 11,
            "lease_generation": 1,
            "kind": "job-completed",
            "payload": {"job_id": "job-1"},
            "attempt": 1,
        }
    )

    assert len(FakeGitLab.notes) == 1
    assert label_calls == [("12", watcher.MR_READY_LABEL)]


def test_job_started_sets_working_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeGitLab.notes = []
    labels = []
    monkeypatch.setattr(watcher, "GitLab", FakeGitLab)
    monkeypatch.setattr(
        watcher,
        "set_issue_agent_label",
        lambda gl, project, iid, label: labels.append(label),
    )
    store = terminal_store(tmp_path / "controller.db")
    with store.transaction() as connection:
        connection.execute("UPDATE jobs SET state = 'running'")
    outbox = HostedOutbox(store, "bot-token")

    outbox.deliver(
        {
            "outbox_id": 11,
            "lease_generation": 1,
            "kind": "job-started",
            "payload": {"job_id": "job-1"},
            "attempt": 1,
        }
    )

    assert labels == [watcher.WORKING_LABEL]


def test_job_started_research_trigger_sets_researching_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeGitLab.notes = []
    labels = []
    monkeypatch.setattr(watcher, "GitLab", FakeGitLab)
    monkeypatch.setattr(
        watcher,
        "set_issue_agent_label",
        lambda gl, project, iid, label: labels.append(label),
    )
    store = terminal_store(
        tmp_path / "controller.db", trigger_kind="agent::ready-research"
    )
    with store.transaction() as connection:
        connection.execute("UPDATE jobs SET state = 'running'")
    outbox = HostedOutbox(store, "bot-token")

    outbox.deliver(
        {
            "outbox_id": 11,
            "lease_generation": 1,
            "kind": "job-started",
            "payload": {"job_id": "job-1"},
            "attempt": 1,
        }
    )

    assert labels == [watcher.RESEARCHING_LABEL]


def test_job_started_on_terminal_job_writes_no_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Fast runs can finish before the started row delivers: the terminal
    # delivery's label supersedes, so this must be a silent no-op.
    FakeGitLab.notes = []
    labels = []
    monkeypatch.setattr(watcher, "GitLab", FakeGitLab)
    monkeypatch.setattr(
        watcher,
        "set_issue_agent_label",
        lambda gl, project, iid, label: labels.append(label),
    )
    store = terminal_store(tmp_path / "controller.db")
    outbox = HostedOutbox(store, "bot-token")

    outbox.deliver(
        {
            "outbox_id": 11,
            "lease_generation": 1,
            "kind": "job-started",
            "payload": {"job_id": "job-1"},
            "attempt": 1,
        }
    )

    assert labels == []
