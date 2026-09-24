from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from eastwatch.controller.api import ApiSettings, create_app
from eastwatch.controller.models import JobEnvelope
from eastwatch.controller.store import ControllerStore, token_digest


def seeded_store(path: Path) -> ControllerStore:
    store = ControllerStore(path)
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
                      'bw-workspace-alice', '/srv/eastwatch/users/alice/home', 1)
            """,
            (token_digest("alice-token"), json.dumps(["pi:gpt-5.6-sol:high"])),
        )
        connection.execute(
            """
            INSERT INTO conversations(
                conversation_key, project_key, issue_iid, workspace_id,
                status, provider, model, effort, updated_at
            ) VALUES ('gitlab.example.com/group/app#12', 'gitlab.example.com/group/app',
                      12, 'alice', 'idle', 'pi', 'gpt-5.6-sol', 'high', 1)
            """
        )
    store.enqueue_job(
        JobEnvelope(
            schema_version=1,
            job_id="job-1",
            conversation_key="gitlab.example.com/group/app#12",
            host="gitlab.example.com",
            project_path="group/app",
            project_id=7,
            issue_iid=12,
            issue_url="https://gitlab.example.com/group/app/-/issues/12",
            trigger_kind="agent::ready",
            trigger_event_key="gitlab.example.com/group/app:label:9",
            owner_username="alice",
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
        ),
        now=1,
    )
    return store


def test_runner_http_lifecycle_and_retries(tmp_path: Path) -> None:
    store = seeded_store(tmp_path / "controller.db")
    client = TestClient(create_app(store, ApiSettings(), clock=lambda: 10.0))
    auth = {"Authorization": "Bearer alice-token"}

    assert (
        client.post("/v1/runner/claim", json={"free_slots": 1}, headers={}).status_code
        == 401
    )
    assert (
        client.post(
            "/v1/runner/claim",
            json={"free_slots": 1},
            headers={"Authorization": "Bearer wrong"},
        ).status_code
        == 401
    )
    assert client.post("/v1/runner/ready", json={}, headers=auth).status_code == 204
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT ready FROM workspaces WHERE workspace_id = 'alice'"
            ).fetchone()[0]
            == 1
        )
    claim = client.post(
        "/v1/runner/claim",
        json={"free_slots": 1, "wait_seconds": 0},
        headers=auth,
    )
    assert claim.status_code == 200
    leased = claim.json()["jobs"][0]
    generation = leased["lease_generation"]
    assert leased["envelope"]["owner_username"] == "alice"

    invalid = client.post(
        "/v1/runner/jobs/job-1/started",
        json={
            "lease_generation": generation,
            "refs": {
                "run_id": "run-1",
                "tmux_session": "bw-run-1",
                "worktree_relpath": "/etc",
                "run_dir_relpath": "state/run-1",
            },
        },
        headers=auth,
    )
    assert invalid.status_code == 422

    refs = {
        "run_id": "run-1",
        "tmux_session": "bw-run-1",
        "worktree_relpath": "worktrees/issue-12",
        "run_dir_relpath": "state/issue-12/runs/run-1",
        "session_file_relpath": ".pi/agent/sessions/issue-12/run-1.jsonl",
    }
    first_start = client.post(
        "/v1/runner/jobs/job-1/started",
        json={"lease_generation": generation, "refs": refs},
        headers=auth,
    )
    assert first_start.json() == {"accepted": True}
    start_retry = client.post(
        "/v1/runner/jobs/job-1/started",
        json={"lease_generation": generation, "refs": refs},
        headers=auth,
    )
    assert start_retry.json() == {"accepted": False}
    changed = dict(refs, tmux_session="bw-different")
    assert (
        client.post(
            "/v1/runner/jobs/job-1/started",
            json={"lease_generation": generation, "refs": changed},
            headers=auth,
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/v1/runner/heartbeat",
            json={"active": [{"job_id": "job-1", "lease_generation": generation}]},
            headers=auth,
        ).status_code
        == 204
    )

    completion = {
        "lease_generation": generation,
        "state": "succeeded",
        "result": {"final": "done"},
    }
    assert client.post(
        "/v1/runner/jobs/job-1/complete", json=completion, headers=auth
    ).json() == {"accepted": True}
    assert client.post(
        "/v1/runner/jobs/job-1/complete", json=completion, headers=auth
    ).json() == {"accepted": False}
    assert (
        client.post(
            "/v1/runner/jobs/job-1/complete",
            json=dict(completion, lease_generation=generation + 1),
            headers=auth,
        ).status_code
        == 409
    )
