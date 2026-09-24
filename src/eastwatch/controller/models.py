from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

JobState = Literal["queued", "leased", "running", "succeeded", "failed", "cancelled"]
CompletionState = Literal["succeeded", "failed"]


@dataclass(frozen=True, slots=True)
class JobEnvelope:
    schema_version: int
    job_id: str
    conversation_key: str
    host: str
    project_path: str
    project_id: int
    issue_iid: int
    issue_url: str
    trigger_kind: str
    trigger_event_key: str
    owner_username: str
    provider: str
    model: str
    effort: str | None
    context: dict[str, object]
    messages: tuple[str, ...]
    reply_target: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["messages"] = list(self.messages)
        return payload


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    envelope: JobEnvelope
    workspace_id: str
    lease_generation: int
    lease_until: float


@dataclass(frozen=True, slots=True)
class RunReferences:
    run_id: str
    tmux_session: str
    worktree_relpath: str
    run_dir_relpath: str
    session_file_relpath: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ActiveLease:
    job_id: str
    lease_generation: int


@dataclass(frozen=True, slots=True)
class Completion:
    state: CompletionState
    result: dict[str, object] | None
    error: dict[str, object] | None


class StaleLeaseError(RuntimeError):
    pass


class OwnershipError(RuntimeError):
    pass


class UnknownWorkspaceError(RuntimeError):
    pass
