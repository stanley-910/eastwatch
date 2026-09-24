from __future__ import annotations

import argparse
import base64
import binascii
import fcntl
import json
import os
import re
import sqlite3
import subprocess
import time
from contextlib import closing
from dataclasses import asdict
from pathlib import Path, PurePosixPath

from eastwatch.fleet.core import FleetRow, chat_lock_path

DERIVED = {
    "queued": "queued",
    "leased": "working",
    "running": "working",
    "succeeded": "finished",
    "failed": "crashed",
    "cancelled": "parked-review",
    "archived": "finished",
    "parked": "parked-input",
    "finishing": "finishing",
    "cleaning": "finishing",
    "cleanup-failed": "crashed",
}
ACTIVE = frozenset({"leased", "running"})
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "parked"})


def relative_path(value: str | None) -> str:
    if not value:
        return ""
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) in ("", "."):
        raise RuntimeError(f"unsafe stored relative path: {value!r}")
    return str(path)


def host_path(root: str, relative: str) -> str:
    if not relative:
        return ""
    base = Path(root).resolve()
    result = (base / relative_path(relative)).resolve()
    if not result.is_relative_to(base):
        raise RuntimeError(f"stored path escapes workspace: {relative!r}")
    return str(result)


def container_path(relative: str) -> str:
    return f"/home/bw/{relative_path(relative)}"


class HostedFleet:
    def __init__(self, database: Path) -> None:
        self.database = database

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"file:{self.database}?mode=ro",
            uri=True,
            timeout=5,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def rows(self, owner: str) -> tuple[FleetRow, ...]:
        with closing(self.connect()) as connection:
            records = connection.execute(
                """
                SELECT j.job_id, j.state, j.failure_stage, j.lease_generation, j.envelope_json,
                       j.created_at, j.started_at, j.finished_at, j.last_heartbeat_at,
                       c.issue_iid, c.status AS conversation_status, c.session_refs_json,
                       p.project_key, p.project_path,
                       w.workspace_id, w.owner_username, w.container_name, w.host_root,
                       r.run_id AS runner_run_id, r.refs_json, r.result_json,
                       rr.compacted_at, rr.audit_json
                FROM jobs AS j
                JOIN conversations AS c ON c.conversation_key = j.conversation_key
                JOIN projects AS p ON p.project_key = c.project_key
                JOIN workspaces AS w ON w.workspace_id = j.workspace_id
                LEFT JOIN runs AS r ON r.job_id = j.job_id
                LEFT JOIN retention_records AS rr ON rr.run_id = r.run_id
                WHERE w.owner_username = ?
                ORDER BY COALESCE(j.started_at, j.created_at) DESC, j.job_id
                """,
                (owner,),
            ).fetchall()
        rows = []
        for record in records:
            envelope = json.loads(record["envelope_json"])
            refs = json.loads(record["refs_json"]) if record["refs_json"] else {}
            result = json.loads(record["result_json"]) if record["result_json"] else {}
            session_refs = (
                json.loads(record["session_refs_json"])
                if record["session_refs_json"]
                else {}
            )
            worktree_relpath = relative_path(refs.get("worktree_relpath"))
            run_dir_relpath = relative_path(refs.get("run_dir_relpath"))
            session_relpath = relative_path(
                result.get("session_file_relpath")
                or session_refs.get("session_file_relpath")
                or refs.get("session_file_relpath")
            )
            provider = str(envelope["provider"])
            model_id = str(envelope["model"])
            session = container_path(session_relpath) if session_relpath else ""
            session_lock = (
                str(
                    chat_lock_path(
                        session,
                        Path(str(record["host_root"])) / ".locks",
                    )
                )
                if session
                else ""
            )
            effort = str(envelope.get("effort") or "")
            model = ":".join(part for part in (provider, model_id, effort) if part)
            job_state = str(record["state"])
            conversation_state = str(record["conversation_status"])
            state = (
                job_state
                if job_state in ("queued", "leased", "running")
                else conversation_state
                if conversation_state in DERIVED
                else job_state
            )
            archived = state == "archived"
            capabilities = []
            if archived and record["audit_json"]:
                capabilities.append("audit")
            if not archived:
                has_prestart_error = (
                    job_state == "failed"
                    and not record["runner_run_id"]
                    and bool(record["failure_stage"])
                )
                if run_dir_relpath or has_prestart_error:
                    capabilities.append("logs")
                if worktree_relpath:
                    capabilities.extend(("path", "open"))
                if job_state == "running" and refs.get("tmux_session"):
                    capabilities.append("attach")
                if session_relpath and worktree_relpath and state in TERMINAL:
                    capabilities.append("resume")
            issue_key = f"{record['project_path']}#{record['issue_iid']}"
            journal = host_path(
                str(record["host_root"]),
                f"{run_dir_relpath}/run.jsonl" if run_dir_relpath else "",
            )
            rows.append(
                FleetRow(
                    identity=f"{record['project_key']}#{record['issue_iid']}",
                    key=issue_key,
                    surface="issue",
                    status=state,
                    derived=DERIVED[state],
                    model=model,
                    provider=provider,
                    model_id=model_id,
                    session=session,
                    tmux_alive=job_state == "running",
                    log=journal,
                    url=str(envelope["issue_url"]),
                    cwd=host_path(str(record["host_root"]), worktree_relpath),
                    started_at=float(record["started_at"])
                    if record["started_at"]
                    else None,
                    finished_at=float(record["finished_at"])
                    if record["finished_at"]
                    else None,
                    effort=effort,
                    run_id=str(refs.get("run_id") or record["runner_run_id"] or ""),
                    journal=journal,
                    job_id=str(record["job_id"]),
                    owner=str(record["owner_username"]),
                    workspace_id=str(record["workspace_id"]),
                    container=str(record["container_name"]),
                    host_path=host_path(str(record["host_root"]), worktree_relpath),
                    run_dir_relpath=run_dir_relpath,
                    worktree_relpath=worktree_relpath,
                    session_file_relpath=session_relpath,
                    session_lock_path=session_lock,
                    tmux_session=str(refs.get("tmux_session") or ""),
                    lease_generation=int(record["lease_generation"]),
                    remote=True,
                    capabilities=tuple(capabilities),
                    audit_available=bool(record["audit_json"]),
                    last_heartbeat_at=(
                        float(record["last_heartbeat_at"])
                        if record["last_heartbeat_at"]
                        else None
                    ),
                )
            )
        return tuple(rows)

    def match(self, owner: str, query: str) -> FleetRow:
        rows = self.rows(owner)
        needle = query.casefold()
        run_exact = [
            row
            for row in rows
            if needle in {row.job_id.casefold(), row.run_id.casefold()}
        ]
        if len(run_exact) == 1:
            return run_exact[0]
        issue_exact = [
            row
            for row in rows
            if needle
            in {row.identity.casefold(), row.key.casefold(), row.url.casefold()}
        ]
        if issue_exact:
            return issue_exact[0]
        digits = query[1:] if query.startswith("#") else query
        if digits.isdigit():
            iid_matches = [
                row
                for row in rows
                if row.key.casefold().endswith(f"#{digits}".casefold())
            ]
            if iid_matches:
                return iid_matches[0]
        partial = [
            row
            for row in rows
            if needle
            in " ".join(
                (row.job_id, row.run_id, row.identity, row.key, row.url)
            ).casefold()
        ]
        if len(partial) == 1:
            return partial[0]
        if not partial:
            raise RuntimeError(f"no hosted run matches {query!r} for @{owner}")
        raise RuntimeError(
            f"ambiguous hosted run {query!r}: "
            + ", ".join(row.job_id for row in partial[:10])
        )

    def audit(self, owner: str, query: str) -> dict[str, object]:
        row = self.match(owner, query)
        if not row.audit_available or not row.run_id:
            raise RuntimeError(f"{row.job_id or row.run_id} has no compact audit")
        with closing(self.connect()) as connection:
            record = connection.execute(
                """
                SELECT rr.audit_json
                FROM retention_records AS rr
                JOIN runs AS r ON r.run_id = rr.run_id
                JOIN jobs AS j ON j.job_id = r.job_id
                JOIN workspaces AS w ON w.workspace_id = j.workspace_id
                WHERE rr.run_id = ? AND w.owner_username = ?
                """,
                (row.run_id, owner),
            ).fetchone()
        if record is None or not record["audit_json"]:
            raise RuntimeError(f"{row.job_id or row.run_id} has no compact audit")
        return dict(json.loads(record["audit_json"]))

    def workspace(self, owner: str) -> sqlite3.Row:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT workspace_id, container_name, host_root, last_seen_at FROM workspaces "
                "WHERE owner_username = ? AND enabled = 1",
                (owner,),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"no enabled workspace for @{owner}")
        return row


def docker_exec(
    row: FleetRow,
    command: list[str],
    *,
    interactive: bool,
    workdir: str | None = None,
) -> int:
    argv = ["sudo", "docker", "exec"]
    if interactive:
        argv.append("-it")
    if workdir:
        argv.extend(["--workdir", workdir])
    argv.extend([row.container, *command])
    return subprocess.run(argv, check=False).returncode


def container_tmux_alive(row: FleetRow) -> bool:
    if not row.tmux_session:
        return False
    result = subprocess.run(
        [
            "sudo",
            "docker",
            "exec",
            row.container,
            "tmux",
            "list-panes",
            "-t",
            f"={row.tmux_session}",
            "-F",
            "#{pane_dead}",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return result.returncode == 0 and any(
        line.strip() == "0" for line in result.stdout.splitlines()
    )


def resume_lock(row: FleetRow):
    if not row.session_lock_path:
        raise RuntimeError(f"{row.job_id or row.run_id} has no session lock path")
    path = Path(row.session_lock_path)
    if not path.is_absolute():
        raise RuntimeError("stored session lock path is not absolute")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError("interactive resume is already active for this session")
    return lock


def encode_server_request(owner: str, command: str, args: tuple[str, ...]) -> str:
    payload = json.dumps(
        {"owner": owner, "command": command, "args": list(args)},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def valid_server_args(command: str, args: list[str]) -> bool:
    if command == "fleet":
        return args in ([], ["--json"])
    if command in ("inspect", "attach", "resume", "audit"):
        return len(args) == 1 and bool(args[0]) and not args[0].startswith("-")
    if command == "logs":
        if not args or not args[0] or args[0].startswith("-"):
            return False
        flags = args[1:]
        return len(flags) == len(set(flags)) and set(flags) <= {"--follow", "--session"}
    return command in ("doctor", "home") and not args


def server_request(token: str) -> int:
    try:
        padding = "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(token + padding))
        owner = payload["owner"]
        command = payload["command"]
        args = payload["args"]
    except (
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        binascii.Error,
    ) as error:
        raise RuntimeError("invalid encoded server request") from error
    if (
        not isinstance(owner, str)
        or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", owner) is None
        or not isinstance(command, str)
    ):
        raise RuntimeError("invalid encoded server request fields")
    if (
        not isinstance(args, list)
        or not all(isinstance(item, str) for item in args)
        or not valid_server_args(command, args)
    ):
        raise RuntimeError("invalid encoded server request arguments")
    return server_main(["--owner", owner, command, *args])


def server_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="bw --server-request")
    parser.add_argument("--owner", required=True)
    parser.add_argument(
        "--database",
        default=os.environ.get(
            "BW_CONTROLLER_DB",
            "/srv/eastwatch/controller/data/controller.db",
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    fleet = sub.add_parser("fleet")
    fleet.add_argument("--json", action="store_true")
    inspect = sub.add_parser("inspect")
    inspect.add_argument("query")
    logs = sub.add_parser("logs")
    logs.add_argument("query")
    logs.add_argument("--follow", action="store_true")
    logs.add_argument("--session", action="store_true")
    attach = sub.add_parser("attach")
    attach.add_argument("query")
    resume = sub.add_parser("resume")
    resume.add_argument("query")
    audit = sub.add_parser("audit")
    audit.add_argument("query")
    sub.add_parser("doctor")
    sub.add_parser("home")
    args = parser.parse_args(argv)
    hosted = HostedFleet(Path(args.database))

    if args.command == "fleet":
        rows = hosted.rows(args.owner)
        payload = [asdict(row) for row in rows]
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        else:
            for row in rows:
                line = f"{row.status:10} {row.model:28} {row.job_id or row.run_id} {row.key}"
                if (
                    row.status in ("leased", "running")
                    and row.last_heartbeat_at is not None
                ):
                    age = round(time.time() - row.last_heartbeat_at)
                    line += f" hb={age}s"
                print(line)
        return 0
    if args.command == "doctor":
        workspace = hosted.workspace(args.owner)
        return subprocess.run(
            [
                "sudo",
                "docker",
                "exec",
                str(workspace["container_name"]),
                "python",
                "-m",
                "eastwatch.runner.doctor",
            ],
            check=False,
        ).returncode
    if args.command == "home":
        workspace = hosted.workspace(args.owner)
        print(str(workspace["host_root"]))
        return 0

    row = hosted.match(args.owner, args.query)
    if args.command == "inspect":
        print(json.dumps(asdict(row), sort_keys=True))
        return 0
    if args.command == "audit":
        print(
            json.dumps(hosted.audit(args.owner, args.query), indent=2, sort_keys=True)
        )
        return 0
    if args.command == "logs":
        if "logs" not in row.capabilities:
            raise RuntimeError(f"{row.job_id or row.run_id} has no retained logs")
        if not row.run_dir_relpath:
            return docker_exec(
                row,
                ["cat", container_path(f"state/prestart/{row.job_id}/error.json")],
                interactive=False,
            )
        if args.session:
            # The session transcript is the actual conversation (what the
            # fleet TUI traces); run.jsonl is worker lifecycle only.
            if not row.session:
                raise RuntimeError(
                    f"{row.job_id or row.run_id} has no session transcript"
                )
            target = row.session
        else:
            target = container_path(f"{row.run_dir_relpath}/run.jsonl")
        if args.session:
            # Full transcript from the start; the fleet TUI dedupes replayed
            # backlog lines rather than dropping history via -n 200.
            command = (
                ["tail", "-F", "-n", "+1", target]
                if args.follow
                else ["tail", "-n", "+1", target]
            )
        else:
            command = (
                ["tail", "-F", "-n", "200", target]
                if args.follow
                else ["tail", "-n", "200", target]
            )
        return docker_exec(row, command, interactive=args.follow)
    if args.command == "attach":
        if row.status != "running" or not row.tmux_session:
            raise RuntimeError(f"{row.job_id or row.run_id} is not an active tmux run")
        return docker_exec(
            row,
            ["tmux", "attach-session", "-r", "-t", f"={row.tmux_session}"],
            interactive=True,
        )
    if args.command == "resume":
        if (
            row.status not in TERMINAL
            or not row.session_file_relpath
            or not row.worktree_relpath
        ):
            raise RuntimeError(
                f"{row.job_id or row.run_id} is not ready for post-run resume"
            )
        if container_tmux_alive(row):
            raise RuntimeError(
                f"{row.job_id or row.run_id} still has a live worker pane"
            )
        lock = resume_lock(row)
        try:
            return docker_exec(
                row,
                [
                    "pi",
                    "--session",
                    container_path(row.session_file_relpath),
                ],
                interactive=True,
                workdir=container_path(row.worktree_relpath),
            )
        finally:
            lock.close()
    raise RuntimeError(f"unsupported server command: {args.command}")
