from __future__ import annotations

import json
import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from eastwatch.controller.models import (
    ActiveLease,
    Completion,
    JobEnvelope,
    OwnershipError,
    RunReferences,
    StaleLeaseError,
)
from eastwatch.controller.store import ControllerStore, token_digest


def seed(store: ControllerStore) -> None:
    store.migrate()
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
            ) VALUES ('alice', 'alice', 42, ?, 10, 'pi:gpt-5.6-sol:high', ?,
                      'bw-workspace-alice', '/srv/eastwatch/users/alice/home', 1.0)
            """,
            (token_digest("alice-token"), json.dumps(["pi:gpt-5.6-sol:high"])),
        )
        connection.execute(
            """
            INSERT INTO conversations(
                conversation_key, project_key, issue_iid, workspace_id,
                status, provider, model, effort, updated_at
            ) VALUES ('gitlab.example.com/group/app#12', 'gitlab.example.com/group/app',
                      12, 'alice', 'idle', 'pi', 'gpt-5.6-sol', 'high', 1.0)
            """
        )


def envelope(job_id: str = "job-1", owner: str = "alice") -> JobEnvelope:
    return JobEnvelope(
        schema_version=1,
        job_id=job_id,
        conversation_key="gitlab.example.com/group/app#12",
        host="gitlab.example.com",
        project_path="group/app",
        project_id=7,
        issue_iid=12,
        issue_url="https://gitlab.example.com/group/app/-/issues/12",
        trigger_kind="agent::ready",
        trigger_event_key=f"label:{job_id}",
        owner_username=owner,
        provider="pi",
        model="gpt-5.6-sol",
        effort="high",
        context={
            "issue_title": "Ship issue 12",
            "issue_description": "Implement it",
            "thread_context": "",
            "worker_briefing": None,
            "jira_context": "",
        },
        messages=("Implement issue 12",),
        reply_target={"kind": "issue", "iid": 12},
    )


def refs(run_id: str = "run-1") -> RunReferences:
    return RunReferences(
        run_id=run_id,
        tmux_session=f"bw-{run_id}",
        worktree_relpath="worktrees/group-app/issue-12",
        run_dir_relpath=f"state/issues/12/runs/{run_id}",
        session_file_relpath=f".pi/agent/sessions/issue-12/{run_id}.jsonl",
    )


def test_concurrent_claim_returns_job_once(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    seed(store)
    store.enqueue_job(envelope())
    barrier = threading.Barrier(2)

    def claim() -> tuple[object, ...]:
        barrier.wait(timeout=2)
        return store.claim_jobs("alice", limit=1, lease_seconds=30, now=10.0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _: claim(), range(2)))

    assert sum(len(result) for result in results) == 1


def test_owner_is_derived_from_conversation(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    seed(store)

    with pytest.raises(OwnershipError):
        store.enqueue_job(envelope(owner="mallory"))

    with closing(store.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_dispatch_receipt_and_job_commit_together(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    seed(store)
    job = envelope()

    assert store.record_dispatch_event(
        job,
        event_kind="label",
        actor_username="alice",
        payload={"label": "agent::ready"},
        now=5.0,
    ) == ("job-1", True)
    assert store.record_dispatch_event(
        job,
        event_kind="label",
        actor_username="alice",
        payload={"label": "agent::ready"},
        now=6.0,
    ) == ("job-1", False)
    with closing(store.connect()) as connection:
        receipt_count = connection.execute(
            "SELECT COUNT(*) FROM event_receipts"
        ).fetchone()[0]
        job_count = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert (receipt_count, job_count) == (1, 1)


def test_start_completion_and_outbox_are_fenced_and_idempotent(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    seed(store)
    assert store.enqueue_job(envelope(), now=1.0) == "job-1"
    assert store.enqueue_job(envelope(), now=2.0) == "job-1"
    lease = store.claim_jobs("alice", limit=1, lease_seconds=30, now=10.0)[0]

    with pytest.raises(StaleLeaseError):
        store.mark_started(
            "alice", "job-1", lease.lease_generation + 1, refs(), now=11.0
        )

    assert store.mark_started(
        "alice", "job-1", lease.lease_generation, refs(), now=11.0
    )
    assert not store.mark_started(
        "alice", "job-1", lease.lease_generation, refs(), now=11.5
    )
    completion = Completion(state="succeeded", result={"final": "done"}, error=None)
    assert store.complete_job(
        "alice", "job-1", lease.lease_generation, completion, now=12.0
    )
    assert not store.complete_job(
        "alice", "job-1", lease.lease_generation, completion, now=13.0
    )

    with pytest.raises(StaleLeaseError):
        store.heartbeat(
            "alice",
            [ActiveLease(job_id="job-1", lease_generation=lease.lease_generation)],
            lease_seconds=30,
            now=14.0,
        )

    claimed = store.claim_outbox(limit=10, lease_seconds=30, now=14.0)
    assert [item["kind"] for item in claimed] == ["job-started", "job-completed"]
    claimed = [item for item in claimed if item["kind"] == "job-completed"]
    store.mark_outbox_failed(
        int(claimed[0]["outbox_id"]),
        int(claimed[0]["lease_generation"]),
        "temporary",
        retry_at=20.0,
    )
    assert store.claim_outbox(limit=10, lease_seconds=30, now=19.0) == ()
    retried = store.claim_outbox(limit=10, lease_seconds=30, now=20.0)
    assert retried[0]["attempt"] == 2
    with pytest.raises(RuntimeError):
        store.mark_outbox_delivered(
            int(retried[0]["outbox_id"]),
            int(claimed[0]["lease_generation"]),
            now=20.5,
        )
    store.mark_outbox_delivered(
        int(retried[0]["outbox_id"]),
        int(retried[0]["lease_generation"]),
        now=21.0,
    )


def test_mark_started_upgrades_session_relpath_only(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    seed(store)
    store.enqueue_job(envelope(), now=1.0)
    lease = store.claim_jobs("alice", limit=1, lease_seconds=30, now=10.0)[0]

    started = replace(refs(), session_file_relpath=None)
    assert store.mark_started(
        "alice", "job-1", lease.lease_generation, started, now=11.0
    )

    upgraded = refs()
    assert (
        store.mark_started("alice", "job-1", lease.lease_generation, upgraded, now=11.5)
        is False
    )

    with closing(store.connect()) as connection:
        rows = connection.execute(
            "SELECT refs_json FROM runs WHERE job_id = 'job-1'"
        ).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["refs_json"]) == upgraded.to_dict()


def test_mark_started_rejects_other_ref_changes(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    seed(store)
    store.enqueue_job(envelope(), now=1.0)
    lease = store.claim_jobs("alice", limit=1, lease_seconds=30, now=10.0)[0]

    assert store.mark_started(
        "alice", "job-1", lease.lease_generation, refs(), now=11.0
    )
    assert (
        store.mark_started("alice", "job-1", lease.lease_generation, refs(), now=11.5)
        is False
    )

    changed = replace(refs(), tmux_session="bw-different")
    with pytest.raises(StaleLeaseError):
        store.mark_started("alice", "job-1", lease.lease_generation, changed, now=12.0)

    with closing(store.connect()) as connection:
        rows = connection.execute(
            "SELECT refs_json FROM runs WHERE job_id = 'job-1'"
        ).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["refs_json"]) == refs().to_dict()


def test_expired_unstarted_requeues_but_started_run_parks(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    seed(store)
    store.enqueue_job(envelope(), now=1.0)
    first = store.claim_jobs("alice", limit=1, lease_seconds=5, now=10.0)[0]

    assert store.reap_expired(now=16.0) == ()
    second = store.claim_jobs("alice", limit=1, lease_seconds=5, now=16.0)[0]
    assert second.lease_generation == first.lease_generation + 1
    store.mark_started("alice", "job-1", second.lease_generation, refs(), now=17.0)

    assert store.reap_expired(now=22.0) == ("job-1",)
    with closing(store.connect()) as connection:
        job = connection.execute(
            "SELECT state, failure_stage FROM jobs WHERE job_id = 'job-1'"
        ).fetchone()
        assert tuple(job) == ("failed", "runner-lost")
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM outbox WHERE dedupe_key = 'runner-lost:job-1'"
            ).fetchone()[0]
            == 1
        )


def test_claim_stamps_last_heartbeat_at(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    seed(store)
    store.enqueue_job(envelope(), now=1.0)
    store.claim_jobs("alice", limit=1, lease_seconds=30, now=10.0)
    with closing(store.connect()) as connection:
        row = connection.execute(
            "SELECT last_heartbeat_at FROM jobs WHERE job_id = 'job-1'"
        ).fetchone()
        assert row["last_heartbeat_at"] == 10.0


def test_heartbeat_advances_last_heartbeat_at(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    seed(store)
    store.enqueue_job(envelope(), now=1.0)
    lease = store.claim_jobs("alice", limit=1, lease_seconds=30, now=10.0)[0]
    store.heartbeat(
        "alice",
        [ActiveLease(job_id="job-1", lease_generation=lease.lease_generation)],
        lease_seconds=30,
        now=20.0,
    )
    with closing(store.connect()) as connection:
        row = connection.execute(
            "SELECT last_heartbeat_at FROM jobs WHERE job_id = 'job-1'"
        ).fetchone()
        assert row["last_heartbeat_at"] == 20.0


def test_reap_expired_logs_requeue_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    seed(store)
    store.enqueue_job(envelope(), now=1.0)
    store.claim_jobs("alice", limit=1, lease_seconds=5, now=10.0)

    with caplog.at_level(logging.WARNING, logger="eastwatch.controller.store"):
        assert store.reap_expired(now=16.0) == ()

    assert any("requeued 1 leased job(s)" in message for message in caplog.messages)
    with closing(store.connect()) as connection:
        job = connection.execute(
            "SELECT state, lease_until FROM jobs WHERE job_id = 'job-1'"
        ).fetchone()
        assert tuple(job) == ("queued", None)


def test_migrate_v1_database_adds_heartbeat_column(tmp_path: Path) -> None:
    path = tmp_path / "controller.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                conversation_key TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                trigger_event_key TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL CHECK (state IN
                    ('queued', 'leased', 'running', 'succeeded', 'failed', 'cancelled')),
                envelope_json TEXT NOT NULL,
                lease_generation INTEGER NOT NULL DEFAULT 0,
                lease_until REAL,
                created_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                failure_stage TEXT,
                failure_reason TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE schema_meta (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                version INTEGER NOT NULL
            )
            """
        )
        connection.execute("INSERT INTO schema_meta(singleton, version) VALUES (1, 1)")
        connection.commit()

    store = ControllerStore(path)
    store.migrate()

    with closing(store.connect()) as connection:
        version = connection.execute(
            "SELECT version FROM schema_meta WHERE singleton = 1"
        ).fetchone()["version"]
        assert version == 2
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
        assert "last_heartbeat_at" in columns

    seed(store)
    assert store.enqueue_job(envelope(), now=1.0) == "job-1"
    lease = store.claim_jobs("alice", limit=1, lease_seconds=30, now=5.0)[0]
    assert lease.lease_generation == 1
    with closing(store.connect()) as connection:
        row = connection.execute(
            "SELECT last_heartbeat_at FROM jobs WHERE job_id = 'job-1'"
        ).fetchone()
        assert row["last_heartbeat_at"] == 5.0


def test_event_receipts_wal_and_integrity(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    seed(store)
    payload = {"label": "agent::ready", "actor": "alice"}

    assert store.record_event(
        "gitlab.example.com/group/app", "label:99", "label", "alice", payload, now=5.0
    )
    assert not store.record_event(
        "gitlab.example.com/group/app", "label:99", "label", "alice", payload, now=6.0
    )
    with closing(store.connect()) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_mark_started_inserts_job_started_outbox_once(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    seed(store)
    store.enqueue_job(envelope(), now=1.0)
    lease = store.claim_jobs("alice", limit=1, lease_seconds=30, now=10.0)[0]

    assert store.mark_started(
        "alice", "job-1", lease.lease_generation, refs(), now=11.0
    )
    assert not store.mark_started(
        "alice", "job-1", lease.lease_generation, refs(), now=11.5
    )

    with closing(store.connect()) as connection:
        rows = connection.execute(
            "SELECT dedupe_key, kind FROM outbox WHERE kind = 'job-started'"
        ).fetchall()
    assert [(row["dedupe_key"], row["kind"]) for row in rows] == [
        ("job-started:job-1", "job-started")
    ]
