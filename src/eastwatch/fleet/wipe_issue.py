"""Permanently remove one inactive issue from local watcher state."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence, TextIO

from eastwatch.env import getenv


class WipeError(RuntimeError):
    """The requested issue cannot be safely wiped."""


def load_json(path: Path, *, required: bool) -> dict | None:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        if required:
            raise WipeError(f"watcher state not found: {path}") from None
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise WipeError(f"could not read {path}: {exc}") from exc


def atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def project_path(project_key: str) -> str:
    return project_key.split("/", 1)[1] if "/" in project_key else project_key


def matching_projects(projects: dict, slug: str) -> list[str]:
    return [
        key
        for key in projects
        if slug in {key, project_path(key), project_path(key).rsplit("/", 1)[-1]}
    ]


def remove_issue_memory(state: dict, project_key: str, issue: str) -> tuple[dict | None, int]:
    project = state.get("projects", {}).get(project_key)
    if not isinstance(project, dict):
        return None, 0
    conversation = project.setdefault("conversations", {}).pop(issue, None)
    mr_index = project.get("mr_index") or {}
    removed_mappings = 0
    for mr_iid, mapping in list(mr_index.items()):
        if not isinstance(mapping, dict):
            continue
        if str(mapping.get("issue_iid")) == issue or str(mapping.get("conversation_key")) == issue:
            del mr_index[mr_iid]
            removed_mappings += 1
    return conversation, removed_mappings


def checked_artifact_dir(root: Path, candidate: Path) -> Path:
    convos_root = (root / "convos").resolve()
    candidate = candidate.expanduser().resolve()
    try:
        candidate.relative_to(convos_root)
    except ValueError:
        raise WipeError(
            f"refusing to delete artifact path outside {convos_root}: {candidate}"
        ) from None
    if candidate == convos_root:
        raise WipeError(f"refusing to delete artifact root: {candidate}")
    return candidate


def kill_retained_tmux(conversation: dict | None) -> None:
    if not conversation:
        return
    names = {
        str(run.get("tmux_session"))
        for run in (conversation.get("last_run"),)
        if isinstance(run, dict) and run.get("tmux_session")
    }
    tmux = shutil.which("tmux")
    if not tmux:
        return
    for name in names:
        subprocess.run(
            [tmux, "kill-session", "-t", f"={name}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )


def wipe_issue(root: Path, slug: str, issue: str) -> tuple[str, dict | None, int, Path] | None:
    state_path = root / "state.json"
    backup_path = root / "state.json.bak"
    lock_path = root / "cycle.lock"
    root.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WipeError("watcher cycle is active; retry in a few seconds") from None

        state = load_json(state_path, required=True)
        assert state is not None
        matches = matching_projects(state.get("projects", {}), slug)
        if not matches:
            available = ", ".join(sorted(state.get("projects", {}))) or "none"
            raise WipeError(
                f"project slug {slug!r} not found; available projects: {available}"
            )
        if len(matches) > 1:
            raise WipeError(
                f"project slug {slug!r} is ambiguous; use one of: {', '.join(sorted(matches))}"
            )
        project_key = matches[0]

        project = state["projects"][project_key]
        current_conversation = (project.get("conversations") or {}).get(issue)
        if isinstance(current_conversation, dict) and current_conversation.get("current_run"):
            raise WipeError("issue has an active run; stop it in Fleet before wiping")

        backup = load_json(backup_path, required=False)
        backup_project = (backup or {}).get("projects", {}).get(project_key, {})
        backup_conversation = (backup_project.get("conversations") or {}).get(issue)
        artifact_conversation = current_conversation or backup_conversation
        default_artifact_dir = root / "convos" / f"{project_path(project_key).replace('/', '-')}-{issue}"
        configured_artifact_dir = (
            Path(artifact_conversation["session_dir"])
            if isinstance(artifact_conversation, dict) and artifact_conversation.get("session_dir")
            else default_artifact_dir
        )
        artifact_dir = checked_artifact_dir(root, configured_artifact_dir)

        removed_conversation, removed_mappings = remove_issue_memory(state, project_key, issue)
        removed_backup_conversation = None
        if backup is not None:
            removed_backup_conversation, _ = remove_issue_memory(backup, project_key, issue)

        if (
            removed_conversation is None
            and removed_backup_conversation is None
            and not artifact_dir.exists()
        ):
            return None

        kill_retained_tmux(removed_conversation or removed_backup_conversation)
        atomic_write_json(state_path, state)
        if backup is not None:
            atomic_write_json(backup_path, backup)
        else:
            shutil.copy2(state_path, backup_path)
        if artifact_dir.exists():
            shutil.rmtree(artifact_dir)

    return project_key, removed_conversation, removed_mappings, artifact_dir


def usage(stream: TextIO) -> None:
    print("usage: fleet-wipe-issue [--yes] PROJECT_SLUG ISSUE_NUMBER", file=stream)
    print("", file=stream)
    print(
        "PROJECT_SLUG may be a repository basename, namespace/project, or full watcher project key.",
        file=stream,
    )
    print("Without --yes, the command asks for confirmation.", file=stream)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    assume_yes = bool(args and args[0] == "--yes")
    if assume_yes:
        args.pop(0)
    if len(args) != 2:
        usage(sys.stderr)
        return 2
    slug, issue = args
    if not issue.isdigit() or int(issue) < 1:
        print("fleet-wipe-issue: issue number must be a positive integer", file=sys.stderr)
        return 2

    confirmation = f"{slug}#{issue}"
    if not assume_yes:
        if not sys.stdin.isatty():
            print(
                "fleet-wipe-issue: confirmation requires a terminal; pass --yes for non-interactive use",
                file=sys.stderr,
            )
            return 2
        print(
            f"Permanently wipe local watcher state and artifacts for {confirmation}?",
            file=sys.stderr,
        )
        print(f"Type {confirmation} to continue: ", file=sys.stderr, end="", flush=True)
        if sys.stdin.readline().rstrip("\n") != confirmation:
            print("fleet-wipe-issue: cancelled", file=sys.stderr)
            return 1

    root = Path(getenv("EASTWATCH_STATE_DIR") or "~/.local/state/eastwatch").expanduser()
    try:
        result = wipe_issue(root, slug, issue)
    except WipeError as exc:
        print(f"fleet-wipe-issue: {exc}", file=sys.stderr)
        return 1
    if result is None:
        print(f"Nothing to wipe for {slug} issue {issue}")
        return 0

    project_key, removed_conversation, removed_mappings, artifact_dir = result
    print(f"Wiped {project_key} issue {issue}")
    state_status = "removed" if removed_conversation is not None else "already absent"
    print(f"  conversation state: {state_status}")
    print(f"  MR mappings: {removed_mappings} removed")
    print(f"  artifacts: {artifact_dir}")
    print("Shared watcher logs and remote GitLab data were not modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
