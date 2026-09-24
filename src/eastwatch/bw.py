from __future__ import annotations

import argparse
import contextlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from eastwatch.fleet.core import FleetRow
from eastwatch.remote import encode_server_request, server_request


@dataclass(frozen=True, slots=True)
class RemoteConfig:
    ssh_target: str
    ssh_alias: str
    owner: str
    server_command: str
    editor: str


def load_config(path: Path | None = None) -> RemoteConfig:
    config_path = path or Path.home() / ".config" / "eastwatch" / "remote.yaml"
    payload = yaml.safe_load(config_path.read_text())
    required = ("ssh_target", "ssh_alias", "owner", "server_command", "editor")
    missing = [
        field
        for field in required
        if not isinstance(payload.get(field), str) or not payload[field]
    ]
    if missing:
        raise RuntimeError(f"invalid remote config; missing: {', '.join(missing)}")
    if payload["ssh_target"].startswith("-"):
        raise RuntimeError("ssh_target cannot be an SSH option")
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", payload["server_command"]):
        raise RuntimeError("server_command must be a shell-safe absolute path")
    return RemoteConfig(**{field: payload[field] for field in required})


# Multiplex repeat ssh calls over one master connection: the fleet TUI polls
# `bw fleet` every few seconds and a fresh handshake per poll dominates
# latency. %C hashes host+port+user into a short socket name.
#
# Only quick capture verbs share the master. Interactive sessions (resume,
# attach) and long-lived followers (logs --follow) each get a dedicated
# connection: they outlive their callers, get killed uncleanly, and a shared
# master wedged by one of them silently hangs every other bw call.
SSH_MUX_OPTIONS = (
    "-o",
    "ControlMaster=auto",
    "-o",
    "ControlPath=~/.ssh/bw-mux-%C",
    "-o",
    "ControlPersist=600",
)
SSH_DIRECT_OPTIONS = ("-o", "ControlMaster=no", "-S", "none")


def remote_argv(
    config: RemoteConfig, command: str, *args: str, mux: bool = True
) -> list[str]:
    request = encode_server_request(config.owner, command, args)
    return [
        "ssh",
        *(SSH_MUX_OPTIONS if mux else SSH_DIRECT_OPTIONS),
        config.ssh_target,
        config.server_command,
        "--server-request",
        request,
    ]


def reset_mux(config: RemoteConfig) -> None:
    """Drop a wedged control master so the next call gets a fresh one."""
    subprocess.run(
        ["ssh", *SSH_MUX_OPTIONS, "-O", "exit", config.ssh_target],
        capture_output=True,
        timeout=5,
        check=False,
    )
    # A SIGKILLed or unresponsive master ignores -O exit and leaves its
    # socket behind; every later mux attempt then hangs on it. Unlink the
    # sockets outright — an orphaned live master times out on its own.
    for socket in Path.home().glob(".ssh/bw-mux-*"):
        with contextlib.suppress(OSError):
            socket.unlink()


def capture(config: RemoteConfig, command: str, *args: str) -> str:
    try:
        process = subprocess.run(
            remote_argv(config, command, *args),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # A healthy round trip is under a second; a 15s stall means the shared
        # control master is wedged (killed follower, slept laptop). Retire it
        # and retry once on a dedicated connection.
        try:
            reset_mux(config)
        except subprocess.TimeoutExpired:
            pass
        process = subprocess.run(
            remote_argv(config, command, *args, mux=False),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    if process.returncode:
        raise RuntimeError(
            process.stderr.strip() or f"remote command exited {process.returncode}"
        )
    return process.stdout


def inspect(config: RemoteConfig, query: str) -> FleetRow:
    return FleetRow.from_mapping(json.loads(capture(config, "inspect", query)))


def copy_text(text: str) -> None:
    pbcopy = shutil.which("pbcopy")
    if pbcopy:
        subprocess.run([pbcopy], input=text, text=True, check=True)
    else:
        print(text)


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if len(args_list) == 2 and args_list[0] == "--server-request":
        return server_request(args_list[1])
    parser = argparse.ArgumentParser(prog="bw")
    sub = parser.add_subparsers(dest="command", required=True)
    fleet = sub.add_parser("fleet")
    fleet.add_argument("--json", action="store_true")
    path = sub.add_parser("path")
    path.add_argument("query")
    path.add_argument("--copy", action="store_true")
    logs = sub.add_parser("logs")
    logs.add_argument("query")
    logs.add_argument("--follow", action="store_true")
    logs.add_argument("--session", action="store_true")
    attach = sub.add_parser("attach")
    attach.add_argument("query")
    open_command = sub.add_parser("open")
    open_command.add_argument("query")
    resume = sub.add_parser("resume")
    resume.add_argument("query")
    audit = sub.add_parser("audit")
    audit.add_argument("query")
    sub.add_parser("doctor")
    sub.add_parser("home")
    args = parser.parse_args(args_list)
    config = load_config()

    if args.command == "fleet":
        extra = ["--json"] if args.json else []
        print(capture(config, "fleet", *extra), end="")
        return 0
    if args.command == "path":
        row = inspect(config, args.query)
        if not row.host_path:
            raise RuntimeError(f"{row.job_id or row.run_id} has no retained worktree")
        copy_text(row.host_path) if args.copy else print(row.host_path)
        return 0
    if args.command == "open":
        row = inspect(config, args.query)
        if not row.host_path:
            raise RuntimeError(f"{row.job_id or row.run_id} has no retained worktree")
        return subprocess.run(
            [
                config.editor,
                "--remote",
                f"ssh-remote+{config.ssh_alias}",
                row.host_path,
            ],
            check=False,
        ).returncode
    if args.command == "audit":
        print(capture(config, "audit", args.query), end="")
        return 0
    if args.command == "home":
        print(capture(config, "home"), end="")
        return 0
    if args.command in ("logs", "attach", "resume", "doctor"):
        extra = []
        if args.command != "doctor":
            extra.append(args.query)
        if args.command == "logs" and args.follow:
            extra.append("--follow")
        if args.command == "logs" and args.session:
            extra.append("--session")
        # -tt forces remote pty allocation even without a local tty (e.g. the
        # fleet TUI following logs through a pipe); the server-side
        # `docker exec -it` refuses to run without one.
        tty_flag = "-t" if sys.stdin.isatty() else "-tt"
        # mux=False: these sessions are long-lived and often killed uncleanly;
        # routed through the shared master they can wedge it and hang every
        # other bw call (including an interactive resume with no timeout).
        return subprocess.run(
            [
                "ssh",
                tty_flag,
                *remote_argv(config, args.command, *extra, mux=False)[1:],
            ],
            check=False,
        ).returncode
    raise RuntimeError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
