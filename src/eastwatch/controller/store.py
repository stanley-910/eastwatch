from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from pathlib import Path

from eastwatch.controller.models import (
    ActiveLease,
    ClaimedJob,
    Completion,
    JobEnvelope,
    RunReferences,
    OwnershipError,
    StaleLeaseError,
    UnknownWorkspaceError,
)

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2
SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    project_key TEXT PRIMARY KEY,
    host TEXT NOT NULL,
    project_path TEXT NOT NULL,
    project_id INTEGER NOT NULL,
    last_event_id INTEGER NOT NULL DEFAULT 0 CHECK (last_event_id >= 0),
    bootstrapped INTEGER NOT NULL DEFAULT 0 CHECK (bootstrapped IN (0, 1)),
    UNIQUE (host, project_path)
);
CREATE TABLE IF NOT EXISTS event_receipts (
    project_key TEXT NOT NULL REFERENCES projects(project_key),
    event_key TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    actor_username TEXT,
    payload_json TEXT NOT NULL,
    observed_at REAL NOT NULL,
    PRIMARY KEY (project_key, event_key)
);
CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id TEXT PRIMARY KEY,
    owner_username TEXT NOT NULL UNIQUE,
    owner_user_id INTEGER NOT NULL UNIQUE,
    token_sha256 TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    ready INTEGER NOT NULL DEFAULT 0 CHECK (ready IN (0, 1)),
    capacity INTEGER NOT NULL CHECK (capacity BETWEEN 1 AND 10),
    default_spec TEXT NOT NULL,
    allowed_specs_json TEXT NOT NULL,
    container_name TEXT NOT NULL UNIQUE,
    host_root TEXT NOT NULL UNIQUE,
    last_seen_at REAL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    conversation_key TEXT PRIMARY KEY,
    project_key TEXT NOT NULL REFERENCES projects(project_key),
    issue_iid INTEGER NOT NULL,
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
    status TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    effort TEXT,
    pending_json TEXT NOT NULL DEFAULT '[]',
    session_refs_json TEXT,
    current_job_id TEXT,
    updated_at REAL NOT NULL,
    UNIQUE (project_key, issue_iid)
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    conversation_key TEXT NOT NULL REFERENCES conversations(conversation_key),
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
    trigger_event_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('queued', 'leased', 'running', 'succeeded', 'failed', 'cancelled')),
    envelope_json TEXT NOT NULL,
    lease_generation INTEGER NOT NULL DEFAULT 0,
    lease_until REAL,
    last_heartbeat_at REAL,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    failure_stage TEXT,
    failure_reason TEXT
);
CREATE INDEX IF NOT EXISTS jobs_claimable
    ON jobs(workspace_id, state, created_at);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
    lease_generation INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('running', 'succeeded', 'failed')),
    refs_json TEXT NOT NULL,
    result_json TEXT,
    error_json TEXT,
    started_at REAL NOT NULL,
    finished_at REAL,
    UNIQUE (job_id, lease_generation)
);
CREATE TABLE IF NOT EXISTS outbox (
    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'delivering', 'delivered')),
    available_at REAL NOT NULL,
    lease_until REAL,
    lease_generation INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at REAL NOT NULL,
    delivered_at REAL
);
CREATE TABLE IF NOT EXISTS merge_requests (
    project_key TEXT NOT NULL REFERENCES projects(project_key),
    mr_iid INTEGER NOT NULL,
    conversation_key TEXT NOT NULL REFERENCES conversations(conversation_key),
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    state TEXT NOT NULL,
    merged_at REAL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (project_key, mr_iid, job_id, run_id)
);
CREATE TABLE IF NOT EXISTS workspace_actions (
    action_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'leased', 'done', 'failed')),
    lease_generation INTEGER NOT NULL DEFAULT 0,
    lease_until REAL,
    created_at REAL NOT NULL,
    finished_at REAL,
    error TEXT
);
CREATE TABLE IF NOT EXISTS retention_records (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
    eligible_after REAL NOT NULL,
    compacted_at REAL,
    audit_json TEXT
);
"""


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ControllerStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> None:
        connection = self.connect()
        try:
            connection.execute("BEGIN EXCLUSIVE")
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    connection.execute(statement)
            row = connection.execute(
                "SELECT version FROM schema_meta WHERE singleton = 1"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO schema_meta(singleton, version) VALUES (1, ?)",
                    (SCHEMA_VERSION,),
                )
            elif row["version"] == 1:
                connection.execute("ALTER TABLE jobs ADD COLUMN last_heartbeat_at REAL")
                connection.execute(
                    "UPDATE schema_meta SET version = ? WHERE singleton = 1",
                    (SCHEMA_VERSION,),
                )
            elif row["version"] != SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported controller schema {row['version']}; expected {SCHEMA_VERSION}"
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def authenticate_workspace(self, token: str) -> sqlite3.Row:
        digest = token_digest(token)
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM workspaces WHERE token_sha256 = ? AND enabled = 1",
                (digest,),
            ).fetchone()
        if row is None:
            raise UnknownWorkspaceError("invalid or disabled workspace token")
        return row

    def mark_workspace_ready(self, workspace_id: str, now: float | None = None) -> None:
        ready_at = time.time() if now is None else now
        with self.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE workspaces
                SET ready = 1, last_seen_at = ?
                WHERE workspace_id = ? AND enabled = 1
                """,
                (ready_at, workspace_id),
            ).rowcount
            if changed != 1:
                raise UnknownWorkspaceError(workspace_id)

    def record_event(
        self,
        project_key: str,
        event_key: str,
        event_kind: str,
        actor_username: str | None,
        payload: dict[str, object],
        now: float | None = None,
    ) -> bool:
        observed_at = time.time() if now is None else now
        with self.transaction() as connection:
            inserted = connection.execute(
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
                    json.dumps(payload, sort_keys=True),
                    observed_at,
                ),
            ).rowcount
        return inserted == 1

    def claim_outbox(
        self,
        limit: int,
        lease_seconds: float,
        now: float | None = None,
    ) -> tuple[dict[str, object], ...]:
        claimed_at = time.time() if now is None else now
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE outbox
                SET state = 'pending', lease_until = NULL
                WHERE state = 'delivering' AND lease_until < ?
                """,
                (claimed_at,),
            )
            rows = connection.execute(
                """
                SELECT outbox_id, kind, payload_json, attempts, lease_generation
                FROM outbox
                WHERE state = 'pending' AND available_at <= ?
                ORDER BY outbox_id
                LIMIT ?
                """,
                (claimed_at, max(0, limit)),
            ).fetchall()
            claimed: list[dict[str, object]] = []
            for row in rows:
                connection.execute(
                    """
                    UPDATE outbox
                    SET state = 'delivering', lease_until = ?,
                        lease_generation = lease_generation + 1,
                        attempts = attempts + 1
                    WHERE outbox_id = ? AND state = 'pending'
                    """,
                    (claimed_at + lease_seconds, row["outbox_id"]),
                )
                claimed.append(
                    {
                        "outbox_id": int(row["outbox_id"]),
                        "kind": str(row["kind"]),
                        "payload": json.loads(row["payload_json"]),
                        "attempt": int(row["attempts"]) + 1,
                        "lease_generation": int(row["lease_generation"]) + 1,
                    }
                )
        return tuple(claimed)

    def mark_outbox_delivered(
        self,
        outbox_id: int,
        lease_generation: int,
        now: float | None = None,
    ) -> None:
        delivered_at = time.time() if now is None else now
        with self.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE outbox
                SET state = 'delivered', lease_until = NULL, delivered_at = ?
                WHERE outbox_id = ? AND state = 'delivering' AND lease_generation = ?
                """,
                (delivered_at, outbox_id, lease_generation),
            ).rowcount
            if changed != 1:
                raise RuntimeError(f"outbox item is not leased: {outbox_id}")

    def mark_outbox_failed(
        self,
        outbox_id: int,
        lease_generation: int,
        error: str,
        retry_at: float,
    ) -> None:
        with self.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE outbox
                SET state = 'pending', lease_until = NULL,
                    available_at = ?, last_error = ?
                WHERE outbox_id = ? AND state = 'delivering' AND lease_generation = ?
                """,
                (retry_at, error[-1000:], outbox_id, lease_generation),
            ).rowcount
            if changed != 1:
                raise RuntimeError(f"outbox item is not leased: {outbox_id}")

    @staticmethod
    def _job_owner(
        connection: sqlite3.Connection,
        envelope: JobEnvelope,
    ) -> tuple[str, str]:
        owner = connection.execute(
            """
            SELECT c.workspace_id, c.project_key, w.owner_username
            FROM conversations AS c
            JOIN workspaces AS w ON w.workspace_id = c.workspace_id
            WHERE c.conversation_key = ? AND w.enabled = 1
            """,
            (envelope.conversation_key,),
        ).fetchone()
        if owner is None:
            raise OwnershipError(
                f"conversation has no enabled workspace: {envelope.conversation_key}"
            )
        if owner["owner_username"] != envelope.owner_username:
            raise OwnershipError(
                f"envelope owner {envelope.owner_username!r} does not own {envelope.conversation_key}"
            )
        return str(owner["workspace_id"]), str(owner["project_key"])

    @staticmethod
    def _insert_job(
        connection: sqlite3.Connection,
        envelope: JobEnvelope,
        workspace_id: str,
        created_at: float,
    ) -> None:
        connection.execute(
            """
            INSERT INTO jobs(
                job_id, conversation_key, workspace_id, trigger_event_key,
                state, envelope_json, created_at
            ) VALUES (?, ?, ?, ?, 'queued', ?, ?)
            """,
            (
                envelope.job_id,
                envelope.conversation_key,
                workspace_id,
                envelope.trigger_event_key,
                json.dumps(envelope.to_dict(), sort_keys=True),
                created_at,
            ),
        )
        connection.execute(
            "UPDATE conversations SET current_job_id = ?, status = 'queued', updated_at = ? "
            "WHERE conversation_key = ?",
            (envelope.job_id, created_at, envelope.conversation_key),
        )

    def record_dispatch_event(
        self,
        envelope: JobEnvelope,
        event_kind: str,
        actor_username: str,
        payload: dict[str, object],
        now: float | None = None,
    ) -> tuple[str, bool]:
        created_at = time.time() if now is None else now
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT job_id FROM jobs WHERE trigger_event_key = ?",
                (envelope.trigger_event_key,),
            ).fetchone()
            if existing is not None:
                return str(existing["job_id"]), False
            workspace_id, project_key = self._job_owner(connection, envelope)
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO event_receipts(
                    project_key, event_key, event_kind, actor_username,
                    payload_json, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    project_key,
                    envelope.trigger_event_key,
                    event_kind,
                    actor_username,
                    json.dumps(payload, sort_keys=True),
                    created_at,
                ),
            ).rowcount
            if inserted != 1:
                raise RuntimeError(
                    f"dispatch receipt exists without a job: {project_key}/{envelope.trigger_event_key}"
                )
            self._insert_job(connection, envelope, workspace_id, created_at)
        return envelope.job_id, True

    def queue_pending(
        self,
        conversation_key: str,
        entry: dict[str, object],
        *,
        project_key: str,
        event_key: str,
        event_kind: str,
        actor_username: str,
        reason: str,
        now: float | None = None,
    ) -> bool:
        """Append entry to conversations.pending_json and receipt the event, one transaction.

        Returns False (no-op, entry not appended) when the event was already receipted.
        """
        observed_at = time.time() if now is None else now
        with self.transaction() as connection:
            inserted = connection.execute(
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
                    observed_at,
                ),
            ).rowcount
            if inserted != 1:
                return False
            row = connection.execute(
                "SELECT pending_json FROM conversations WHERE conversation_key = ?",
                (conversation_key,),
            ).fetchone()
            pending = json.loads(row["pending_json"]) if row is not None else []
            pending.append(entry)
            connection.execute(
                "UPDATE conversations SET pending_json = ?, updated_at = ? WHERE conversation_key = ?",
                (json.dumps(pending, sort_keys=True), observed_at, conversation_key),
            )
        return True

    def pending_conversations(self, project_key: str) -> tuple[sqlite3.Row, ...]:
        """Conversations with queued comment gestures whose current job (if any) has finished."""
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT c.conversation_key, c.issue_iid, c.pending_json
                FROM conversations AS c
                LEFT JOIN jobs AS j ON j.job_id = c.current_job_id
                WHERE c.project_key = ?
                  AND c.pending_json != '[]'
                  AND (c.current_job_id IS NULL OR j.state IN ('succeeded', 'failed'))
                  AND c.status NOT IN ('finishing', 'cleaning', 'cleanup-failed')
                """,
                (project_key,),
            ).fetchall()
        return tuple(rows)

    def clear_pending(
        self, conversation_key: str, count: int, now: float | None = None
    ) -> None:
        """Drop the first `count` entries from pending_json; later concurrent additions survive."""
        updated_at = time.time() if now is None else now
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT pending_json FROM conversations WHERE conversation_key = ?",
                (conversation_key,),
            ).fetchone()
            if row is None:
                return
            remaining = json.loads(row["pending_json"])[count:]
            connection.execute(
                "UPDATE conversations SET pending_json = ?, updated_at = ? WHERE conversation_key = ?",
                (json.dumps(remaining, sort_keys=True), updated_at, conversation_key),
            )

    def enqueue_job(self, envelope: JobEnvelope, now: float | None = None) -> str:
        created_at = time.time() if now is None else now
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT job_id FROM jobs WHERE trigger_event_key = ?",
                (envelope.trigger_event_key,),
            ).fetchone()
            if existing is not None:
                return str(existing["job_id"])
            workspace_id, _project_key = self._job_owner(connection, envelope)
            self._insert_job(connection, envelope, workspace_id, created_at)
        return envelope.job_id

    def claim_jobs(
        self,
        workspace_id: str,
        limit: int,
        lease_seconds: float,
        now: float | None = None,
    ) -> tuple[ClaimedJob, ...]:
        claimed_at = time.time() if now is None else now
        requested = max(0, min(limit, 10))
        if requested == 0:
            return ()
        with self.transaction() as connection:
            workspace = connection.execute(
                "SELECT capacity FROM workspaces WHERE workspace_id = ? AND enabled = 1",
                (workspace_id,),
            ).fetchone()
            if workspace is None:
                raise UnknownWorkspaceError(workspace_id)
            running = int(
                connection.execute(
                    "SELECT COUNT(*) FROM jobs WHERE workspace_id = ? AND state IN ('leased', 'running')",
                    (workspace_id,),
                ).fetchone()[0]
            )
            available = max(0, min(requested, int(workspace["capacity"]) - running))
            rows = connection.execute(
                """
                SELECT job_id, envelope_json, lease_generation
                FROM jobs
                WHERE workspace_id = ? AND state = 'queued'
                ORDER BY created_at, job_id
                LIMIT ?
                """,
                (workspace_id, available),
            ).fetchall()
            leases: list[ClaimedJob] = []
            lease_until = claimed_at + lease_seconds
            for row in rows:
                generation = int(row["lease_generation"]) + 1
                changed = connection.execute(
                    """
                    UPDATE jobs
                    SET state = 'leased', lease_generation = ?, lease_until = ?,
                        last_heartbeat_at = ?
                    WHERE job_id = ? AND state = 'queued'
                    """,
                    (generation, lease_until, claimed_at, row["job_id"]),
                ).rowcount
                if changed != 1:
                    continue
                payload = json.loads(row["envelope_json"])
                payload["messages"] = tuple(payload.get("messages", ()))
                leases.append(
                    ClaimedJob(
                        envelope=JobEnvelope(**payload),
                        workspace_id=workspace_id,
                        lease_generation=generation,
                        lease_until=lease_until,
                    )
                )
            connection.execute(
                "UPDATE workspaces SET last_seen_at = ? WHERE workspace_id = ?",
                (claimed_at, workspace_id),
            )
        return tuple(leases)

    def mark_started(
        self,
        workspace_id: str,
        job_id: str,
        generation: int,
        refs: RunReferences,
        now: float | None = None,
    ) -> bool:
        started_at = time.time() if now is None else now
        refs_json = json.dumps(refs.to_dict(), sort_keys=True)
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT state, lease_until
                FROM jobs
                WHERE job_id = ? AND workspace_id = ? AND lease_generation = ?
                """,
                (job_id, workspace_id, generation),
            ).fetchone()
            if row is None:
                raise StaleLeaseError(
                    f"cannot start unknown lease {job_id}/{generation}"
                )
            if row["state"] == "running":
                run = connection.execute(
                    """
                    SELECT run_id, refs_json FROM runs
                    WHERE job_id = ? AND workspace_id = ? AND lease_generation = ?
                    """,
                    (job_id, workspace_id, generation),
                ).fetchone()
                if run is not None and run["run_id"] == refs.run_id:
                    if run["refs_json"] == refs_json:
                        return False
                    stored_refs = json.loads(run["refs_json"])
                    new_refs = refs.to_dict()
                    stored_session = stored_refs.get("session_file_relpath")
                    new_session = new_refs.get("session_file_relpath")
                    stored_rest = {
                        k: v
                        for k, v in stored_refs.items()
                        if k != "session_file_relpath"
                    }
                    new_rest = {
                        k: v for k, v in new_refs.items() if k != "session_file_relpath"
                    }
                    if (
                        not stored_session
                        and isinstance(new_session, str)
                        and new_session
                        and stored_rest == new_rest
                    ):
                        connection.execute(
                            "UPDATE runs SET refs_json = ? "
                            "WHERE job_id = ? AND workspace_id = ? AND lease_generation = ?",
                            (refs_json, job_id, workspace_id, generation),
                        )
                        return False
                raise StaleLeaseError(
                    f"start retry changed run references for {job_id}/{generation}"
                )
            if row["state"] != "leased" or row["lease_until"] is None:
                raise StaleLeaseError(
                    f"cannot start {row['state']} job {job_id}/{generation}"
                )
            if float(row["lease_until"]) < started_at:
                raise StaleLeaseError(
                    f"cannot start expired lease {job_id}/{generation}"
                )
            connection.execute(
                "UPDATE jobs SET state = 'running', started_at = ? WHERE job_id = ?",
                (started_at, job_id),
            )
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, job_id, workspace_id, lease_generation,
                    state, refs_json, started_at
                ) VALUES (?, ?, ?, ?, 'running', ?, ?)
                """,
                (refs.run_id, job_id, workspace_id, generation, refs_json, started_at),
            )
            connection.execute(
                "UPDATE conversations SET status = 'working', updated_at = ? "
                "WHERE current_job_id = ?",
                (started_at, job_id),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO outbox(
                    dedupe_key, kind, payload_json, available_at, created_at
                ) VALUES (?, 'job-started', ?, ?, ?)
                """,
                (
                    f"job-started:{job_id}",
                    json.dumps({"job_id": job_id}, sort_keys=True),
                    started_at,
                    started_at,
                ),
            )
        return True

    def heartbeat(
        self,
        workspace_id: str,
        active: Sequence[ActiveLease],
        lease_seconds: float,
        now: float | None = None,
    ) -> None:
        heartbeat_at = time.time() if now is None else now
        lease_until = heartbeat_at + lease_seconds
        stale: list[str] = []
        with self.transaction() as connection:
            changed = connection.execute(
                "UPDATE workspaces SET last_seen_at = ? WHERE workspace_id = ? AND enabled = 1",
                (heartbeat_at, workspace_id),
            ).rowcount
            if changed != 1:
                raise UnknownWorkspaceError(workspace_id)
            for lease in active:
                updated = connection.execute(
                    """
                    UPDATE jobs SET lease_until = ?, last_heartbeat_at = ?
                    WHERE job_id = ? AND workspace_id = ? AND lease_generation = ?
                      AND state IN ('leased', 'running') AND lease_until >= ?
                    """,
                    (
                        lease_until,
                        heartbeat_at,
                        lease.job_id,
                        workspace_id,
                        lease.lease_generation,
                        heartbeat_at,
                    ),
                ).rowcount
                if updated != 1:
                    stale.append(f"{lease.job_id}/{lease.lease_generation}")
        if stale:
            raise StaleLeaseError(
                f"cannot heartbeat stale lease(s): {', '.join(stale)}"
            )

    def complete_job(
        self,
        workspace_id: str,
        job_id: str,
        generation: int,
        completion: Completion,
        now: float | None = None,
    ) -> bool:
        finished_at = time.time() if now is None else now
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT j.state, j.lease_until, r.run_id
                FROM jobs AS j
                LEFT JOIN runs AS r
                  ON r.job_id = j.job_id AND r.lease_generation = j.lease_generation
                WHERE j.job_id = ? AND j.workspace_id = ? AND j.lease_generation = ?
                """,
                (job_id, workspace_id, generation),
            ).fetchone()
            if row is None:
                raise StaleLeaseError(f"unknown lease {job_id}/{generation}")
            if row["state"] in ("succeeded", "failed"):
                return False
            if row["state"] != "running" or row["run_id"] is None:
                raise StaleLeaseError(
                    f"cannot complete {row['state']} job {job_id}/{generation}"
                )
            if row["lease_until"] is None or float(row["lease_until"]) < finished_at:
                raise StaleLeaseError(
                    f"cannot complete expired lease {job_id}/{generation}"
                )
            error = completion.error or {}
            connection.execute(
                """
                UPDATE jobs
                SET state = ?, finished_at = ?, lease_until = NULL,
                    failure_stage = ?, failure_reason = ?
                WHERE job_id = ? AND workspace_id = ? AND lease_generation = ?
                """,
                (
                    completion.state,
                    finished_at,
                    error.get("kind") if completion.state == "failed" else None,
                    error.get("message") if completion.state == "failed" else None,
                    job_id,
                    workspace_id,
                    generation,
                ),
            )
            connection.execute(
                """
                UPDATE runs
                SET state = ?, result_json = ?, error_json = ?, finished_at = ?
                WHERE run_id = ? AND state = 'running'
                """,
                (
                    completion.state,
                    json.dumps(completion.result, sort_keys=True)
                    if completion.result
                    else None,
                    json.dumps(completion.error, sort_keys=True)
                    if completion.error
                    else None,
                    finished_at,
                    row["run_id"],
                ),
            )
            connection.execute(
                "UPDATE conversations SET status = 'finishing', updated_at = ? WHERE current_job_id = ?",
                (finished_at, job_id),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO outbox(
                    dedupe_key, kind, payload_json, available_at, created_at
                ) VALUES (?, 'job-completed', ?, ?, ?)
                """,
                (
                    f"job-completed:{job_id}:{generation}",
                    json.dumps(
                        {
                            "job_id": job_id,
                            "workspace_id": workspace_id,
                            "lease_generation": generation,
                            "state": completion.state,
                        },
                        sort_keys=True,
                    ),
                    finished_at,
                    finished_at,
                ),
            )
        return True

    def fail_unstarted(
        self,
        workspace_id: str,
        job_id: str,
        generation: int,
        error: dict[str, object],
        now: float | None = None,
    ) -> bool:
        finished_at = time.time() if now is None else now
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT state, lease_until, failure_stage, failure_reason
                FROM jobs
                WHERE job_id = ? AND workspace_id = ? AND lease_generation = ?
                """,
                (job_id, workspace_id, generation),
            ).fetchone()
            if row is None:
                raise StaleLeaseError(f"unknown lease {job_id}/{generation}")
            if row["state"] == "failed":
                return False
            if row["state"] != "leased" or row["lease_until"] is None:
                raise StaleLeaseError(f"cannot fail {row['state']} job before start")
            if float(row["lease_until"]) < finished_at:
                raise StaleLeaseError(
                    f"cannot fail expired lease {job_id}/{generation}"
                )
            stage = str(error.get("kind") or "runner-preflight")
            reason = str(error.get("message") or "runner failed before launch")[-1000:]
            connection.execute(
                """
                UPDATE jobs
                SET state = 'failed', finished_at = ?, lease_until = NULL,
                    failure_stage = ?, failure_reason = ?
                WHERE job_id = ?
                """,
                (finished_at, stage, reason, job_id),
            )
            connection.execute(
                "UPDATE conversations SET status = 'finishing', updated_at = ? WHERE current_job_id = ?",
                (finished_at, job_id),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO outbox(
                    dedupe_key, kind, payload_json, available_at, created_at
                ) VALUES (?, 'job-completed', ?, ?, ?)
                """,
                (
                    f"job-completed:{job_id}:{generation}",
                    json.dumps(
                        {
                            "job_id": job_id,
                            "workspace_id": workspace_id,
                            "lease_generation": generation,
                            "state": "failed",
                        },
                        sort_keys=True,
                    ),
                    finished_at,
                    finished_at,
                ),
            )
        return True

    def reap_expired(self, now: float | None = None) -> tuple[str, ...]:
        checked_at = time.time() if now is None else now
        with self.transaction() as connection:
            requeued = connection.execute(
                "SELECT job_id FROM jobs WHERE state = 'leased' AND lease_until < ?",
                (checked_at,),
            ).fetchall()
            requeued_ids = tuple(str(row["job_id"]) for row in requeued)
            connection.execute(
                """
                UPDATE jobs
                SET state = 'queued', lease_until = NULL
                WHERE state = 'leased' AND lease_until < ?
                """,
                (checked_at,),
            )
            lost = connection.execute(
                "SELECT job_id FROM jobs WHERE state = 'running' AND lease_until < ?",
                (checked_at,),
            ).fetchall()
            job_ids = tuple(str(row["job_id"]) for row in lost)
            for job_id in job_ids:
                connection.execute(
                    """
                    UPDATE jobs
                    SET state = 'failed', finished_at = ?, lease_until = NULL,
                        failure_stage = 'runner-lost',
                        failure_reason = 'workspace heartbeat expired; inspect artifacts before retrying'
                    WHERE job_id = ? AND state = 'running'
                    """,
                    (checked_at, job_id),
                )
                connection.execute(
                    "UPDATE runs SET state = 'failed', finished_at = ? "
                    "WHERE job_id = ? AND state = 'running'",
                    (checked_at, job_id),
                )
                connection.execute(
                    "UPDATE conversations SET status = 'finishing', updated_at = ? "
                    "WHERE current_job_id = ?",
                    (checked_at, job_id),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO outbox(
                        dedupe_key, kind, payload_json, available_at, created_at
                    ) VALUES (?, 'runner-lost', ?, ?, ?)
                    """,
                    (
                        f"runner-lost:{job_id}",
                        json.dumps({"job_id": job_id}, sort_keys=True),
                        checked_at,
                        checked_at,
                    ),
                )
        if requeued_ids:
            log.warning(
                "requeued %d leased job(s) after lease expiry: %s",
                len(requeued_ids),
                ", ".join(requeued_ids),
            )
        if job_ids:
            log.warning(
                "failed %d running job(s) after lease expiry: %s",
                len(job_ids),
                ", ".join(job_ids),
            )
        return job_ids

    def claim_workspace_actions(
        self,
        workspace_id: str,
        limit: int = 5,
        lease_seconds: float = 90,
        now: float | None = None,
    ) -> tuple[dict[str, object], ...]:
        claimed_at = time.time() if now is None else now
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE workspace_actions
                SET state = 'pending', lease_until = NULL
                WHERE workspace_id = ? AND state = 'leased' AND lease_until < ?
                """,
                (workspace_id, claimed_at),
            )
            rows = connection.execute(
                """
                SELECT action_id, kind, payload_json, lease_generation
                FROM workspace_actions
                WHERE workspace_id = ? AND state = 'pending'
                ORDER BY created_at, action_id
                LIMIT ?
                """,
                (workspace_id, max(0, min(limit, 5))),
            ).fetchall()
            claimed = []
            for row in rows:
                payload = json.loads(row["payload_json"])
                job = connection.execute(
                    """
                    SELECT j.state, c.current_job_id, c.conversation_key
                    FROM jobs AS j
                    JOIN conversations AS c ON c.conversation_key = j.conversation_key
                    WHERE j.job_id = ? AND j.workspace_id = ?
                    """,
                    (payload["job_id"], workspace_id),
                ).fetchone()
                if (
                    job is None
                    or job["state"] not in ("succeeded", "failed", "cancelled")
                    or job["current_job_id"] != payload["job_id"]
                ):
                    continue
                generation = int(row["lease_generation"]) + 1
                updated = connection.execute(
                    """
                    UPDATE workspace_actions
                    SET state = 'leased', lease_generation = ?, lease_until = ?
                    WHERE action_id = ? AND state = 'pending'
                    """,
                    (generation, claimed_at + lease_seconds, row["action_id"]),
                ).rowcount
                if updated != 1:
                    continue
                connection.execute(
                    "UPDATE conversations SET status = 'cleaning', updated_at = ? "
                    "WHERE conversation_key = ?",
                    (claimed_at, job["conversation_key"]),
                )
                claimed.append(
                    {
                        "action_id": str(row["action_id"]),
                        "kind": str(row["kind"]),
                        "payload": payload,
                        "lease_generation": generation,
                        "lease_until": claimed_at + lease_seconds,
                    }
                )
        return tuple(claimed)

    def finish_workspace_action(
        self,
        workspace_id: str,
        action_id: str,
        generation: int,
        *,
        success: bool,
        error: str | None,
        now: float | None = None,
    ) -> bool:
        finished_at = time.time() if now is None else now
        with self.transaction() as connection:
            action = connection.execute(
                """
                SELECT state, kind, payload_json, lease_until
                FROM workspace_actions
                WHERE action_id = ? AND workspace_id = ? AND lease_generation = ?
                """,
                (action_id, workspace_id, generation),
            ).fetchone()
            if action is None:
                raise StaleLeaseError(
                    f"unknown workspace action {action_id}/{generation}"
                )
            if action["state"] in ("done", "failed"):
                return False
            if action["state"] != "leased":
                raise StaleLeaseError(
                    f"workspace action is {action['state']}: {action_id}"
                )
            if (
                action["lease_until"] is None
                or float(action["lease_until"]) < finished_at
            ):
                raise StaleLeaseError(
                    f"workspace action lease expired: {action_id}/{generation}"
                )
            if action["kind"] != "cleanup-run":
                raise RuntimeError(
                    f"unsupported workspace action kind: {action['kind']}"
                )
            payload = json.loads(action["payload_json"])
            job_id = str(payload["job_id"])
            row = connection.execute(
                """
                SELECT j.job_id, j.state, j.failure_stage, j.created_at,
                       j.started_at, j.finished_at, j.envelope_json,
                       c.conversation_key, c.issue_iid,
                       p.project_key, w.owner_username,
                       r.run_id, r.lease_generation
                FROM jobs AS j
                JOIN conversations AS c ON c.conversation_key = j.conversation_key
                JOIN projects AS p ON p.project_key = c.project_key
                JOIN workspaces AS w ON w.workspace_id = j.workspace_id
                LEFT JOIN runs AS r ON r.job_id = j.job_id
                WHERE j.job_id = ?
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"workspace action references unknown job: {job_id}")
            if not success:
                connection.execute(
                    """
                    UPDATE workspace_actions
                    SET state = 'failed', finished_at = ?, lease_until = NULL, error = ?
                    WHERE action_id = ?
                    """,
                    (finished_at, (error or "cleanup failed")[-1000:], action_id),
                )
                connection.execute(
                    "UPDATE conversations SET status = 'cleanup-failed', updated_at = ? "
                    "WHERE conversation_key = ?",
                    (finished_at, row["conversation_key"]),
                )
                return True
            mrs = connection.execute(
                """
                SELECT mr_iid, state, merged_at
                FROM merge_requests
                WHERE job_id = ? AND run_id = ?
                ORDER BY mr_iid
                """,
                (row["job_id"], row["run_id"]),
            ).fetchall()
            envelope = json.loads(row["envelope_json"])
            audit = {
                "schema_version": 1,
                "job_id": row["job_id"],
                "run_id": row["run_id"],
                "conversation_key": row["conversation_key"],
                "project_key": row["project_key"],
                "issue_iid": int(row["issue_iid"]),
                "owner_username": row["owner_username"],
                "provider": envelope["provider"],
                "model": envelope["model"],
                "effort": envelope.get("effort"),
                "state": row["state"],
                "failure_stage": row["failure_stage"],
                "created_at": row["created_at"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "merge_requests": [
                    {
                        "iid": int(mr["mr_iid"]),
                        "state": mr["state"],
                        "merged_at": mr["merged_at"],
                    }
                    for mr in mrs
                ],
            }
            compacted = connection.execute(
                """
                UPDATE retention_records
                SET compacted_at = ?, audit_json = ?
                WHERE run_id = ? AND compacted_at IS NULL
                """,
                (finished_at, json.dumps(audit, sort_keys=True), row["run_id"]),
            ).rowcount
            if compacted != 1:
                raise RuntimeError(
                    f"retention record is missing or already compacted: {row['run_id']}"
                )
            envelope["messages"] = []
            envelope["context"] = {}
            connection.execute(
                "UPDATE jobs SET envelope_json = ? WHERE job_id = ?",
                (json.dumps(envelope, sort_keys=True), row["job_id"]),
            )
            connection.execute(
                """
                UPDATE runs
                SET refs_json = '{}', result_json = NULL, error_json = NULL
                WHERE run_id = ?
                """,
                (row["run_id"],),
            )
            connection.execute(
                """
                UPDATE conversations
                SET status = 'archived', session_refs_json = NULL, updated_at = ?
                WHERE conversation_key = ?
                """,
                (finished_at, row["conversation_key"]),
            )
            connection.execute(
                """
                UPDATE workspace_actions
                SET state = 'done', finished_at = ?, lease_until = NULL, error = NULL
                WHERE action_id = ?
                """,
                (finished_at, action_id),
            )
        return True

    @staticmethod
    def new_job_id() -> str:
        return str(uuid.uuid4())
