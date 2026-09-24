from __future__ import annotations

import json
import time
from contextlib import closing
from datetime import datetime

from eastwatch import watcher
from eastwatch.controller.store import ControllerStore
from eastwatch.controller.tokens import ProjectTokens, token_for_project

MERGE_RETENTION_SECONDS = 7 * 24 * 60 * 60


def timestamp(value: str | None) -> float | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class RetentionManager:
    def __init__(self, store: ControllerStore, bot_tokens: ProjectTokens) -> None:
        self.store = store
        self.bot_tokens = bot_tokens

    def refresh_merge_requests(self) -> int:
        with closing(self.store.connect()) as connection:
            rows = connection.execute(
                """
                SELECT mr.project_key, mr.mr_iid, p.host, p.project_id
                FROM merge_requests AS mr
                JOIN projects AS p ON p.project_key = mr.project_key
                WHERE mr.state != 'merged'
                ORDER BY mr.project_key, mr.mr_iid
                """
            ).fetchall()
        observations = []
        for row in rows:
            gl = watcher.GitLab(
                str(row["host"]),
                token_for_project(self.bot_tokens, str(row["project_key"])),
            )
            mr = gl.get(f"projects/{row['project_id']}/merge_requests/{row['mr_iid']}")
            observations.append(
                (
                    str(mr.get("state") or "unknown"),
                    timestamp(mr.get("merged_at")),
                    time.time(),
                    row["project_key"],
                    int(row["mr_iid"]),
                )
            )
        if observations:
            with self.store.transaction() as connection:
                connection.executemany(
                    """
                    UPDATE merge_requests
                    SET state = ?, merged_at = ?, updated_at = ?
                    WHERE project_key = ? AND mr_iid = ?
                    """,
                    observations,
                )
        return len(observations)

    def schedule_due(self, now: float | None = None) -> int:
        checked_at = time.time() if now is None else now
        scheduled = 0
        with self.store.transaction() as connection:
            runs = connection.execute(
                """
                SELECT r.run_id, r.refs_json, r.result_json,
                       j.job_id, j.workspace_id, j.envelope_json,
                       c.conversation_key, c.current_job_id
                FROM runs AS r
                JOIN jobs AS j ON j.job_id = r.job_id
                JOIN conversations AS c ON c.conversation_key = j.conversation_key
                WHERE r.state = 'succeeded' AND j.state = 'succeeded'
                  AND c.current_job_id = j.job_id AND c.status = 'succeeded'
                """
            ).fetchall()
            for run in runs:
                envelope = json.loads(run["envelope_json"])
                if envelope.get("trigger_kind") != "agent::ready":
                    continue
                mrs = connection.execute(
                    """
                    SELECT state, merged_at
                    FROM merge_requests
                    WHERE job_id = ? AND run_id = ?
                    """,
                    (run["job_id"], run["run_id"]),
                ).fetchall()
                if not mrs or any(
                    mr["state"] != "merged" or mr["merged_at"] is None for mr in mrs
                ):
                    continue
                eligible_after = (
                    max(float(mr["merged_at"]) for mr in mrs) + MERGE_RETENTION_SECONDS
                )
                connection.execute(
                    """
                    INSERT INTO retention_records(run_id, eligible_after)
                    VALUES (?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET eligible_after = excluded.eligible_after
                    """,
                    (run["run_id"], eligible_after),
                )
                if eligible_after > checked_at:
                    continue
                refs = json.loads(run["refs_json"])
                result = json.loads(run["result_json"]) if run["result_json"] else {}
                payload = {
                    "job_id": run["job_id"],
                    "run_id": run["run_id"],
                    "conversation_key": run["conversation_key"],
                    "host": envelope["host"],
                    "project_path": envelope["project_path"],
                    "worktree_relpath": refs.get("worktree_relpath"),
                    "run_dir_relpath": refs.get("run_dir_relpath"),
                    "session_file_relpath": result.get("session_file_relpath"),
                }
                inserted = connection.execute(
                    """
                    INSERT OR IGNORE INTO workspace_actions(
                        action_id, workspace_id, kind, payload_json, created_at
                    ) VALUES (?, ?, 'cleanup-run', ?, ?)
                    """,
                    (
                        f"cleanup:{run['run_id']}",
                        run["workspace_id"],
                        json.dumps(payload, sort_keys=True),
                        checked_at,
                    ),
                ).rowcount
                scheduled += int(inserted == 1)
        return scheduled

    def run_once(self) -> int:
        refreshed = self.refresh_merge_requests()
        return refreshed + self.schedule_due()
