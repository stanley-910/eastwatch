from __future__ import annotations

import fcntl
import shutil
import subprocess
from pathlib import Path
from typing import Any, TextIO

from eastwatch.fleet.core import chat_lock_path
from eastwatch.runner.executor import canonical_checkout, relative_to_root


class CleanupBusy(RuntimeError):
    pass


class CleanupExecutor:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def acquire_session_lock(self, payload: dict[str, Any]) -> TextIO | None:
        session_relpath = payload.get("session_file_relpath")
        if not session_relpath:
            return None
        session_file = self.root / str(session_relpath)
        relative_to_root(session_file, self.root)
        container_session = f"/home/bw/{Path(str(session_relpath)).as_posix()}"
        lock_path = chat_lock_path(container_session, self.root / ".locks")
        relative_to_root(lock_path, self.root)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = lock_path.open("w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            lock.close()
            raise CleanupBusy("interactive session resume is active") from error
        return lock

    def remove(self, action: dict[str, Any]) -> None:
        if action.get("kind") != "cleanup-run":
            raise RuntimeError(f"unsupported workspace action: {action.get('kind')}")
        payload = dict(action["payload"])
        lock = self.acquire_session_lock(payload)
        try:
            self.remove_payload(payload)
        finally:
            if lock is not None:
                lock.close()

    def remove_payload(self, payload: dict[str, Any]) -> None:
        checkout = canonical_checkout(
            self.root,
            str(payload["host"]),
            str(payload["project_path"]),
            require_exists=False,
        )
        worktree_relpath = payload.get("worktree_relpath")
        if worktree_relpath:
            worktree = self.root / str(worktree_relpath)
            relative_to_root(worktree, self.root)
            if worktree.exists() and checkout.is_dir():
                result = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(checkout),
                        "worktree",
                        "remove",
                        "--force",
                        str(worktree),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                if result.returncode != 0 and worktree.exists():
                    raise RuntimeError((result.stderr or result.stdout)[-1000:])
            elif worktree.exists():
                shutil.rmtree(worktree, ignore_errors=False)
        run_dir_relpath = payload.get("run_dir_relpath")
        if run_dir_relpath:
            run_dir = self.root / str(run_dir_relpath)
            relative_to_root(run_dir, self.root)
            if run_dir.exists():
                shutil.rmtree(run_dir, ignore_errors=False)
        session_relpath = payload.get("session_file_relpath")
        if session_relpath:
            session_file = self.root / str(session_relpath)
            relative_to_root(session_file, self.root)
            session_file.unlink(missing_ok=True)
