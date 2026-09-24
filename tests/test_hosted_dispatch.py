from __future__ import annotations

import json
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from eastwatch import watcher
from eastwatch.controller.dispatch import Assignee, DispatchCandidate, HostedDispatcher
from eastwatch.controller.store import ControllerStore, token_digest


def store_with_workspaces(path: Path) -> ControllerStore:
    store = ControllerStore(path)
    store.migrate()
    with store.transaction() as connection:
        for workspace_id, username, user_id in (
            ("alice", "alice", 42),
            ("bob", "bob", 43),
        ):
            connection.execute(
                """
                INSERT INTO workspaces(
                    workspace_id, owner_username, owner_user_id, token_sha256,
                    ready, capacity, default_spec, allowed_specs_json,
                    container_name, host_root, created_at
                ) VALUES (?, ?, ?, ?, 1, 10, 'pi:gpt-5.6-sol:high', ?, ?, ?, 1)
                """,
                (
                    workspace_id,
                    username,
                    user_id,
                    token_digest(f"{username}-token"),
                    json.dumps(["pi:gpt-5.6-sol:high", "pi:gpt-5.6-luna:low"]),
                    f"bw-workspace-{username}",
                    f"/srv/eastwatch/users/{username}/home",
                ),
            )
    return store


def candidate(event: str = "1") -> DispatchCandidate:
    return DispatchCandidate(
        host="gitlab.example.com",
        project_path="group/app",
        project_id=7,
        issue_iid=12,
        issue_url="https://gitlab.example.com/group/app/-/issues/12",
        issue_title="Ship hosted runner",
        issue_description="Implement it",
        event_key=event,
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


def counts(store: ControllerStore) -> tuple[int, int, int]:
    with closing(store.connect()) as connection:
        return tuple(
            int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("event_receipts", "jobs", "outbox")
        )


def test_assignee_dispatch_and_duplicate_are_one_job(tmp_path: Path) -> None:
    store = store_with_workspaces(tmp_path / "controller.db")
    dispatcher = HostedDispatcher(
        store, frozenset({"admin"}), frozenset({"trusted-bot"})
    )

    first = dispatcher.dispatch(candidate())
    second = dispatcher.dispatch(candidate())

    assert first.accepted and second.accepted
    assert first.job_id == second.job_id
    assert counts(store) == (1, 1, 0)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"assignees": ()}, "no resolvable user id"),
        ({"assignees": (Assignee("alice", 42), Assignee("bob", 43))}, "at most one"),
        ({"assignees": (Assignee("alice", 999),)}, "identity changed"),
        ({"actor_username": "mallory"}, "cannot dispatch"),
        ({"hint_texts": ("[pi:not-approved:high]",)}, "not approved"),
    ],
)
def test_rejected_event_is_receipted_once(
    tmp_path: Path,
    change: dict[str, object],
    reason: str,
) -> None:
    store = store_with_workspaces(tmp_path / "controller.db")
    dispatcher = HostedDispatcher(
        store, frozenset({"admin"}), frozenset({"trusted-bot"})
    )
    item = replace(candidate(), **change)

    result = dispatcher.dispatch(item)
    duplicate = dispatcher.dispatch(item)

    assert not result.accepted and reason in str(result.reason)
    assert not duplicate.accepted
    assert counts(store) == (1, 0, 1)


def test_unassigned_candidate_dispatches_to_actor(tmp_path: Path) -> None:
    store = store_with_workspaces(tmp_path / "controller.db")
    dispatcher = HostedDispatcher(store, frozenset(), frozenset())
    item = replace(candidate(), assignees=(), actor_username="bob", actor_user_id=43)

    result = dispatcher.dispatch(item)

    assert result.accepted
    assert counts(store) == (1, 1, 0)
    with closing(store.connect()) as connection:
        envelope = json.loads(
            connection.execute(
                "SELECT envelope_json FROM jobs WHERE job_id = ?", (result.job_id,)
            ).fetchone()[0]
        )
        conversations = connection.execute(
            "SELECT COUNT(*) FROM conversations"
        ).fetchone()[0]
    assert envelope["owner_username"] == "bob"
    assert conversations == 1


def test_unassigned_candidate_actor_without_workspace_is_rejected(
    tmp_path: Path,
) -> None:
    store = store_with_workspaces(tmp_path / "controller.db")
    dispatcher = HostedDispatcher(store, frozenset(), frozenset())
    item = replace(candidate(), assignees=(), actor_username="carol", actor_user_id=999)

    result = dispatcher.dispatch(item)

    assert not result.accepted
    assert "no dispatch-ready workspace" in str(result.reason)


@pytest.mark.parametrize("actor", ["admin", "trusted-bot"])
def test_admin_and_bot_may_dispatch_for_assignee(tmp_path: Path, actor: str) -> None:
    store = store_with_workspaces(tmp_path / "controller.db")
    dispatcher = HostedDispatcher(
        store, frozenset({"admin"}), frozenset({"trusted-bot"})
    )

    result = dispatcher.dispatch(replace(candidate(), actor_username=actor))

    assert result.accepted
    assert counts(store) == (1, 1, 0)


def test_approved_hint_and_active_reassignment_guard(tmp_path: Path) -> None:
    store = store_with_workspaces(tmp_path / "controller.db")
    dispatcher = HostedDispatcher(store, frozenset(), frozenset())
    hinted = replace(candidate(), hint_texts=("[pi:gpt-5.6-luna:low]",))
    first = dispatcher.dispatch(hinted)
    assert first.accepted
    with closing(store.connect()) as connection:
        envelope = json.loads(
            connection.execute(
                "SELECT envelope_json FROM jobs WHERE job_id = ?", (first.job_id,)
            ).fetchone()[0]
        )
        assert (envelope["provider"], envelope["model"], envelope["effort"]) == (
            "pi",
            "gpt-5.6-luna",
            "low",
        )

    duplicate_run = dispatcher.dispatch(candidate(event="2"))
    assert not duplicate_run.accepted
    assert "already has an active" in str(duplicate_run.reason)

    reassigned = replace(
        candidate(event="3"),
        actor_username="bob",
        assignees=(Assignee("bob", 43),),
    )
    rejected = dispatcher.dispatch(reassigned)
    assert not rejected.accepted
    assert "reassigned" in str(rejected.reason)
    assert counts(store) == (3, 1, 2)


def test_vanished_issue_comment_event_is_receipted_and_skipped(tmp_path: Path) -> None:
    store = store_with_workspaces(tmp_path / "controller.db")
    dispatcher = HostedDispatcher(store, frozenset(), frozenset())
    item = candidate(event="9")

    dispatcher.record_skipped_event(
        project_key=item.project_key,
        host=item.host,
        project_path=item.project_path,
        project_id=item.project_id,
        event_key=item.durable_event_key,
        event_kind="comment",
        actor_username=item.actor_username,
        reason="issue !12 not found during hosted polling",
    )

    assert (
        dispatcher.consumed_event(item.project_key, item.durable_event_key) is not None
    )
    assert counts(store) == (1, 0, 0)

    repeat = dispatcher.dispatch(item)
    assert not repeat.accepted
    assert counts(store) == (1, 0, 0)


def test_dispatch_without_reply_target_defaults_to_legacy_issue_shape(
    tmp_path: Path,
) -> None:
    store = store_with_workspaces(tmp_path / "controller.db")
    dispatcher = HostedDispatcher(store, frozenset(), frozenset())

    result = dispatcher.dispatch(candidate())

    assert result.accepted
    with closing(store.connect()) as connection:
        envelope = json.loads(
            connection.execute(
                "SELECT envelope_json FROM jobs WHERE job_id = ?", (result.job_id,)
            ).fetchone()[0]
        )
    assert envelope["reply_target"] == {"kind": "issue", "iid": 12}


def test_hosted_comment_events_routes_mr_and_issue_notes_and_filters_bot() -> None:
    class FakeEventsGL:
        def __init__(self, events: list[dict]) -> None:
            self.events = events

        def get(self, path: str, **params):
            assert path == "projects/7/events"
            return self.events

    proj = {"id": 7, "bot_user_id": 99, "bot_username": "eastwatch-bot"}
    events = [
        {
            "id": 1,
            "author": {"username": "alice", "id": 1},
            "note": {
                "id": 101,
                "system": False,
                "noteable_type": "Issue",
                "noteable_iid": 12,
                "body": "@agent look at this",
            },
        },
        {
            "id": 2,
            "author": {"username": "alice", "id": 1},
            "note": {
                "id": 102,
                "system": False,
                "noteable_type": "MergeRequest",
                "noteable_iid": 5,
                "body": "@agent what changed?",
            },
        },
        {
            "id": 3,
            "author": {"username": "eastwatch-bot", "id": 99},
            "note": {
                "id": 103,
                "system": False,
                "noteable_type": "MergeRequest",
                "noteable_iid": 5,
                "body": "@agent bot noise",
            },
        },
        {
            "id": 4,
            "author": {"username": "alice", "id": 1},
            "note": {
                "id": 104,
                "system": False,
                "noteable_type": "MergeRequest",
                "noteable_iid": 5,
                "body": "no mention here",
            },
        },
    ]
    comments, cursor = watcher.hosted_comment_events(
        FakeEventsGL(events), proj, after_event_id=0
    )

    assert cursor == 4
    assert [c["event_id"] for c in comments] == ["1", "2", "4"]
    issue_comment, mr_comment, plain_mr_comment = comments
    assert issue_comment["noteable_type"] == "Issue"
    assert issue_comment["noteable_iid"] == 12
    assert issue_comment["note_id"] == 101
    assert issue_comment["question"] == "look at this"
    assert issue_comment["actor_user_id"] == 1
    assert mr_comment["noteable_type"] == "MergeRequest"
    assert mr_comment["noteable_iid"] == 5
    assert mr_comment["note_id"] == 102
    assert mr_comment["question"] == "what changed?"
    assert mr_comment["actor_user_id"] == 1
    # non-system, non-bot-authored plain note (no @agent mention) is still emitted,
    # with question=None, so hosted_cycle can route it as a resume gesture.
    assert plain_mr_comment["noteable_type"] == "MergeRequest"
    assert plain_mr_comment["note_id"] == 104
    assert plain_mr_comment["body"] == "no mention here"
    assert plain_mr_comment["question"] is None


def test_hosted_comment_events_actor_user_id_missing_is_none() -> None:
    class FakeEventsGL:
        def __init__(self, events: list[dict]) -> None:
            self.events = events

        def get(self, path: str, **params):
            return self.events

    proj = {"id": 7, "bot_user_id": 99, "bot_username": "eastwatch-bot"}
    events = [
        {
            "id": 1,
            "author": {"username": "alice"},
            "note": {
                "id": 101,
                "system": False,
                "noteable_type": "Issue",
                "noteable_iid": 12,
                "body": "@agent look at this",
            },
        },
    ]
    comments, _cursor = watcher.hosted_comment_events(
        FakeEventsGL(events), proj, after_event_id=0
    )

    assert comments[0]["actor_user_id"] is None


def test_hosted_label_fires_captures_actor_user_id() -> None:
    class FakeLabelGL:
        def get(self, path: str, **params):
            if path == "projects/7/issues":
                return [
                    {
                        "iid": 12,
                        "web_url": "https://gitlab.example.com/group/app/-/issues/12",
                        "title": "Ship hosted runner",
                        "description": "Implement it",
                        "assignees": [],
                    }
                ]
            if path == "projects/7/issues/12/resource_label_events":
                return [
                    {
                        "id": 1,
                        "action": "add",
                        "label": {"name": "agent::ready"},
                        "user": {"username": "alice", "id": 1},
                    },
                ]
            raise AssertionError(f"unexpected GET {path}")

    proj = {"id": 7}

    fires = watcher.hosted_label_fires(FakeLabelGL(), proj, "agent::ready")

    assert len(fires) == 1
    assert fires[0]["actor_username"] == "alice"
    assert fires[0]["actor_user_id"] == 1


def test_hosted_label_fires_actor_user_id_missing_is_none() -> None:
    class FakeLabelGL:
        def get(self, path: str, **params):
            if path == "projects/7/issues":
                return [
                    {
                        "iid": 12,
                        "web_url": "https://gitlab.example.com/group/app/-/issues/12",
                        "title": "Ship hosted runner",
                        "description": "Implement it",
                        "assignees": [],
                    }
                ]
            if path == "projects/7/issues/12/resource_label_events":
                return [
                    {
                        "id": 1,
                        "action": "add",
                        "label": {"name": "agent::ready"},
                        "user": {"username": "alice"},
                    },
                ]
            raise AssertionError(f"unexpected GET {path}")

    proj = {"id": 7}

    fires = watcher.hosted_label_fires(FakeLabelGL(), proj, "agent::ready")

    assert fires[0]["actor_user_id"] is None


def seed_bootstrapped_project(
    store: ControllerStore, project_key: str, host: str, path: str, project_id: int
) -> None:
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO projects(project_key, host, project_path, project_id, last_event_id, bootstrapped)
            VALUES (?, ?, ?, ?, 0, 1)
            """,
            (project_key, host, path, project_id),
        )


MR_MARKER = "<!-- eastwatch: source_project=group/app source_issue_iid=12 conversation_key=12 -->"


class FakeHostedGL:
    """Routes GET/POST calls used by hosted_cycle for a single MR-note cycle."""

    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get(self, path: str, **params):
        self.calls.append(path)
        if path in self.responses:
            return self.responses[path]
        if (
            path.endswith("/notes")
            or path.endswith("/links")
            or path.endswith("/discussions")
        ):
            return []
        raise AssertionError(f"unexpected GET {path}")


def hosted_mr_note_gl(
    *, note_id: int = 555, discussion_id: str | None = "disc-abc"
) -> FakeHostedGL:
    discussions = (
        [{"id": discussion_id, "individual_note": False, "notes": [{"id": note_id}]}]
        if discussion_id
        else []
    )
    return FakeHostedGL(
        {
            "projects/7/events": [
                {
                    "id": 1,
                    "author": {"username": "alice", "id": 1},
                    "note": {
                        "id": note_id,
                        "system": False,
                        "noteable_type": "MergeRequest",
                        "noteable_iid": 5,
                        "body": "@agent what's the status?",
                    },
                }
            ],
            "projects/7/merge_requests/5": {
                "iid": 5,
                "web_url": "https://gitlab.example.com/group/app/-/merge_requests/5",
                "description": MR_MARKER,
            },
            "projects/7/issues/12": {
                "iid": 12,
                "web_url": "https://gitlab.example.com/group/app/-/issues/12",
                "title": "Ship hosted runner",
                "description": "Implement it",
                "assignees": [{"username": "alice", "id": 42}],
            },
            "projects/7/merge_requests/5/discussions": discussions,
        }
    )


def test_hosted_cycle_routes_mr_note_to_mapped_issue_conversation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = store_with_workspaces(tmp_path / "controller.db")
    seed_bootstrapped_project(
        store, "gitlab.example.com/group/app", "gitlab.example.com", "group/app", 7
    )
    dispatcher = HostedDispatcher(store, frozenset(), frozenset())
    gl = hosted_mr_note_gl()
    monkeypatch.setattr(watcher, "GitLab", lambda host, token: gl)
    proj = {
        "host": "gitlab.example.com",
        "path": "group/app",
        "id": 7,
        "triggers": ["mention"],
    }
    cfg = {"projects": [proj]}

    watcher.hosted_cycle(cfg, store, dispatcher, "bot-token")

    with closing(store.connect()) as connection:
        job = connection.execute(
            "SELECT job_id, conversation_key, envelope_json FROM jobs"
        ).fetchone()
    assert job is not None
    assert job["conversation_key"] == "gitlab.example.com/group/app#12"
    envelope = json.loads(job["envelope_json"])
    assert envelope["reply_target"] == {
        "kind": "mr",
        "iid": 5,
        "discussion_id": "disc-abc",
    }
    message = envelope["messages"][0]
    assert "merge request !5" in message
    assert "what's the status?" in message


def test_hosted_cycle_mr_note_without_marker_is_skipped_and_receipted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = store_with_workspaces(tmp_path / "controller.db")
    seed_bootstrapped_project(
        store, "gitlab.example.com/group/app", "gitlab.example.com", "group/app", 7
    )
    dispatcher = HostedDispatcher(store, frozenset(), frozenset())
    gl = hosted_mr_note_gl()
    gl.responses["projects/7/merge_requests/5"] = {
        "iid": 5,
        "web_url": "https://gitlab.example.com/group/app/-/merge_requests/5",
        "description": "no marker here",
    }
    monkeypatch.setattr(watcher, "GitLab", lambda host, token: gl)
    proj = {
        "host": "gitlab.example.com",
        "path": "group/app",
        "id": 7,
        "triggers": ["mention"],
    }
    cfg = {"projects": [proj]}

    watcher.hosted_cycle(cfg, store, dispatcher, "bot-token")

    with closing(store.connect()) as connection:
        jobs = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        receipts = connection.execute(
            "SELECT payload_json FROM event_receipts WHERE project_key = 'gitlab.example.com/group/app'"
        ).fetchone()
    assert jobs == 0
    assert receipts is not None
    reason = json.loads(receipts["payload_json"])["reason"]
    assert "no eastwatch source marker" in reason


def test_hosted_cycle_issue_note_reply_target_carries_discussion_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = store_with_workspaces(tmp_path / "controller.db")
    seed_bootstrapped_project(
        store, "gitlab.example.com/group/app", "gitlab.example.com", "group/app", 7
    )
    dispatcher = HostedDispatcher(store, frozenset(), frozenset())
    gl = FakeHostedGL(
        {
            "projects/7/events": [
                {
                    "id": 1,
                    "author": {"username": "alice", "id": 1},
                    "note": {
                        "id": 777,
                        "system": False,
                        "noteable_type": "Issue",
                        "noteable_iid": 12,
                        "body": "@agent any update?",
                    },
                }
            ],
            "projects/7/issues/12": {
                "iid": 12,
                "web_url": "https://gitlab.example.com/group/app/-/issues/12",
                "title": "Ship hosted runner",
                "description": "Implement it",
                "assignees": [{"username": "alice", "id": 42}],
            },
            "projects/7/issues/12/discussions": [
                {"id": "disc-issue", "individual_note": False, "notes": [{"id": 777}]}
            ],
        }
    )
    monkeypatch.setattr(watcher, "GitLab", lambda host, token: gl)
    proj = {
        "host": "gitlab.example.com",
        "path": "group/app",
        "id": 7,
        "triggers": ["mention"],
    }
    cfg = {"projects": [proj]}

    watcher.hosted_cycle(cfg, store, dispatcher, "bot-token")

    with closing(store.connect()) as connection:
        envelope = json.loads(
            connection.execute("SELECT envelope_json FROM jobs").fetchone()[
                "envelope_json"
            ]
        )
    assert envelope["reply_target"] == {
        "kind": "issue",
        "iid": 12,
        "discussion_id": "disc-issue",
    }


def test_hosted_cycle_issue_top_level_note_has_no_discussion_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = store_with_workspaces(tmp_path / "controller.db")
    seed_bootstrapped_project(
        store, "gitlab.example.com/group/app", "gitlab.example.com", "group/app", 7
    )
    dispatcher = HostedDispatcher(store, frozenset(), frozenset())
    gl = FakeHostedGL(
        {
            "projects/7/events": [
                {
                    "id": 1,
                    "author": {"username": "alice", "id": 1},
                    "note": {
                        "id": 888,
                        "system": False,
                        "noteable_type": "Issue",
                        "noteable_iid": 12,
                        "body": "@agent any update?",
                    },
                }
            ],
            "projects/7/issues/12": {
                "iid": 12,
                "web_url": "https://gitlab.example.com/group/app/-/issues/12",
                "title": "Ship hosted runner",
                "description": "Implement it",
                "assignees": [{"username": "alice", "id": 42}],
            },
            "projects/7/issues/12/discussions": [],
        }
    )
    monkeypatch.setattr(watcher, "GitLab", lambda host, token: gl)
    proj = {
        "host": "gitlab.example.com",
        "path": "group/app",
        "id": 7,
        "triggers": ["mention"],
    }
    cfg = {"projects": [proj]}

    watcher.hosted_cycle(cfg, store, dispatcher, "bot-token")

    with closing(store.connect()) as connection:
        envelope = json.loads(
            connection.execute("SELECT envelope_json FROM jobs").fetchone()[
                "envelope_json"
            ]
        )
    assert envelope["reply_target"] == {"kind": "issue", "iid": 12}
    assert "discussion_id" not in envelope["reply_target"]


def hosted_plain_issue_gl(
    *,
    note_id: int = 900,
    body: str = "thanks, looks good",
    discussion_id: str | None = "disc-plain",
    individual_note: bool = False,
    bot_note_body: str | None = "bot: done here",
    issue_state: str = "opened",
) -> FakeHostedGL:
    notes = [{"id": note_id, "author": {"id": 1}, "body": body}]
    if bot_note_body is not None:
        notes.insert(
            0, {"id": note_id - 1, "author": {"id": 99}, "body": bot_note_body}
        )
    discussions = (
        [{"id": discussion_id, "individual_note": individual_note, "notes": notes}]
        if discussion_id
        else []
    )
    return FakeHostedGL(
        {
            "projects/7/events": [
                {
                    "id": 1,
                    "author": {"username": "alice", "id": 1},
                    "note": {
                        "id": note_id,
                        "system": False,
                        "noteable_type": "Issue",
                        "noteable_iid": 12,
                        "body": body,
                    },
                }
            ],
            "projects/7/issues/12": {
                "iid": 12,
                "web_url": "https://gitlab.example.com/group/app/-/issues/12",
                "title": "Ship hosted runner",
                "description": "Implement it",
                "assignees": [{"username": "alice", "id": 42}],
                "state": issue_state,
            },
            "projects/7/issues/12/discussions": discussions,
        }
    )


def plain_note_proj() -> dict:
    return {
        "host": "gitlab.example.com",
        "path": "group/app",
        "id": 7,
        "bot_user_id": 99,
        "triggers": ["mention"],
    }


def jobs_and_receipts(store: ControllerStore) -> tuple[int, int]:
    with closing(store.connect()) as connection:
        jobs = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        receipts = connection.execute(
            "SELECT COUNT(*) FROM event_receipts WHERE project_key = 'gitlab.example.com/group/app'"
        ).fetchone()[0]
    return jobs, receipts


def test_hosted_cycle_plain_issue_note_dispatches_in_bot_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = store_with_workspaces(tmp_path / "controller.db")
    seed_bootstrapped_project(
        store, "gitlab.example.com/group/app", "gitlab.example.com", "group/app", 7
    )
    dispatcher = HostedDispatcher(store, frozenset(), frozenset())
    gl = hosted_plain_issue_gl(body="thanks, looks good")
    monkeypatch.setattr(watcher, "GitLab", lambda host, token: gl)
    cfg = {"projects": [plain_note_proj()]}

    watcher.hosted_cycle(cfg, store, dispatcher, "bot-token")

    with closing(store.connect()) as connection:
        job = connection.execute("SELECT envelope_json FROM jobs").fetchone()
    assert job is not None
    envelope = json.loads(job["envelope_json"])
    assert envelope["reply_target"] == {
        "kind": "issue",
        "iid": 12,
        "discussion_id": "disc-plain",
    }
    assert envelope["messages"] == ["thanks, looks good"]


def test_hosted_cycle_plain_top_level_note_is_not_dispatched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = store_with_workspaces(tmp_path / "controller.db")
    seed_bootstrapped_project(
        store, "gitlab.example.com/group/app", "gitlab.example.com", "group/app", 7
    )
    dispatcher = HostedDispatcher(store, frozenset(), frozenset())
    gl = hosted_plain_issue_gl(individual_note=True)
    monkeypatch.setattr(watcher, "GitLab", lambda host, token: gl)
    cfg = {"projects": [plain_note_proj()]}

    watcher.hosted_cycle(cfg, store, dispatcher, "bot-token")

    assert jobs_and_receipts(store) == (0, 0)


def test_hosted_cycle_plain_note_without_bot_note_is_not_dispatched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = store_with_workspaces(tmp_path / "controller.db")
    seed_bootstrapped_project(
        store, "gitlab.example.com/group/app", "gitlab.example.com", "group/app", 7
    )
    dispatcher = HostedDispatcher(store, frozenset(), frozenset())
    gl = hosted_plain_issue_gl(bot_note_body=None)
    monkeypatch.setattr(watcher, "GitLab", lambda host, token: gl)
    cfg = {"projects": [plain_note_proj()]}

    watcher.hosted_cycle(cfg, store, dispatcher, "bot-token")

    assert jobs_and_receipts(store) == (0, 0)


def test_hosted_cycle_plain_note_echoing_bot_body_is_not_dispatched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = store_with_workspaces(tmp_path / "controller.db")
    seed_bootstrapped_project(
        store, "gitlab.example.com/group/app", "gitlab.example.com", "group/app", 7
    )
    dispatcher = HostedDispatcher(store, frozenset(), frozenset())
    gl = hosted_plain_issue_gl(
        body="  bot: done here  ", bot_note_body="bot: done here"
    )
    monkeypatch.setattr(watcher, "GitLab", lambda host, token: gl)
    cfg = {"projects": [plain_note_proj()]}

    watcher.hosted_cycle(cfg, store, dispatcher, "bot-token")

    assert jobs_and_receipts(store) == (0, 0)


def test_hosted_cycle_plain_note_on_closed_issue_is_not_dispatched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = store_with_workspaces(tmp_path / "controller.db")
    seed_bootstrapped_project(
        store, "gitlab.example.com/group/app", "gitlab.example.com", "group/app", 7
    )
    dispatcher = HostedDispatcher(store, frozenset(), frozenset())
    gl = hosted_plain_issue_gl(issue_state="closed")
    monkeypatch.setattr(watcher, "GitLab", lambda host, token: gl)
    cfg = {"projects": [plain_note_proj()]}

    watcher.hosted_cycle(cfg, store, dispatcher, "bot-token")

    assert jobs_and_receipts(store) == (0, 0)


def hosted_plain_mr_gl(
    *,
    note_id: int = 950,
    body: str = "sounds good, thanks",
    discussion_id: str | None = "disc-mr-plain",
    marker: str | None = MR_MARKER,
) -> FakeHostedGL:
    notes = [
        {"id": note_id - 1, "author": {"id": 99}, "body": "bot: done here"},
        {"id": note_id, "author": {"id": 1}, "body": body},
    ]
    discussions = (
        [{"id": discussion_id, "individual_note": False, "notes": notes}]
        if discussion_id
        else []
    )
    return FakeHostedGL(
        {
            "projects/7/events": [
                {
                    "id": 1,
                    "author": {"username": "alice", "id": 1},
                    "note": {
                        "id": note_id,
                        "system": False,
                        "noteable_type": "MergeRequest",
                        "noteable_iid": 5,
                        "body": body,
                    },
                }
            ],
            "projects/7/merge_requests/5": {
                "iid": 5,
                "web_url": "https://gitlab.example.com/group/app/-/merge_requests/5",
                "description": marker or "no marker here",
            },
            "projects/7/issues/12": {
                "iid": 12,
                "web_url": "https://gitlab.example.com/group/app/-/issues/12",
                "title": "Ship hosted runner",
                "description": "Implement it",
                "assignees": [{"username": "alice", "id": 42}],
            },
            "projects/7/merge_requests/5/discussions": discussions,
        }
    )


def test_hosted_cycle_plain_mr_note_dispatches_to_mapped_issue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = store_with_workspaces(tmp_path / "controller.db")
    seed_bootstrapped_project(
        store, "gitlab.example.com/group/app", "gitlab.example.com", "group/app", 7
    )
    dispatcher = HostedDispatcher(store, frozenset(), frozenset())
    gl = hosted_plain_mr_gl(body="sounds good, thanks")
    monkeypatch.setattr(watcher, "GitLab", lambda host, token: gl)
    cfg = {"projects": [plain_note_proj()]}

    watcher.hosted_cycle(cfg, store, dispatcher, "bot-token")

    with closing(store.connect()) as connection:
        job = connection.execute(
            "SELECT conversation_key, envelope_json FROM jobs"
        ).fetchone()
    assert job is not None
    assert job["conversation_key"] == "gitlab.example.com/group/app#12"
    envelope = json.loads(job["envelope_json"])
    assert envelope["reply_target"] == {
        "kind": "mr",
        "iid": 5,
        "discussion_id": "disc-mr-plain",
    }
    message = envelope["messages"][0]
    assert "merge request !5" in message
    assert "sounds good, thanks" in message


def test_hosted_cycle_plain_mr_note_without_marker_is_not_dispatched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = store_with_workspaces(tmp_path / "controller.db")
    seed_bootstrapped_project(
        store, "gitlab.example.com/group/app", "gitlab.example.com", "group/app", 7
    )
    dispatcher = HostedDispatcher(store, frozenset(), frozenset())
    gl = hosted_plain_mr_gl(marker=None)
    monkeypatch.setattr(watcher, "GitLab", lambda host, token: gl)
    cfg = {"projects": [plain_note_proj()]}

    watcher.hosted_cycle(cfg, store, dispatcher, "bot-token")

    assert jobs_and_receipts(store) == (0, 0)


def test_fetch_issue_raise_transient() -> None:
    class GL404:
        def get(self, path: str, **params):
            raise requests.HTTPError(response=SimpleNamespace(status_code=404))

    class GL500:
        def get(self, path: str, **params):
            raise requests.HTTPError(response=SimpleNamespace(status_code=500))

    proj = {"id": 7}
    assert watcher.fetch_issue(GL404(), proj, "12", raise_transient=True) is None
    with pytest.raises(watcher.TransientIssueFetchError):
        watcher.fetch_issue(GL500(), proj, "12", raise_transient=True)


def test_comment_during_active_run_same_workspace_is_queued(tmp_path: Path) -> None:
    store = store_with_workspaces(tmp_path / "controller.db")
    dispatcher = HostedDispatcher(store, frozenset(), frozenset())
    first = dispatcher.dispatch(candidate())
    assert first.accepted

    comment = replace(
        candidate(event="2"),
        event_kind="comment",
        trigger_messages=("please rebase",),
    )
    result = dispatcher.dispatch(comment)

    assert not result.accepted
    assert result.job_id is None
    assert result.reason == "queued for next run"
    # event_receipts: label(1) + comment(1) = 2; jobs: only the label's = 1; outbox: no dispatch-rejected row = 0
    assert counts(store) == (2, 1, 0)

    with closing(store.connect()) as connection:
        row = connection.execute(
            "SELECT pending_json FROM conversations WHERE conversation_key = ?",
            ("gitlab.example.com/group/app#12",),
        ).fetchone()
    pending = json.loads(row["pending_json"])
    assert len(pending) == 1
    assert pending[0]["messages"] == ["please rebase"]
    assert pending[0]["event_key"] == comment.durable_event_key

    duplicate = dispatcher.dispatch(comment)
    assert not duplicate.accepted
    assert duplicate.reason == "event was already consumed"
    with closing(store.connect()) as connection:
        row = connection.execute(
            "SELECT pending_json FROM conversations WHERE conversation_key = ?",
            ("gitlab.example.com/group/app#12",),
        ).fetchone()
    assert len(json.loads(row["pending_json"])) == 1


def test_label_refire_during_active_run_is_rejected(tmp_path: Path) -> None:
    store = store_with_workspaces(tmp_path / "controller.db")
    dispatcher = HostedDispatcher(store, frozenset(), frozenset())
    first = dispatcher.dispatch(candidate())
    assert first.accepted

    second = dispatcher.dispatch(candidate(event="2"))
    assert not second.accepted
    assert "already has an active" in str(second.reason)


def test_active_run_different_workspace_rejects_comment_too(tmp_path: Path) -> None:
    store = store_with_workspaces(tmp_path / "controller.db")
    dispatcher = HostedDispatcher(store, frozenset(), frozenset())
    first = dispatcher.dispatch(candidate())
    assert first.accepted

    reassigned_comment = replace(
        candidate(event="2"),
        event_kind="comment",
        actor_username="bob",
        assignees=(Assignee("bob", 43),),
    )
    result = dispatcher.dispatch(reassigned_comment)

    assert not result.accepted
    assert "reassigned" in str(result.reason)


def test_hosted_cycle_drains_pending_queue_after_terminal_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = store_with_workspaces(tmp_path / "controller.db")
    seed_bootstrapped_project(
        store, "gitlab.example.com/group/app", "gitlab.example.com", "group/app", 7
    )
    dispatcher = HostedDispatcher(store, frozenset(), frozenset())
    conversation_key = "gitlab.example.com/group/app#12"
    now = 1_700_000_000.0
    pending_entries = [
        {
            "messages": ["first message"],
            "reply_target": {"kind": "issue", "iid": 12},
            "actor_username": "alice",
            "actor_user_id": 42,
            "event_key": "gitlab.example.com/group/app:comment:10",
            "queued_at": now,
        },
        {
            "messages": ["second message"],
            "reply_target": {"kind": "issue", "iid": 12, "discussion_id": "disc-1"},
            "actor_username": "alice",
            "actor_user_id": 42,
            "event_key": "gitlab.example.com/group/app:comment:11",
            "queued_at": now + 1,
        },
    ]
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO conversations(
                conversation_key, project_key, issue_iid, workspace_id,
                status, provider, model, effort, current_job_id, pending_json, updated_at
            ) VALUES (?, 'gitlab.example.com/group/app', 12, 'alice', 'idle', 'pi', 'gpt-5.6-sol', 'high', ?, ?, ?)
            """,
            (conversation_key, "old-job", json.dumps(pending_entries), now),
        )
        connection.execute(
            """
            INSERT INTO jobs(job_id, conversation_key, workspace_id, trigger_event_key, state, envelope_json, created_at)
            VALUES ('old-job', ?, 'alice', 'trigger-key-old', 'succeeded', '{}', ?)
            """,
            (conversation_key, now),
        )
    gl = FakeHostedGL(
        {
            "projects/7/events": [],
            "projects/7/issues/12": {
                "iid": 12,
                "web_url": "https://gitlab.example.com/group/app/-/issues/12",
                "title": "Ship hosted runner",
                "description": "Implement it",
                "state": "opened",
                "assignees": [{"username": "alice", "id": 42}],
            },
        }
    )
    monkeypatch.setattr(watcher, "GitLab", lambda host, token: gl)
    proj = {
        "host": "gitlab.example.com",
        "path": "group/app",
        "id": 7,
        "triggers": ["mention"],
    }
    cfg = {"projects": [proj]}

    watcher.hosted_cycle(cfg, store, dispatcher, "bot-token")

    with closing(store.connect()) as connection:
        job = connection.execute(
            "SELECT envelope_json FROM jobs WHERE job_id != 'old-job'"
        ).fetchone()
        conv = connection.execute(
            "SELECT pending_json FROM conversations WHERE conversation_key = ?",
            (conversation_key,),
        ).fetchone()
    assert job is not None
    envelope = json.loads(job["envelope_json"])
    assert envelope["messages"] == ["first message", "second message"]
    assert envelope["reply_target"] == {
        "kind": "issue",
        "iid": 12,
        "discussion_id": "disc-1",
    }
    assert json.loads(conv["pending_json"]) == []

    watcher.hosted_cycle(cfg, store, dispatcher, "bot-token")
    with closing(store.connect()) as connection:
        jobs_count = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert jobs_count == 2  # no re-dispatch: drained event already consumed


def test_hosted_cycle_transient_mr_fetch_halts_and_retries_next_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = store_with_workspaces(tmp_path / "controller.db")
    seed_bootstrapped_project(
        store, "gitlab.example.com/group/app", "gitlab.example.com", "group/app", 7
    )
    dispatcher = HostedDispatcher(store, frozenset(), frozenset())

    class FlakyGL:
        def __init__(self) -> None:
            self.mr_calls = 0

        def get(self, path: str, **params):
            if path == "projects/7/events":
                return [
                    {
                        "id": 1,
                        "author": {"username": "alice", "id": 1},
                        "note": {
                            "id": 555,
                            "system": False,
                            "noteable_type": "MergeRequest",
                            "noteable_iid": 5,
                            "body": "@agent what's the status?",
                        },
                    }
                ]
            if path == "projects/7/merge_requests/5":
                self.mr_calls += 1
                raise requests.RequestException("gitlab is down")
            raise AssertionError(f"unexpected GET {path}")

    gl = FlakyGL()
    monkeypatch.setattr(watcher, "GitLab", lambda host, token: gl)
    proj = {
        "host": "gitlab.example.com",
        "path": "group/app",
        "id": 7,
        "triggers": ["mention"],
    }
    cfg = {"projects": [proj]}

    watcher.hosted_cycle(cfg, store, dispatcher, "bot-token")

    with closing(store.connect()) as connection:
        jobs = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        receipts = connection.execute("SELECT COUNT(*) FROM event_receipts").fetchone()[
            0
        ]
        last_event_id = connection.execute(
            "SELECT last_event_id FROM projects WHERE project_key = ?",
            ("gitlab.example.com/group/app",),
        ).fetchone()[0]
    assert jobs == 0
    assert receipts == 0
    assert last_event_id == 0  # halted before the transient event: not consumed
    assert gl.mr_calls == 1

    # Next cycle with healthy fetches: the same event is re-fetched and dispatched normally.
    gl2 = hosted_mr_note_gl()
    monkeypatch.setattr(watcher, "GitLab", lambda host, token: gl2)
    watcher.hosted_cycle(cfg, store, dispatcher, "bot-token")
    with closing(store.connect()) as connection:
        jobs = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert jobs == 1


def test_hosted_cycle_transient_failure_gives_up_after_900s(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = store_with_workspaces(tmp_path / "controller.db")
    seed_bootstrapped_project(
        store, "gitlab.example.com/group/app", "gitlab.example.com", "group/app", 7
    )
    dispatcher = HostedDispatcher(store, frozenset(), frozenset())

    class FlakyGL:
        def get(self, path: str, **params):
            if path == "projects/7/events":
                return [
                    {
                        "id": 1,
                        "author": {"username": "alice", "id": 1},
                        "note": {
                            "id": 555,
                            "system": False,
                            "noteable_type": "MergeRequest",
                            "noteable_iid": 5,
                            "body": "@agent what's the status?",
                        },
                        "created_at": "2020-01-01T00:00:00Z",
                    }
                ]
            if path == "projects/7/merge_requests/5":
                raise requests.RequestException("gitlab is down")
            raise AssertionError(f"unexpected GET {path}")

    monkeypatch.setattr(watcher, "GitLab", lambda host, token: FlakyGL())
    proj = {
        "host": "gitlab.example.com",
        "path": "group/app",
        "id": 7,
        "triggers": ["mention"],
    }
    cfg = {"projects": [proj]}

    watcher.hosted_cycle(cfg, store, dispatcher, "bot-token")

    with closing(store.connect()) as connection:
        jobs = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        receipt = connection.execute(
            "SELECT payload_json FROM event_receipts WHERE project_key = ?",
            ("gitlab.example.com/group/app",),
        ).fetchone()
        last_event_id = connection.execute(
            "SELECT last_event_id FROM projects WHERE project_key = ?",
            ("gitlab.example.com/group/app",),
        ).fetchone()[0]
    assert jobs == 0
    assert receipt is not None
    assert (
        "giving up after repeated transient failures"
        in json.loads(receipt["payload_json"])["reason"]
    )
    assert last_event_id == 1  # given up: event receipted and cursor advances past it


def test_comment_during_finishing_window_queues(tmp_path: Path) -> None:
    # A run can reach 'finishing' (terminal, note undelivered) seconds after
    # dispatch; a comment landing in that window must queue, not burn.
    store = store_with_workspaces(tmp_path / "controller.db")
    dispatcher = HostedDispatcher(store, frozenset(), frozenset())
    first = dispatcher.dispatch(candidate())
    assert first.accepted
    with store.transaction() as connection:
        connection.execute("UPDATE jobs SET state = 'succeeded'")
        connection.execute("UPDATE conversations SET status = 'finishing'")

    comment = replace(
        candidate(event="9"),
        event_kind="comment",
        trigger_messages=("one more thing",),
    )
    result = dispatcher.dispatch(comment)

    assert not result.accepted
    assert result.reason == "queued for next run"
