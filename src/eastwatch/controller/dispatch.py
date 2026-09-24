from __future__ import annotations

import json
import re
import time
from contextlib import closing
from dataclasses import dataclass
from typing import Any

from eastwatch.controller.models import JobEnvelope
from eastwatch.controller.store import ControllerStore

HINT_RE = re.compile(r"\\?\[(claude|pi):([A-Za-z0-9._-]+?)(?::([a-z]+))?\\?\]")


class DispatchRejected(RuntimeError):
    def __init__(self, message: str, *, set_failed_label: bool = True) -> None:
        super().__init__(message)
        self.set_failed_label = set_failed_label


@dataclass(frozen=True, slots=True)
class Assignee:
    username: str
    user_id: int


@dataclass(frozen=True, slots=True)
class DispatchCandidate:
    host: str
    project_path: str
    project_id: int
    issue_iid: int
    issue_url: str
    issue_title: str
    issue_description: str
    event_key: str
    event_kind: str
    actor_username: str
    trigger_kind: str
    assignees: tuple[Assignee, ...]
    hint_texts: tuple[str, ...]
    trigger_messages: tuple[str, ...]
    thread_context: str
    worker_briefing: str | None
    jira_context: str
    reply_target: dict | None = None
    actor_user_id: int | None = None

    @property
    def project_key(self) -> str:
        return f"{self.host}/{self.project_path}"

    @property
    def durable_event_key(self) -> str:
        return f"{self.project_key}:{self.event_kind}:{self.event_key}"


@dataclass(frozen=True, slots=True)
class DispatchResult:
    accepted: bool
    job_id: str | None
    reason: str | None


def parse_hint(texts: tuple[str, ...]) -> str | None:
    for text in texts:
        match = HINT_RE.search(text or "")
        if match:
            provider, model, effort = match.groups()
            return ":".join(part for part in (provider, model, effort) if part)
    return None


def split_spec(spec: str) -> tuple[str, str, str | None]:
    parts = spec.split(":")
    if len(parts) not in (2, 3) or not parts[0] or not parts[1]:
        raise DispatchRejected(f"invalid model spec: {spec!r}")
    return parts[0], parts[1], parts[2] if len(parts) == 3 else None


class HostedDispatcher:
    def __init__(
        self,
        store: ControllerStore,
        admins: frozenset[str],
        approved_bots: frozenset[str],
    ) -> None:
        self.store = store
        self.admins = admins
        self.approved_bots = approved_bots

    def _ensure_project(self, candidate: DispatchCandidate) -> None:
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO projects(project_key, host, project_path, project_id, bootstrapped)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(project_key) DO UPDATE SET
                    project_id = excluded.project_id,
                    host = excluded.host,
                    project_path = excluded.project_path
                """,
                (
                    candidate.project_key,
                    candidate.host,
                    candidate.project_path,
                    candidate.project_id,
                ),
            )

    def _reject(
        self,
        candidate: DispatchCandidate,
        reason: str,
        *,
        set_failed_label: bool,
    ) -> DispatchResult:
        now = time.time()
        with self.store.transaction() as connection:
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO event_receipts(
                    project_key, event_key, event_kind, actor_username,
                    payload_json, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.project_key,
                    candidate.durable_event_key,
                    candidate.event_kind,
                    candidate.actor_username,
                    json.dumps(
                        {"issue_iid": candidate.issue_iid, "reason": reason},
                        sort_keys=True,
                    ),
                    now,
                ),
            ).rowcount
            if inserted:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO outbox(
                        dedupe_key, kind, payload_json, available_at, created_at
                    ) VALUES (?, 'dispatch-rejected', ?, ?, ?)
                    """,
                    (
                        f"dispatch-rejected:{candidate.durable_event_key}",
                        json.dumps(
                            {
                                "project_key": candidate.project_key,
                                "issue_iid": candidate.issue_iid,
                                "reason": reason,
                                "set_failed_label": set_failed_label,
                                "rejected_at": now,
                            },
                            sort_keys=True,
                        ),
                        now,
                        now,
                    ),
                )
        return DispatchResult(accepted=False, job_id=None, reason=reason)

    def _workspace(self, assignee: Assignee) -> dict[str, Any]:
        with closing(self.store.connect()) as connection:
            row = connection.execute(
                """
                SELECT workspace_id, owner_username, owner_user_id,
                       default_spec, allowed_specs_json
                FROM workspaces
                WHERE owner_username = ? AND enabled = 1 AND ready = 1
                """,
                (assignee.username,),
            ).fetchone()
        if row is None:
            raise DispatchRejected(
                f"assignee @{assignee.username} has no dispatch-ready workspace"
            )
        if int(row["owner_user_id"]) != assignee.user_id:
            raise DispatchRejected(
                f"GitLab identity changed for @{assignee.username}; admin must reprovision"
            )
        return dict(row)

    def _active_run(self, conversation_key: str) -> Any:
        with closing(self.store.connect()) as connection:
            return connection.execute(
                """
                SELECT c.workspace_id, c.status, j.state
                FROM conversations AS c
                LEFT JOIN jobs AS j ON j.job_id = c.current_job_id
                WHERE c.conversation_key = ?
                """,
                (conversation_key,),
            ).fetchone()

    def _queue_pending(
        self, candidate: DispatchCandidate, conversation_key: str
    ) -> DispatchResult:
        entry = {
            "messages": list(candidate.trigger_messages),
            "reply_target": dict(candidate.reply_target)
            if candidate.reply_target
            else None,
            "actor_username": candidate.actor_username,
            "actor_user_id": candidate.actor_user_id,
            "event_key": candidate.durable_event_key,
            "queued_at": time.time(),
        }
        self.store.queue_pending(
            conversation_key,
            entry,
            project_key=candidate.project_key,
            event_key=candidate.durable_event_key,
            event_kind=candidate.event_kind,
            actor_username=candidate.actor_username,
            reason="queued for next run",
        )
        return DispatchResult(accepted=False, job_id=None, reason="queued for next run")

    def _upsert_conversation(
        self,
        candidate: DispatchCandidate,
        workspace_id: str,
        provider: str,
        model: str,
        effort: str | None,
    ) -> str:
        key = f"{candidate.project_key}#{candidate.issue_iid}"
        with self.store.transaction() as connection:
            current = connection.execute(
                "SELECT status FROM conversations WHERE conversation_key = ?",
                (key,),
            ).fetchone()
            if current is not None and current["status"] in (
                "finishing",
                "cleaning",
                "cleanup-failed",
            ):
                raise DispatchRejected(
                    f"conversation is {current['status']}; resolve retention before dispatch",
                    set_failed_label=False,
                )
            connection.execute(
                """
                INSERT INTO conversations(
                    conversation_key, project_key, issue_iid, workspace_id,
                    status, provider, model, effort, updated_at
                ) VALUES (?, ?, ?, ?, 'idle', ?, ?, ?, ?)
                ON CONFLICT(conversation_key) DO UPDATE SET
                    workspace_id = excluded.workspace_id,
                    provider = excluded.provider,
                    model = excluded.model,
                    effort = excluded.effort,
                    updated_at = excluded.updated_at
                """,
                (
                    key,
                    candidate.project_key,
                    candidate.issue_iid,
                    workspace_id,
                    provider,
                    model,
                    effort,
                    time.time(),
                ),
            )
        return key

    @staticmethod
    def _context(candidate: DispatchCandidate) -> dict[str, object]:
        return {
            "issue_title": candidate.issue_title,
            "issue_description": candidate.issue_description[:6000],
            "thread_context": candidate.thread_context[:12000],
            "worker_briefing": candidate.worker_briefing,
            "jira_context": candidate.jira_context[:12000],
        }

    def consumed_event(self, project_key: str, event_key: str) -> DispatchResult | None:
        with closing(self.store.connect()) as connection:
            job = connection.execute(
                "SELECT job_id FROM jobs WHERE trigger_event_key = ?",
                (event_key,),
            ).fetchone()
            if job is not None:
                return DispatchResult(
                    accepted=True, job_id=str(job["job_id"]), reason=None
                )
            receipt = connection.execute(
                "SELECT 1 FROM event_receipts WHERE project_key = ? AND event_key = ?",
                (project_key, event_key),
            ).fetchone()
        if receipt is not None:
            return DispatchResult(
                accepted=False, job_id=None, reason="event was already consumed"
            )
        return None

    def _consumed(self, candidate: DispatchCandidate) -> DispatchResult | None:
        return self.consumed_event(candidate.project_key, candidate.durable_event_key)

    def record_skipped_event(
        self,
        *,
        project_key: str,
        host: str,
        project_path: str,
        project_id: int,
        event_key: str,
        event_kind: str,
        actor_username: str,
        reason: str,
    ) -> None:
        now = time.time()
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO projects(project_key, host, project_path, project_id, bootstrapped)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(project_key) DO UPDATE SET
                    project_id = excluded.project_id,
                    host = excluded.host,
                    project_path = excluded.project_path
                """,
                (project_key, host, project_path, project_id),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO event_receipts(
                    project_key, event_key, event_kind, actor_username,
                    payload_json, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    project_key,
                    event_key,
                    event_kind,
                    actor_username,
                    json.dumps({"reason": reason}, sort_keys=True),
                    now,
                ),
            )

    def dispatch(self, candidate: DispatchCandidate) -> DispatchResult:
        self._ensure_project(candidate)
        consumed = self._consumed(candidate)
        if consumed is not None:
            return consumed
        try:
            if len(candidate.assignees) > 1:
                raise DispatchRejected(
                    "hosted dispatch requires at most one issue assignee"
                )
            if candidate.assignees:
                assignee = candidate.assignees[0]
            else:
                if candidate.actor_user_id is None:
                    raise DispatchRejected(
                        f"issue has no assignee and actor @{candidate.actor_username} has no resolvable user id"
                    )
                assignee = Assignee(
                    username=candidate.actor_username,
                    user_id=int(candidate.actor_user_id),
                )
            authorized = {assignee.username, *self.admins, *self.approved_bots}
            if candidate.actor_username not in authorized:
                raise DispatchRejected(
                    f"@{candidate.actor_username} cannot dispatch work owned by @{assignee.username}"
                )
            workspace = self._workspace(assignee)
            workspace_id = str(workspace["workspace_id"])
            conversation_key = f"{candidate.project_key}#{candidate.issue_iid}"
            active = self._active_run(conversation_key)
            # "settling" (finishing/cleaning) is a seconds-wide window between a
            # run's terminal state and its note delivery/cleanup; a comment
            # landing there must queue, not burn. cleanup-failed stays a hard
            # reject below — it needs an operator, and queueing would hide it.
            if active is not None and (
                active["state"] in ("queued", "leased", "running")
                or active["status"] in ("finishing", "cleaning")
            ):
                if active["workspace_id"] != workspace_id:
                    raise DispatchRejected(
                        "issue was reassigned while its previous owner's run is active",
                        set_failed_label=False,
                    )
                if candidate.event_kind == "comment":
                    return self._queue_pending(candidate, conversation_key)
                # Label re-fire is out of scope for queueing: reject as today.
                if active["state"] in ("queued", "leased", "running"):
                    raise DispatchRejected(
                        "issue already has an active hosted run",
                        set_failed_label=False,
                    )
                raise DispatchRejected(
                    f"conversation is {active['status']}; resolve retention before dispatch",
                    set_failed_label=False,
                )
            hinted = parse_hint(candidate.hint_texts)
            spec = hinted or str(workspace["default_spec"])
            allowed = set(json.loads(workspace["allowed_specs_json"]))
            if spec not in allowed:
                raise DispatchRejected(
                    f"model spec {spec!r} is not approved for @{assignee.username}"
                )
            provider, model, effort = split_spec(spec)
            if provider != "pi":
                raise DispatchRejected("hosted pilot supports Pi model specs only")
            conversation_key = self._upsert_conversation(
                candidate,
                str(workspace["workspace_id"]),
                provider,
                model,
                effort,
            )
            envelope = JobEnvelope(
                schema_version=1,
                job_id=self.store.new_job_id(),
                conversation_key=conversation_key,
                host=candidate.host,
                project_path=candidate.project_path,
                project_id=candidate.project_id,
                issue_iid=candidate.issue_iid,
                issue_url=candidate.issue_url,
                trigger_kind=candidate.trigger_kind,
                trigger_event_key=candidate.durable_event_key,
                owner_username=assignee.username,
                provider=provider,
                model=model,
                effort=effort,
                context=self._context(candidate),
                messages=candidate.trigger_messages,
                reply_target=(
                    dict(candidate.reply_target)
                    if candidate.reply_target
                    else {"kind": "issue", "iid": candidate.issue_iid}
                ),
            )
            job_id, _created = self.store.record_dispatch_event(
                envelope,
                candidate.event_kind,
                candidate.actor_username,
                {
                    "issue_iid": candidate.issue_iid,
                    "trigger_kind": candidate.trigger_kind,
                },
            )
            return DispatchResult(accepted=True, job_id=job_id, reason=None)
        except DispatchRejected as error:
            return self._reject(
                candidate,
                str(error),
                set_failed_label=error.set_failed_label,
            )
