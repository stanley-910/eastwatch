from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any

from eastwatch import watcher
from eastwatch.runner.client import Lease, LeaseRejected, RunnerApiError, RunnerClient

logger = logging.getLogger(__name__)

CONVERSATION_META_NAME = "conversation.json"


class WorkspaceError(RuntimeError):
    pass


def safe_component(value: str) -> str:
    if (
        not value
        or value in (".", "..")
        or any(character in value for character in ("/", "\\", "\x00"))
    ):
        raise WorkspaceError(f"unsafe workspace component: {value!r}")
    return value


def canonical_checkout(
    root: Path,
    host: str,
    project_path: str,
    *,
    require_exists: bool = True,
) -> Path:
    relative = PurePosixPath(project_path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise WorkspaceError(f"unsafe project path: {project_path!r}")
    path = root / "repos" / safe_component(host)
    for part in relative.parts:
        path /= safe_component(part)
    resolved_root = root.resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(resolved_root):
        raise WorkspaceError(f"checkout escapes workspace root: {resolved}")
    if require_exists and not resolved.is_dir():
        raise WorkspaceError(f"checkout is missing: {resolved}")
    return resolved


def relative_to_root(path: Path, root: Path) -> str:
    resolved = path.resolve()
    workspace = root.resolve()
    if not resolved.is_relative_to(workspace):
        raise WorkspaceError(f"artifact escapes workspace root: {resolved}")
    return resolved.relative_to(workspace).as_posix()


def load_conversation_meta(session_dir: Path) -> dict[str, Any]:
    try:
        payload = json.loads((session_dir / CONVERSATION_META_NAME).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def persist_conversation_meta(session_dir: Path, meta: dict[str, Any]) -> None:
    watcher.atomic_write_json(session_dir / CONVERSATION_META_NAME, meta)


def session_mode(conv: dict[str, Any]) -> bool:
    """True when the conversation has no session handle and should launch fresh."""
    return not (conv.get("session_id") or conv.get("session_file"))


def resume_trigger_messages(
    envelope: dict[str, Any],
    messages: list[str],
    is_new: bool,
) -> list[str]:
    """A label re-fire on an existing conversation carries no comment text;
    without a synthesized message the agent resumes with a bare STATUS reminder."""
    trigger_kind = str(envelope.get("trigger_kind") or "")
    if not is_new and not messages and trigger_kind.startswith("agent::"):
        return [
            f"The issue has been labeled `{trigger_kind}` again. "
            "Pick the work back up per the issue."
        ]
    return messages


RUNNER_CONTROL_ENV = frozenset(
    {
        "BW_CONTROLLER_URL",
        "BW_RUNNER_TOKEN",
        "BW_RUNNER_CAPACITY",
        "BW_HEARTBEAT_SECONDS",
        "BW_PROJECTS_JSON",
        "BW_REQUIRED_MODEL",
    }
)


def hosted_worker_env(conv: dict[str, Any]) -> dict[str, str]:
    env = watcher.worker_env(conv)
    for name in RUNNER_CONTROL_ENV:
        env.pop(name, None)
    return env


def prepare_workspace(conv: dict[str, Any], root: Path) -> None:
    command = watcher.forge_board_path()
    if not command.is_file():
        raise WorkspaceError(f"Forge command is unavailable: {command}")
    mode = "work" if conv["kind"] == "agent::ready" else "research"
    try:
        process = subprocess.run(
            [str(command), "start", str(conv["issue_iid"]), mode, "--json"],
            cwd=conv["checkout"],
            env=hosted_worker_env(conv),
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorkspaceError(f"Forge start failed: {error}") from error
    if process.returncode != 0:
        raise WorkspaceError(
            f"Forge start failed: {watcher.command_tail(process.stdout, process.stderr)}"
        )
    try:
        payload = json.loads(process.stdout.strip().splitlines()[-1])
        worktree = Path(payload["worktree"]).expanduser()
        branch = str(payload["branch"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise WorkspaceError(f"Forge start returned invalid JSON: {error}") from error
    relative_to_root(worktree, root)
    if not worktree.is_dir():
        raise WorkspaceError(f"Forge worktree does not exist: {worktree}")
    conv["cwd"] = str(worktree.resolve())
    conv["worktree"] = str(worktree.resolve())
    conv["worktree_branch"] = branch
    conv["workspace_prepared"] = True


class WorkspaceExecutor:
    def __init__(self, root: Path, client: RunnerClient) -> None:
        self.root = root.resolve()
        self.client = client

    def conversation(self, envelope: dict[str, Any]) -> dict[str, Any]:
        checkout = canonical_checkout(
            self.root,
            str(envelope["host"]),
            str(envelope["project_path"]),
        )
        issue_iid = str(int(envelope["issue_iid"]))
        slug = "-".join(
            safe_component(part)
            for part in PurePosixPath(str(envelope["project_path"])).parts
        )
        session_dir = self.root / "state" / "conversations" / f"{slug}-{issue_iid}"
        relative_to_root(session_dir, self.root)
        session_dir.mkdir(parents=True, exist_ok=True)
        messages = [str(message) for message in envelope.get("messages") or ()]
        context = dict(envelope.get("context") or {})

        meta = load_conversation_meta(session_dir)
        session_id: str | None = None
        session_file: str | None = None
        session_file_relpath: str | None = None
        relpath = meta.get("session_file_relpath")
        if meta.get("provider") != str(envelope["provider"]):
            # Provider-specific session handles are not portable; a provider
            # switch always starts a fresh session (mirrors watcher.apply_hint).
            reason = "provider changed" if meta else "no persisted session meta"
        elif not isinstance(relpath, str) or not relpath:
            reason = "persisted meta missing session_file_relpath"
        else:
            resolved = self.root / relpath
            try:
                relative_to_root(resolved, self.root)
            except WorkspaceError as error:
                reason = f"persisted session_file_relpath is unsafe: {error}"
            else:
                if not resolved.is_file():
                    reason = f"persisted session file is missing: {resolved}"
                else:
                    reason = None
                    session_id = meta.get("session_id")
                    session_file = str(resolved)
                    session_file_relpath = relpath
        if reason is not None:
            logger.info(
                "conversation %s: not adopting persisted session (%s)",
                session_dir,
                reason,
            )

        return {
            "provider": str(envelope["provider"]),
            "model": str(envelope["model"]),
            "effort": envelope.get("effort"),
            "session_id": session_id,
            "session_file": session_file,
            "session_file_relpath": session_file_relpath,
            "cwd": str(checkout),
            "session_dir": str(session_dir),
            "host": str(envelope["host"]),
            "project_path": str(envelope["project_path"]),
            "checkout": str(checkout),
            "briefing": context.get("worker_briefing"),
            "thread_context": str(context.get("thread_context") or ""),
            "jira_context": str(context.get("jira_context") or ""),
            "status": "new",
            "kind": str(envelope["trigger_kind"]),
            "anchor": "issue",
            "reply_target": dict(envelope["reply_target"]),
            "issue_iid": issue_iid,
            "issue_title": str(context.get("issue_title") or f"Issue #{issue_iid}"),
            "issue_url": str(envelope["issue_url"]),
            "issue_desc": str(context.get("issue_description") or "")[:6000],
            "pending": messages,
            "mr_iids": [],
            "parked_note_id": None,
            "last_note_id": None,
            "last_reply_body_hash": None,
        }

    def prestart_failure(self, lease: Lease, kind: str, message: str) -> None:
        job_id = str(lease.envelope["job_id"])
        error = {
            "ok": False,
            "kind": kind,
            "message": message[-1000:],
            "completed_at": time.time(),
        }
        failure_dir = self.root / "state" / "prestart" / safe_component(job_id)
        relative_to_root(failure_dir, self.root)
        failure_dir.mkdir(parents=True, exist_ok=True)
        watcher.atomic_write_json(failure_dir / "error.json", error)
        self.client.fail_before_start(job_id, lease.lease_generation, error)

    def upload_terminal(
        self,
        job_id: str,
        generation: int,
        state: str,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        delay = 1.0
        while True:
            try:
                self.client.complete(
                    job_id,
                    generation,
                    state,
                    result=result,
                    error=error,
                )
                return
            except LeaseRejected:
                return
            except RunnerApiError:
                time.sleep(delay)
                delay = min(30.0, delay * 2)

    @staticmethod
    def read_terminal_artifact(
        path: Path, request: dict[str, Any], kind: str
    ) -> dict[str, Any] | None:
        try:
            payload = watcher.read_json_file(path)
            if not isinstance(payload, dict):
                raise TypeError(f"expected JSON object, got {type(payload).__name__}")
            return payload
        except (OSError, json.JSONDecodeError, TypeError) as error:
            if kind == "result" and path.exists():
                try:
                    path.replace(path.with_name(f"{path.name}.corrupt"))
                except OSError:
                    pass
            watcher.write_worker_error(
                request,
                f"corrupt-{kind}",
                f"cannot read {path.name}: {error}",
            )
            return None

    def execute(self, lease: Lease, cancelled: threading.Event | None = None) -> None:
        envelope = lease.envelope
        job_id = str(envelope["job_id"])
        if cancelled is not None and cancelled.is_set():
            return
        try:
            conv = self.conversation(envelope)
            prepare_workspace(conv, self.root)
            if cancelled is not None and cancelled.is_set():
                return
            worktree = Path(conv["cwd"])
            relative_to_root(worktree, self.root)
            messages = [str(message) for message in envelope.get("messages") or ()]
            is_new = session_mode(conv)
            messages = resume_trigger_messages(envelope, messages, is_new)
            prompt = (
                watcher.build_launch_prompt(conv, messages)
                if is_new
                else watcher.build_resume_message(messages)
            )
            run_id = watcher.utc_run_id()
            run_dir = Path(conv["session_dir"]) / "runs" / safe_component(run_id)
            relative_to_root(run_dir, self.root)
            run_dir.mkdir(parents=True, exist_ok=False)
            request = watcher.make_run_request(
                conv, messages, is_new, prompt, run_dir, run_id
            )
            request.update(
                schema_version=2,
                job_id=job_id,
                workspace_owner=str(envelope["owner_username"]),
                lease_generation=lease.lease_generation,
            )
            watcher.atomic_write_json(request["request_path"], request)
            tmux_session = f"bw-{run_id}"
            worker_argv = [
                sys.executable,
                "-m",
                "eastwatch.watcher",
                "--worker",
                str(request["request_path"]),
            ]
        except Exception as error:  # noqa: BLE001 — normalize preparation failures
            self.prestart_failure(lease, "runner-preflight", str(error))
            return

        refs = {
            "run_id": run_id,
            "tmux_session": tmux_session,
            "worktree_relpath": relative_to_root(worktree, self.root),
            "run_dir_relpath": relative_to_root(run_dir, self.root),
            "session_file_relpath": conv.get("session_file_relpath"),
        }
        if cancelled is not None and cancelled.is_set():
            watcher.write_worker_error(
                request, "stale-lease", "controller rejected lease before start"
            )
            return
        try:
            self.client.started(job_id, lease.lease_generation, refs)
        except LeaseRejected as error:
            watcher.write_worker_error(request, "stale-start", str(error))
            return
        except RunnerApiError as error:
            watcher.write_worker_error(request, "start-ack", str(error))
            try:
                self.client.fail_before_start(
                    job_id,
                    lease.lease_generation,
                    watcher.read_json_file(request["error_path"]),
                )
            except (LeaseRejected, RunnerApiError):
                pass
            return

        if cancelled is not None and cancelled.is_set():
            watcher.write_worker_error(
                request, "stale-lease", "controller rejected lease after start"
            )
            return

        try:
            launch = watcher.tmux_launch_worker(
                tmux_session,
                str(worktree),
                worker_argv,
                hosted_worker_env(conv),
            )
            if launch.returncode != 0:
                raise WorkspaceError(
                    f"tmux launch failed ({launch.returncode}): "
                    f"{watcher.command_tail(launch.stdout, launch.stderr)}"
                )
        except Exception as error:  # noqa: BLE001 — report a fenced post-start launch failure
            watcher.tmux_kill_session(tmux_session)
            watcher.write_worker_error(request, "runner-launch", str(error))
            self.upload_terminal(
                job_id,
                lease.lease_generation,
                "failed",
                error=watcher.read_json_file(request["error_path"]),
            )
            return

        deadline = time.monotonic() + float(request["timeout_seconds"])
        session_upgrade_attempted = False
        while True:
            if cancelled is not None and cancelled.is_set():
                watcher.tmux_kill_session(tmux_session)
                watcher.write_worker_error(
                    request, "stale-lease", "controller rejected active lease"
                )
                return
            result_path = Path(request["result_path"])
            error_path = Path(request["error_path"])
            if error_path.is_file():
                error = self.read_terminal_artifact(error_path, request, "error")
                if error is None:
                    continue
                self.upload_terminal(
                    job_id,
                    lease.lease_generation,
                    "failed",
                    error=error,
                )
                return
            if result_path.is_file():
                result = self.read_terminal_artifact(result_path, request, "result")
                if result is None:
                    continue
                session_file = result.pop("session_file", None)
                new_relpath = None
                if session_file:
                    new_relpath = relative_to_root(Path(session_file), self.root)
                    result["session_file_relpath"] = new_relpath
                meta_relpath = new_relpath or conv.get("session_file_relpath")
                if meta_relpath:
                    try:
                        persist_conversation_meta(
                            Path(conv["session_dir"]),
                            {
                                "provider": conv["provider"],
                                "model": conv["model"],
                                "effort": conv.get("effort"),
                                "session_id": result.get("session_id"),
                                "session_file_relpath": meta_relpath,
                                "updated_at": time.time(),
                            },
                        )
                    except OSError as error:
                        logger.info(
                            "conversation %s: failed to persist session meta (%s)",
                            conv["session_dir"],
                            error,
                        )
                self.upload_terminal(
                    job_id,
                    lease.lease_generation,
                    "succeeded",
                    result=result,
                )
                return
            if time.monotonic() >= deadline:
                watcher.tmux_kill_session(tmux_session)
                watcher.write_worker_error(
                    request, "timeout", "worker exceeded configured timeout"
                )
                continue
            if not watcher.tmux_has_session(tmux_session):
                watcher.write_worker_error(
                    request,
                    "disappeared",
                    "tmux pane exited without result or error artifact",
                )
                continue
            if refs["session_file_relpath"] is None and not session_upgrade_attempted:
                session_files = sorted(
                    Path(conv["session_dir"]).glob("*.jsonl"),
                    key=lambda path: path.stat().st_mtime,
                )
                if session_files:
                    refs["session_file_relpath"] = relative_to_root(
                        session_files[-1], self.root
                    )
                    session_upgrade_attempted = True
                    try:
                        self.client.started(job_id, lease.lease_generation, refs)
                    except (LeaseRejected, RunnerApiError) as error:
                        logger.info(
                            "conversation %s: mid-run session upgrade failed (%s)",
                            conv["session_dir"],
                            error,
                        )
            time.sleep(1)
