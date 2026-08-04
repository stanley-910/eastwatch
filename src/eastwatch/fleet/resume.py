"""Open an interactive chat on a resumable eastwatch conversation."""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Sequence, TextIO

from eastwatch.fleet.core import (
    Command,
    FleetRow,
    chat_lock_path,
    chat_name,
    command_argv,
    interactive_command,
    parse_rows,
    resume_eligible,
    tmux_chat_command,
)
from eastwatch.paths import entrypoint_command

FLEET_STATUS = entrypoint_command("eastwatch.fleet.status", "fleet-status")


def load_rows(fleet_status: Command = FLEET_STATUS) -> tuple[FleetRow, ...]:
    try:
        result = subprocess.run(
            [*command_argv(fleet_status), "--json"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"fleet-status failed: {exc}") from exc
    if result.returncode:
        detail = result.stderr.strip()
        raise RuntimeError(
            f"fleet-status exited {result.returncode}"
            + (f": {detail}" if detail else "")
        )
    try:
        return parse_rows(json.loads(result.stdout))
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"fleet-status emitted invalid JSON: {exc}") from exc


def resumable_rows(rows: Sequence[FleetRow]) -> tuple[FleetRow, ...]:
    return tuple(row for row in rows if resume_eligible(row))


def match_row(rows: Sequence[FleetRow], query: str) -> FleetRow:
    needle = query.casefold()
    exact = [
        row
        for row in rows
        if needle in {row.identity.casefold(), row.key.casefold(), row.url.casefold()}
    ]
    if len(exact) == 1:
        return exact[0]
    matches = [
        row
        for row in rows
        if needle in " ".join((row.identity, row.key, row.url)).casefold()
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        keys = ", ".join(row.key for row in matches[:4])
        raise LookupError(f"ambiguous resumable row matching {query!r}: {keys}")
    raise LookupError(f"no resumable row matching: {query}")


def choose_row(
    rows: Sequence[FleetRow],
    *,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
) -> FleetRow:
    fzf = shutil.which("fzf")
    if fzf:
        lines = [
            f"{index}\t{row.derived}\t{row.model}\t{row.key}"
            for index, row in enumerate(rows)
        ]
        result = subprocess.run(
            [fzf, "--prompt=resume> ", "--header=resumable sessions", "--with-nth=2.."],
            input="\n".join(lines) + "\n",
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise KeyboardInterrupt
        try:
            return rows[int(result.stdout.split("\t", 1)[0])]
        except (ValueError, IndexError) as exc:
            raise RuntimeError("fzf returned an invalid selection") from exc

    for index, row in enumerate(rows, start=1):
        print(f"{index:2d}) {row.derived:<13} {row.model:<24} {row.key}", file=stdout)
    print("pick #: ", end="", file=stdout, flush=True)
    choice = stdin.readline()
    if not choice:
        raise KeyboardInterrupt
    try:
        selected = int(choice.strip())
        if selected < 1 or selected > len(rows):
            raise ValueError
        return rows[selected - 1]
    except ValueError as exc:
        raise LookupError("no such row") from exc


def tmux_window_exists(name: str) -> bool:
    if not os.environ.get("TMUX"):
        return False
    tmux = shutil.which("tmux")
    if not tmux:
        return False
    result = subprocess.run(
        [tmux, "list-windows", "-F", "#{window_name}"],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    return result.returncode == 0 and name in result.stdout.splitlines()


def acquire_chat_lock(row: FleetRow):
    path = chat_lock_path(row.session)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError(f"interactive chat already active for {row.key}") from None
    lock.write(f"{os.getpid()} {row.identity}\n")
    lock.flush()
    os.set_inheritable(lock.fileno(), True)
    return lock


def chat_lock_active(row: FleetRow) -> bool:
    try:
        lock = acquire_chat_lock(row)
    except RuntimeError:
        return True
    lock.close()
    return False


def row_payload(row: FleetRow) -> str:
    return base64.urlsafe_b64encode(json.dumps(asdict(row)).encode()).decode()


def row_from_payload(payload: str) -> FleetRow:
    try:
        raw = json.loads(base64.urlsafe_b64decode(payload.encode()))
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("invalid internal row payload") from exc
    return FleetRow.from_mapping(raw)


def point_current_tmux_pane(cwd: str) -> None:
    pane = os.environ.get("TMUX_PANE")
    tmux = shutil.which("tmux")
    if not pane or not tmux:
        return
    subprocess.run(
        [tmux, "set-option", "-p", "-t", pane, "@agent_worktree", cwd],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )


def hold_lock_and_exec(row: FleetRow) -> int:
    lock = acquire_chat_lock(row)
    command = interactive_command(row)
    cwd = row.cwd or str(Path.cwd())
    try:
        point_current_tmux_pane(cwd)
        os.chdir(cwd)
        os.execvp(command[0], command)
    finally:
        lock.close()
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fleet-resume",
        description="Open an interactive chat on a resumable fleet conversation.",
    )
    parser.add_argument(
        "-p", "--pane", action="store_true", help="split current tmux window"
    )
    parser.add_argument(
        "-n", "--dry-run", action="store_true", help="print command only"
    )
    parser.add_argument("query", nargs="?", help="key, identity, or URL fragment")
    parser.add_argument("--hold-lock", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.hold_lock:
        try:
            return hold_lock_and_exec(row_from_payload(args.hold_lock))
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"fleet-resume: {exc}", file=sys.stderr)
            return 1

    try:
        candidates = resumable_rows(load_rows())
        if not candidates:
            raise LookupError("no resumable sessions")
        selected = (
            match_row(candidates, args.query) if args.query else choose_row(candidates)
        )
        command = interactive_command(selected)
    except KeyboardInterrupt:
        return 130
    except (LookupError, RuntimeError, ValueError) as exc:
        print(f"fleet-resume: {exc}", file=sys.stderr)
        return 1

    mode = "pane" if args.pane else "window"
    if args.dry_run:
        import shlex

        print(f"[{mode}] {chat_name(selected)}: {shlex.join(command)}")
        return 0

    if not selected.cwd or not Path(selected.cwd).is_dir():
        print(
            f"fleet-resume: repository cwd is unavailable: {selected.cwd or '(empty)'}",
            file=sys.stderr,
        )
        return 1
    if chat_lock_active(selected):
        print(
            f"fleet-resume: interactive chat already active for {selected.key}",
            file=sys.stderr,
        )
        return 1
    if tmux_window_exists(chat_name(selected)):
        print(
            f"fleet-resume: chat window already active for {selected.key}",
            file=sys.stderr,
        )
        return 1

    print(
        "fleet-resume: close chat before re-queueing this conversation; "
        "two clients cannot safely write one session",
        file=sys.stderr,
    )
    if os.environ.get("TMUX"):
        runner = entrypoint_command("eastwatch.fleet.resume", "fleet-resume") + (
            "--hold-lock",
            row_payload(selected),
        )
        result = subprocess.run(
            tmux_chat_command(selected, pane=args.pane, runner=runner),
            check=False,
        )
        return result.returncode

    try:
        return hold_lock_and_exec(selected)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"fleet-resume: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
