"""Dismiss retained terminal fleet runs while preserving their artifacts."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from eastwatch.env import getenv
from eastwatch.fleet.status import is_terminal_conversation


LOCK_RETRY_ATTEMPTS = 5
LOCK_RETRY_DELAY_S = 0.2


@dataclass(frozen=True, slots=True)
class BulkDismissResult:
    dismissed: tuple[tuple[str, str], ...]
    failures: tuple[tuple[str, str, str], ...]


def state_dir() -> Path:
    raw = getenv("EASTWATCH_STATE_DIR")
    return Path(raw).expanduser() if raw else Path.home() / ".local/state/eastwatch"


def tmux_bin() -> str | None:
    found = shutil.which("tmux")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux"):
        if Path(candidate).exists():
            return candidate
    return None


def kill_session(name: str | None) -> None:
    tmux = tmux_bin()
    if not tmux or not name:
        return
    subprocess.run(
        [tmux, "kill-session", "-t", f"={name}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
        check=False,
    )


def save_state(path: Path, state: dict) -> None:
    backup = path.with_suffix(path.suffix + ".bak")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    if path.exists():
        shutil.copy2(path, backup)
    temporary.replace(path)


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not read watcher state: {exc}") from exc


def dismiss(identity: str, run_id: str, *, root: Path | None = None) -> None:
    root = root or state_dir()
    root.mkdir(parents=True, exist_ok=True)
    lock = (root / "cycle.lock").open("a+")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("watcher cycle is active; try x again shortly") from None

        state_path = root / "state.json"
        state = load_state(state_path)

        try:
            project_key, conversation_key = identity.rsplit(":", 1)
            conversation = state["projects"][project_key]["conversations"][conversation_key]
        except (KeyError, ValueError):
            raise LookupError(f"finished run no longer exists: {identity}") from None

        archived = conversation.get("last_run")
        if conversation.get("current_run") or conversation.get("status") != "done" or not archived:
            raise RuntimeError("selected conversation is no longer finished")
        if not run_id or archived.get("run_id") != run_id:
            raise RuntimeError("selected finished run was replaced; refresh and try again")

        kill_session(archived.get("tmux_session"))
        conversation["last_run"] = None
        save_state(state_path, state)
    finally:
        lock.close()


def acquire_state_lock(root: Path):
    lock = (root / "cycle.lock").open("a+")
    for attempt in range(LOCK_RETRY_ATTEMPTS):
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock
        except BlockingIOError:
            if attempt == LOCK_RETRY_ATTEMPTS - 1:
                lock.close()
                raise RuntimeError(
                    f"watcher cycle remained active after {LOCK_RETRY_ATTEMPTS} attempts"
                ) from None
            time.sleep(LOCK_RETRY_DELAY_S)
    raise AssertionError("unreachable")


def dismiss_all_finished(*, root: Path | None = None) -> BulkDismissResult:
    root = root or state_dir()
    root.mkdir(parents=True, exist_ok=True)
    lock = acquire_state_lock(root)
    try:
        state_path = root / "state.json"
        state = load_state(state_path)
        dismissed: list[tuple[str, str]] = []
        failures: list[tuple[str, str, str]] = []

        for project_key, project in state.get("projects", {}).items():
            for conversation_key, conversation in project.get("conversations", {}).items():
                if not is_terminal_conversation(conversation):
                    continue
                archived = conversation["last_run"]
                identity = f"{project_key}:{conversation_key}"
                run_id = str(archived.get("run_id") or "")
                try:
                    kill_session(archived.get("tmux_session"))
                except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                    failures.append((identity, run_id, str(exc)))
                    continue
                conversation["last_run"] = None
                dismissed.append((identity, run_id))

        if dismissed:
            save_state(state_path, state)
        return BulkDismissResult(tuple(dismissed), tuple(failures))
    finally:
        lock.close()


def bulk_summary(dismissed: int, failed: int) -> str:
    row = "row" if dismissed == 1 else "rows"
    summary = f"Dismissed {dismissed} terminal fleet {row}"
    if failed:
        summary += f"; {failed} failed"
    return summary


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--all-finished"]:
        try:
            result = dismiss_all_finished()
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            print(f"fleet-dismiss: {exc}", file=sys.stderr)
            return 1
        for identity, run_id in result.dismissed:
            print(f"Dismissed {identity} {run_id}")
        for identity, run_id, error in result.failures:
            print(f"fleet-dismiss: {identity} {run_id}: {error}", file=sys.stderr)
        print(bulk_summary(len(result.dismissed), len(result.failures)))
        return 1 if result.failures else 0

    if len(args) != 2:
        print("usage: fleet-dismiss [--all-finished | IDENTITY RUN_ID]", file=sys.stderr)
        return 2
    try:
        dismiss(args[0], args[1])
    except (LookupError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"fleet-dismiss: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
