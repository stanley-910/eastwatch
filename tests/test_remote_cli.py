from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from eastwatch import bw as bw_module
from eastwatch.bw import RemoteConfig, remote_argv
from eastwatch.controller.models import JobEnvelope, RunReferences
from eastwatch.controller.store import ControllerStore, token_digest
from eastwatch.fleet.core import FleetRow
from eastwatch import remote as remote_module
from eastwatch.remote import (
    HostedFleet,
    docker_exec,
    encode_server_request,
    host_path,
    server_main,
    server_request,
)


def seed(database: Path, root: Path) -> None:
    store = ControllerStore(database)
    store.migrate()
    (root / "alice" / "worktrees" / "issue-12").mkdir(parents=True)
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO projects(project_key, host, project_path, project_id, bootstrapped) "
            "VALUES ('gitlab.example.com/group/app', 'gitlab.example.com', 'group/app', 7, 1)"
        )
        for username, user_id in (("alice", 42), ("bob", 43)):
            connection.execute(
                """
                INSERT INTO workspaces(
                    workspace_id, owner_username, owner_user_id, token_sha256,
                    capacity, default_spec, allowed_specs_json,
                    container_name, host_root, created_at
                ) VALUES (?, ?, ?, ?, 10, 'pi:gpt-5.6-sol:high', '[]', ?, ?, 1)
                """,
                (
                    username,
                    username,
                    user_id,
                    token_digest(f"{username}-token"),
                    f"bw-workspace-{username}",
                    str(root / username),
                ),
            )
        for index, (username, issue_iid) in enumerate(
            (("alice", 12), ("bob", 13)), start=1
        ):
            conversation = f"gitlab.example.com/group/app#{issue_iid}"
            job_id = f"job-{username}"
            envelope = {
                "schema_version": 1,
                "job_id": job_id,
                "conversation_key": conversation,
                "host": "gitlab.example.com",
                "project_path": "group/app",
                "project_id": 7,
                "issue_iid": issue_iid,
                "issue_url": f"https://gitlab.example.com/group/app/-/issues/{issue_iid}",
                "trigger_kind": "agent::ready",
                "trigger_event_key": f"event-{index}",
                "owner_username": username,
                "provider": "pi",
                "model": "gpt-5.6-sol",
                "effort": "high",
                "messages": [],
                "reply_target": {"kind": "issue", "iid": issue_iid},
            }
            connection.execute(
                """
                INSERT INTO conversations(
                    conversation_key, project_key, issue_iid, workspace_id,
                    status, provider, model, effort, current_job_id,
                    session_refs_json, updated_at
                ) VALUES (?, 'gitlab.example.com/group/app', ?, ?, 'succeeded',
                          'pi', 'gpt-5.6-sol', 'high', ?, ?, 2)
                """,
                (
                    conversation,
                    issue_iid,
                    username,
                    job_id,
                    json.dumps(
                        {"session_file_relpath": f".pi/agent/sessions/{job_id}.jsonl"}
                    ),
                ),
            )
            connection.execute(
                """
                INSERT INTO jobs(
                    job_id, conversation_key, workspace_id, trigger_event_key,
                    state, envelope_json, lease_generation, created_at, started_at, finished_at
                ) VALUES (?, ?, ?, ?, 'succeeded', ?, 1, 1, 1, 2)
                """,
                (
                    job_id,
                    conversation,
                    username,
                    f"event-{index}",
                    json.dumps(envelope),
                ),
            )
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, job_id, workspace_id, lease_generation, state,
                    refs_json, result_json, started_at, finished_at
                ) VALUES (?, ?, ?, 1, 'succeeded', ?, ?, 1, 2)
                """,
                (
                    f"run-{username}",
                    job_id,
                    username,
                    json.dumps(
                        {
                            "run_id": f"run-{username}",
                            "tmux_session": f"bw-run-{username}",
                            "worktree_relpath": f"worktrees/issue-{issue_iid}",
                            "run_dir_relpath": f"state/issue-{issue_iid}/runs/run-{username}",
                            "session_file_relpath": None,
                        }
                    ),
                    json.dumps(
                        {"session_file_relpath": f".pi/agent/sessions/{job_id}.jsonl"}
                    ),
                ),
            )


def test_old_rows_remain_compatible() -> None:
    row = FleetRow.from_mapping(
        {
            "key": "group/app#1",
            "status": "done",
            "derived": "finished",
            "model": "pi:gpt-5.6-sol",
        }
    )
    assert row.remote is False
    assert row.owner == ""
    assert row.capabilities == ()


def test_hosted_rows_are_owner_filtered_and_exact_match_wins(tmp_path: Path) -> None:
    database = tmp_path / "controller.db"
    seed(database, tmp_path / "users")
    fleet = HostedFleet(database)

    rows = fleet.rows("alice")

    assert [(row.job_id, row.run_id) for row in rows] == [("job-alice", "run-alice")]
    assert fleet.match("alice", "job-alice").owner == "alice"
    with pytest.raises(RuntimeError, match="no hosted run"):
        fleet.match("alice", "job-bob")


def test_hosted_row_session_falls_back_to_run_refs(tmp_path: Path) -> None:
    # A live run's session path is only known via the run's refs_json (the
    # mid-run upgrade) until the job completes and result_json/session_refs_json
    # catch up; the row builder must still surface it.
    database = tmp_path / "controller.db"
    store = ControllerStore(database)
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
            ) VALUES ('alice', 'alice', 42, ?, 10, 'pi:gpt-5.6-sol:high', '[]',
                      'bw-workspace-alice', ?, 1.0)
            """,
            (token_digest("alice-token"), str(tmp_path / "alice")),
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
    envelope = JobEnvelope(
        schema_version=1,
        job_id="job-live",
        conversation_key="gitlab.example.com/group/app#12",
        host="gitlab.example.com",
        project_path="group/app",
        project_id=7,
        issue_iid=12,
        issue_url="https://gitlab.example.com/group/app/-/issues/12",
        trigger_kind="agent::ready",
        trigger_event_key="event-live",
        owner_username="alice",
        provider="pi",
        model="gpt-5.6-sol",
        effort="high",
        context={},
        messages=(),
        reply_target={"kind": "issue", "iid": 12},
    )
    store.enqueue_job(envelope, now=1.0)
    lease = store.claim_jobs("alice", limit=1, lease_seconds=30, now=10.0)[0]
    refs = RunReferences(
        run_id="run-live",
        tmux_session="bw-run-live",
        worktree_relpath="worktrees/issue-12",
        run_dir_relpath="state/issue-12/runs/run-live",
        session_file_relpath=".pi/agent/sessions/job-live.jsonl",
    )
    store.mark_started("alice", "job-live", lease.lease_generation, refs, now=11.0)

    fleet = HostedFleet(database)
    rows = fleet.rows("alice")

    assert len(rows) == 1
    assert rows[0].session == "/home/bw/.pi/agent/sessions/job-live.jsonl"


def test_issue_match_chooses_latest_but_partial_ambiguity_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fleet = HostedFleet(Path("/unused"))
    latest = FleetRow.from_mapping(
        {
            "job_id": "job-new",
            "run_id": "run-new",
            "key": "group/app#12",
            "identity": "gitlab/group/app#12",
            "url": "https://gitlab/group/app/-/issues/12",
        }
    )
    older = FleetRow.from_mapping(
        {
            "job_id": "job-old",
            "run_id": "run-old",
            "key": "group/app#12",
            "identity": "gitlab/group/app#12",
            "url": "https://gitlab/group/app/-/issues/12",
        }
    )
    other = FleetRow.from_mapping(
        {
            "job_id": "job-other",
            "run_id": "run-other",
            "key": "group/app#13",
            "identity": "gitlab/group/app#13",
            "url": "https://gitlab/group/app/-/issues/13",
        }
    )
    monkeypatch.setattr(fleet, "rows", lambda owner: (latest, older, other))

    assert fleet.match("alice", "group/app#12").job_id == "job-new"
    with pytest.raises(RuntimeError, match="ambiguous"):
        fleet.match("alice", "group/app")


def test_numeric_query_matches_newest_issue_row_without_ambiguity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fleet = HostedFleet(Path("/unused"))
    latest = FleetRow.from_mapping(
        {
            "job_id": "job-new",
            "run_id": "run-new",
            "key": "group/app#3",
            "identity": "gitlab/group/app#3",
            "url": "https://gitlab/group/app/-/issues/3",
        }
    )
    older = FleetRow.from_mapping(
        {
            "job_id": "job-old",
            "run_id": "run-old",
            "key": "group/app#3",
            "identity": "gitlab/group/app#3",
            "url": "https://gitlab/group/app/-/issues/3",
        }
    )
    other = FleetRow.from_mapping(
        {
            "job_id": "job-other",
            "run_id": "run-other",
            "key": "group/app#13",
            "identity": "gitlab/group/app#13",
            "url": "https://gitlab/group/app/-/issues/13",
        }
    )
    monkeypatch.setattr(fleet, "rows", lambda owner: (latest, older, other))

    assert fleet.match("alice", "3").job_id == "job-new"
    assert fleet.match("alice", "#3").job_id == "job-new"


def test_numeric_query_without_matching_issue_falls_through_to_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fleet = HostedFleet(Path("/unused"))
    thirteen = FleetRow.from_mapping(
        {
            "job_id": "job-13",
            "run_id": "run-13",
            "key": "group/app#13",
            "identity": "gitlab/group/app#13",
            "url": "https://gitlab/group/app/-/issues/13",
        }
    )
    twenty_three = FleetRow.from_mapping(
        {
            "job_id": "job-23",
            "run_id": "run-23",
            "key": "group/app#23",
            "identity": "gitlab/group/app#23",
            "url": "https://gitlab/group/app/-/issues/23",
        }
    )
    monkeypatch.setattr(fleet, "rows", lambda owner: (thirteen, twenty_three))

    with pytest.raises(RuntimeError, match="ambiguous"):
        fleet.match("alice", "3")


def test_encoded_request_rejects_command_or_option_smuggling() -> None:
    token = encode_server_request("alice", "--database", ("/tmp/other.db", "fleet"))
    with pytest.raises(RuntimeError, match="invalid encoded server request"):
        server_request(token)


def test_stored_path_escape_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="unsafe stored relative path"):
        host_path(str(tmp_path), "../outside")


def test_local_ssh_query_stays_one_argv_item() -> None:
    config = RemoteConfig(
        ssh_target="admin@eastwatch-host",
        ssh_alias="eastwatch",
        owner="alice",
        server_command="/opt/eastwatch/.venv/bin/bw",
        editor="code",
    )
    query = "job-1; rm -rf /"

    argv = remote_argv(config, "inspect", query)

    assert argv[0] == "ssh"
    # Multiplexing options ride between "ssh" and the target; the tail of the
    # command is fixed.
    assert argv[-4:-1] == [
        "admin@eastwatch-host",
        "/opt/eastwatch/.venv/bin/bw",
        "--server-request",
    ]
    assert all(character.isalnum() or character in "-_" for character in argv[-1])
    assert query not in " ".join(argv)


def test_interactive_and_follow_verbs_bypass_the_shared_mux() -> None:
    # Long-lived / interactive ssh sessions get killed uncleanly; routed
    # through the shared control master they can wedge it and hang every
    # other bw call. They must run on dedicated connections.
    config = RemoteConfig(
        ssh_target="admin@eastwatch-host",
        ssh_alias="eastwatch",
        owner="alice",
        server_command="/opt/eastwatch/.venv/bin/bw",
        editor="code",
    )

    muxed = remote_argv(config, "fleet", "--json")
    assert "ControlMaster=auto" in muxed

    direct = remote_argv(config, "resume", "job-1", mux=False)
    assert "ControlMaster=no" in direct
    assert "ControlMaster=auto" not in direct
    assert direct[-3:-1] == [
        "/opt/eastwatch/.venv/bin/bw",
        "--server-request",
    ]


def test_capture_timeout_resets_mux_and_retries_direct(monkeypatch) -> None:
    import subprocess

    config = RemoteConfig(
        ssh_target="admin@eastwatch-host",
        ssh_alias="eastwatch",
        owner="alice",
        server_command="/opt/eastwatch/.venv/bin/bw",
        editor="code",
    )
    calls: list[list[str]] = []
    resets: list[bool] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 15))
        return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")

    monkeypatch.setattr(bw_module.subprocess, "run", fake_run)
    monkeypatch.setattr(bw_module, "reset_mux", lambda cfg: resets.append(True))

    output = bw_module.capture(config, "fleet", "--json")

    assert output == "[]"
    assert resets == [True]
    assert "ControlMaster=auto" in calls[0]
    assert "ControlMaster=no" in calls[1]


def test_server_logs_attach_and_resume_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "controller.db"
    seed(database, tmp_path / "users")
    calls = []
    lock_state = {"closed": False}

    class Lock:
        def close(self):
            lock_state["closed"] = True

    def fake_exec(row, command, *, interactive, workdir=None):
        assert not lock_state["closed"]
        calls.append((command, interactive, workdir))
        return 0

    monkeypatch.setattr(remote_module, "docker_exec", fake_exec)
    monkeypatch.setattr(remote_module, "container_tmux_alive", lambda row: False)
    monkeypatch.setattr(remote_module, "resume_lock", lambda row: Lock())

    assert (
        server_main(
            [
                "--owner",
                "alice",
                "--database",
                str(database),
                "logs",
                "job-alice",
                "--follow",
            ]
        )
        == 0
    )
    assert calls[-1][0] == [
        "tail",
        "-F",
        "-n",
        "200",
        "/home/bw/state/issue-12/runs/run-alice/run.jsonl",
    ]
    assert (
        server_main(
            [
                "--owner",
                "alice",
                "--database",
                str(database),
                "logs",
                "job-alice",
                "--follow",
                "--session",
            ]
        )
        == 0
    )
    assert calls[-1][0] == [
        "tail",
        "-F",
        "-n",
        "+1",
        "/home/bw/.pi/agent/sessions/job-alice.jsonl",
    ]
    assert (
        server_main(
            [
                "--owner",
                "alice",
                "--database",
                str(database),
                "logs",
                "job-alice",
                "--session",
            ]
        )
        == 0
    )
    assert calls[-1][0] == [
        "tail",
        "-n",
        "+1",
        "/home/bw/.pi/agent/sessions/job-alice.jsonl",
    ]
    with pytest.raises(RuntimeError, match="not an active tmux run"):
        server_main(
            ["--owner", "alice", "--database", str(database), "attach", "job-alice"]
        )
    assert (
        server_main(
            ["--owner", "alice", "--database", str(database), "resume", "job-alice"]
        )
        == 0
    )
    assert calls[-1] == (
        ["pi", "--session", "/home/bw/.pi/agent/sessions/job-alice.jsonl"],
        True,
        "/home/bw/worktrees/issue-12",
    )
    assert lock_state["closed"] is True
    lock_state["closed"] = False

    store = ControllerStore(database)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE jobs SET state = 'running', finished_at = NULL WHERE job_id = 'job-alice'"
        )
        connection.execute(
            "UPDATE runs SET state = 'running', finished_at = NULL WHERE job_id = 'job-alice'"
        )
    assert (
        server_main(
            ["--owner", "alice", "--database", str(database), "attach", "job-alice"]
        )
        == 0
    )
    assert calls[-1][0] == [
        "tmux",
        "attach-session",
        "-r",
        "-t",
        "=bw-run-alice",
    ]
    with pytest.raises(RuntimeError, match="not ready for post-run resume"):
        server_main(
            ["--owner", "alice", "--database", str(database), "resume", "job-alice"]
        )


def test_fleet_text_output_shows_heartbeat_age_for_active_rows(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "controller.db"
    seed(database, tmp_path / "users")
    store = ControllerStore(database)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE jobs SET state = 'running', last_heartbeat_at = ? WHERE job_id = 'job-alice'",
            (100.0,),
        )
        connection.execute(
            "UPDATE runs SET state = 'running' WHERE job_id = 'job-alice'"
        )
    monkeypatch.setattr(remote_module.time, "time", lambda: 130.0)

    assert server_main(["--owner", "alice", "--database", str(database), "fleet"]) == 0
    alice_out = capsys.readouterr().out
    assert " hb=30s" in alice_out

    assert server_main(["--owner", "bob", "--database", str(database), "fleet"]) == 0
    bob_out = capsys.readouterr().out
    assert "hb=" not in bob_out


def test_local_path_copy_and_open_use_inspected_host_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = RemoteConfig("host", "alias", "alice", "/opt/bw", "code")
    row = FleetRow.from_mapping(
        {
            "job_id": "job-1",
            "host_path": "/srv/eastwatch/users/alice/home/worktrees/issue-12",
        }
    )
    copied = []
    commands = []
    monkeypatch.setattr(bw_module, "load_config", lambda: config)
    monkeypatch.setattr(bw_module, "inspect", lambda cfg, query: row)
    monkeypatch.setattr(bw_module, "copy_text", copied.append)
    monkeypatch.setattr(
        bw_module.subprocess,
        "run",
        lambda argv, **kwargs: commands.append(argv) or CompletedProcess(argv, 0),
    )

    assert bw_module.main(["path", "job-1", "--copy"]) == 0
    assert copied == [row.host_path]
    assert bw_module.main(["open", "job-1"]) == 0
    assert commands[-1] == [
        "code",
        "--remote",
        "ssh-remote+alias",
        row.host_path,
    ]


def test_docker_exec_uses_fixed_container_and_workdir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    monkeypatch.setattr(
        "eastwatch.remote.subprocess.run",
        lambda argv, **kwargs: calls.append(argv) or CompletedProcess(argv, 0),
    )
    row = FleetRow.from_mapping(
        {
            "key": "group/app#12",
            "status": "succeeded",
            "derived": "finished",
            "model": "pi:gpt-5.6-sol",
            "container": "bw-workspace-alice",
            "remote": True,
        }
    )

    assert (
        docker_exec(
            row,
            ["pi", "--session", "/home/bw/.pi/session.jsonl"],
            interactive=True,
            workdir="/home/bw/worktrees/issue-12",
        )
        == 0
    )
    assert calls == [
        [
            "sudo",
            "docker",
            "exec",
            "-it",
            "--workdir",
            "/home/bw/worktrees/issue-12",
            "bw-workspace-alice",
            "pi",
            "--session",
            "/home/bw/.pi/session.jsonl",
        ]
    ]


def test_logs_server_args_accept_session_and_follow_flags() -> None:
    from eastwatch.remote import valid_server_args

    assert valid_server_args("logs", ["job-1"])
    assert valid_server_args("logs", ["job-1", "--follow"])
    assert valid_server_args("logs", ["job-1", "--session"])
    assert valid_server_args("logs", ["job-1", "--follow", "--session"])
    assert not valid_server_args("logs", ["job-1", "--follow", "--follow"])
    assert not valid_server_args("logs", ["job-1", "--rm"])
    assert not valid_server_args("logs", ["--follow"])
    assert not valid_server_args("logs", [])


def test_home_server_args_accept_no_flags() -> None:
    from eastwatch.remote import valid_server_args

    assert valid_server_args("home", [])
    assert not valid_server_args("home", ["--json"])
    assert not valid_server_args("home", ["extra"])


def test_home_prints_owner_host_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "controller.db"
    users_root = tmp_path / "users"
    seed(database, users_root)

    assert server_main(["--owner", "alice", "--database", str(database), "home"]) == 0
    assert capsys.readouterr().out == f"{users_root / 'alice'}\n"

    assert server_main(["--owner", "bob", "--database", str(database), "home"]) == 0
    assert capsys.readouterr().out == f"{users_root / 'bob'}\n"
