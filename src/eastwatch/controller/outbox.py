from __future__ import annotations

import json
import logging
import time
from contextlib import closing
from typing import Any

import requests

from eastwatch import watcher
from eastwatch.controller.store import ControllerStore
from eastwatch.controller.tokens import ProjectTokens, token_for_project

log = logging.getLogger(__name__)


class HostedOutbox:
    def __init__(self, store: ControllerStore, bot_tokens: ProjectTokens) -> None:
        self.store = store
        self.bot_tokens = bot_tokens

    def job_row(self, job_id: str) -> dict[str, Any]:
        with closing(self.store.connect()) as connection:
            row = connection.execute(
                """
                SELECT j.job_id, j.state, j.failure_stage, j.failure_reason,
                       j.envelope_json, c.conversation_key, c.issue_iid, c.current_job_id,
                       p.project_key, p.host, p.project_path, p.project_id,
                       w.owner_username, w.container_name, r.run_id, r.refs_json,
                       r.result_json, r.error_json
                FROM jobs AS j
                JOIN conversations AS c ON c.conversation_key = j.conversation_key
                JOIN projects AS p ON p.project_key = c.project_key
                JOIN workspaces AS w ON w.workspace_id = j.workspace_id
                LEFT JOIN runs AS r ON r.job_id = j.job_id
                WHERE j.job_id = ?
                ORDER BY r.started_at DESC
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"outbox references unknown job: {job_id}")
        return dict(row)

    @staticmethod
    def marker(outbox_id: int) -> str:
        return f"<!-- eastwatch: outbox={outbox_id} -->"

    @staticmethod
    def project(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "host": row["host"],
            "path": row["project_path"],
            "id": int(row["project_id"]),
            "forge": "gitlab",
        }

    def ensure_note(
        self,
        gl: watcher.GitLab,
        project: dict[str, Any],
        target: dict[str, Any],
        outbox_id: int,
        body: str,
    ) -> int:
        resource = "merge_requests" if target.get("kind") == "mr" else "issues"
        iid = int(target["iid"])
        marker = self.marker(outbox_id)
        path = f"projects/{project['id']}/{resource}/{iid}/notes"
        for page in range(1, watcher.DISCUSSION_LIST_PAGE_CAP + 1):
            notes = gl.get(path, sort="desc", per_page=100, page=page)
            for note in notes:
                if marker in str(note.get("body") or ""):
                    return int(note["id"])
            if len(notes) < 100:
                break
        else:
            raise RuntimeError(
                f"outbox marker lookup exceeded {watcher.DISCUSSION_LIST_PAGE_CAP} pages"
            )
        note_body = f"{body.rstrip()}\n\n{marker}"
        discussion_id = target.get("discussion_id")
        if discussion_id:
            try:
                note = gl.post(
                    f"projects/{project['id']}/{resource}/{iid}/discussions/{discussion_id}/notes",
                    body=note_body,
                )
                return int(note["id"])
            except requests.HTTPError as e:
                status_code = e.response.status_code if e.response is not None else None
                if status_code != 404:
                    raise
                log.warning(
                    "%s !%s: discussion %s no longer exists; falling back to top-level note",
                    resource,
                    iid,
                    discussion_id,
                )
        note = gl.post(path, body=note_body)
        return int(note["id"])

    @staticmethod
    def mr_iids(row: dict[str, Any], reply: str) -> tuple[int, ...]:
        project = {"path": row["project_path"]}
        return tuple(int(value) for value in watcher.parse_mr_iids(reply, project))

    @staticmethod
    def validate_mrs(
        gl: watcher.GitLab,
        project: dict[str, Any],
        row: dict[str, Any],
        mr_iids: tuple[int, ...],
        *,
        require_mr: bool,
    ) -> None:
        envelope = json.loads(row["envelope_json"])
        if (
            require_mr
            and envelope.get("trigger_kind") == "agent::ready"
            and not mr_iids
        ):
            raise ValueError("implementation run completed without an MR reference")
        for mr_iid in mr_iids:
            mr = watcher.fetch_mr(gl, project, str(mr_iid), raise_transient=True)
            if mr is None:
                raise ValueError(
                    f"MR !{mr_iid} does not exist in {row['project_path']}"
                )
            author = str((mr.get("author") or {}).get("username") or "")
            if author != row["owner_username"]:
                raise ValueError(
                    f"MR !{mr_iid} is authored by @{author or 'unknown'}, expected @{row['owner_username']}"
                )
            mapped_issue = watcher.parse_mr_marker(mr.get("description"), project)
            if mapped_issue != str(row["issue_iid"]):
                raise ValueError(f"MR !{mr_iid} has an invalid eastwatch source marker")

    def record_terminal(
        self,
        row: dict[str, Any],
        status: str,
        note_id: int,
        mr_iids: tuple[int, ...],
    ) -> None:
        now = time.time()
        session_refs: dict[str, Any] = {}
        if row.get("refs_json"):
            session_refs.update(json.loads(row["refs_json"]))
        if row.get("result_json"):
            result = json.loads(row["result_json"])
            for key in ("session_id", "session_file_relpath"):
                if result.get(key):
                    session_refs[key] = result[key]
        with self.store.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE conversations
                SET status = ?, session_refs_json = ?, updated_at = ?
                WHERE conversation_key = ? AND current_job_id = ?
                """,
                (
                    status,
                    json.dumps(session_refs, sort_keys=True),
                    now,
                    row["conversation_key"],
                    row["job_id"],
                ),
            ).rowcount
            if updated != 1:
                raise RuntimeError(f"job is no longer current: {row['job_id']}")
            for mr_iid in mr_iids:
                connection.execute(
                    """
                    INSERT INTO merge_requests(
                        project_key, mr_iid, conversation_key, job_id, run_id,
                        state, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'opened', ?)
                    ON CONFLICT(project_key, mr_iid, job_id, run_id) DO UPDATE SET
                        conversation_key = excluded.conversation_key,
                        job_id = excluded.job_id,
                        run_id = excluded.run_id,
                        state = excluded.state,
                        updated_at = excluded.updated_at
                    """,
                    (
                        row["project_key"],
                        mr_iid,
                        row["conversation_key"],
                        row["job_id"],
                        row["run_id"],
                        now,
                    ),
                )

    def completion_body(self, row: dict[str, Any]) -> tuple[str, str, tuple[int, ...]]:
        refs = json.loads(row["refs_json"]) if row.get("refs_json") else {}
        run_dir = refs.get("run_dir_relpath")
        tmux = refs.get("tmux_session")
        container = row["container_name"]
        commands = []
        if run_dir:
            commands.append(
                f"logs: `sudo docker exec {container} tail -f /home/bw/{run_dir}/run.jsonl`"
            )
        else:
            commands.append(
                f"pre-start error: `sudo docker exec {container} "
                f"cat /home/bw/state/prestart/{row['job_id']}/error.json`"
            )
        if tmux:
            commands.append(
                f"read-only attach: `sudo docker exec -it {container} tmux attach-session -r -t ={tmux}`"
            )
        inspect = "Inspect on the host: " + "; ".join(
            commands or [f"container: `{container}`"]
        )
        owner = f"Run owner: @{row['owner_username']}."
        if row["state"] == "succeeded" and row.get("result_json"):
            result = json.loads(row["result_json"])
            reply = str(result.get("reply") or "")
            body, worker_status, _had_status = watcher.split_status(reply)
            body = body.strip() or "(agent produced no reply text)"
            mrs = self.mr_iids(row, reply)
            envelope = json.loads(row["envelope_json"])
            if envelope.get("trigger_kind") != "agent::ready":
                # qa/research replies may reference other MRs in prose; only
                # implementation runs claim MRs as deliverables to validate,
                # label, and record.
                mrs = ()
            stats_footer = watcher.render_stats_footer(result.get("stats"))
            if worker_status == "parked":
                return (
                    f"{body}\n\n{owner}\n\n{inspect}{stats_footer}",
                    watcher.PARKED_LABEL,
                    mrs,
                )
            label = watcher.MR_READY_LABEL if mrs else watcher.FOR_HUMAN_LABEL
            return f"{body}\n\n{owner}\n\n{inspect}{stats_footer}", label, mrs
        stage = str(row.get("failure_stage") or "worker-error")
        reason = str(row.get("failure_reason") or "worker did not complete")[-1000:]
        body = (
            "⚠️ Hosted agent run failed and was parked for inspection.\n\n"
            f"Stage: `{stage}`\n\nReason: {reason}\n\n{owner}\n\n{inspect}"
        )
        return body, watcher.FAILED_LABEL, ()

    def deliver_job(self, item: dict[str, Any], job_id: str) -> None:
        row = self.job_row(job_id)
        if row["current_job_id"] != row["job_id"]:
            return
        project = self.project(row)
        gl = watcher.GitLab(
            str(row["host"]),
            token_for_project(self.bot_tokens, str(row["project_key"])),
        )
        body, label, mr_iids = self.completion_body(row)
        try:
            self.validate_mrs(
                gl,
                project,
                row,
                mr_iids,
                require_mr=(
                    row["state"] == "succeeded" and label != watcher.PARKED_LABEL
                ),
            )
        except ValueError as error:
            body = (
                "⚠️ Hosted run failed completion validation.\n\n"
                f"Reason: {error}\n\n{body}"
            )
            label = watcher.FAILED_LABEL
            mr_iids = ()
        reply_target = json.loads(row["envelope_json"]).get("reply_target") or {
            "kind": "issue",
            "iid": int(row["issue_iid"]),
        }
        note_id = self.ensure_note(
            gl,
            project,
            reply_target,
            int(item["outbox_id"]),
            body,
        )
        watcher.set_issue_agent_label(gl, project, str(row["issue_iid"]), label)
        terminal = (
            "parked"
            if label == watcher.PARKED_LABEL
            else "failed"
            if label == watcher.FAILED_LABEL
            else row["state"]
        )
        self.record_terminal(row, terminal, note_id, mr_iids)

    def deliver_started(self, job_id: str) -> None:
        row = self.job_row(job_id)
        if row["current_job_id"] != row["job_id"]:
            return
        if row["state"] not in ("leased", "running"):
            # Already terminal: the completion delivery's label supersedes.
            return
        project = self.project(row)
        gl = watcher.GitLab(
            str(row["host"]),
            token_for_project(self.bot_tokens, str(row["project_key"])),
        )
        envelope = json.loads(row["envelope_json"])
        label = (
            watcher.RESEARCHING_LABEL
            if envelope.get("trigger_kind") == "agent::ready-research"
            else watcher.WORKING_LABEL
        )
        watcher.set_issue_agent_label(gl, project, str(row["issue_iid"]), label)

    def deliver_rejection(self, item: dict[str, Any], payload: dict[str, Any]) -> None:
        project_key = str(payload["project_key"])
        with closing(self.store.connect()) as connection:
            row = connection.execute(
                """
                SELECT p.host, p.project_path, p.project_id,
                       j.state AS current_job_state, j.created_at AS current_job_created_at
                FROM projects AS p
                LEFT JOIN conversations AS c
                  ON c.project_key = p.project_key AND c.issue_iid = ?
                LEFT JOIN jobs AS j ON j.job_id = c.current_job_id
                WHERE p.project_key = ?
                """,
                (int(payload["issue_iid"]), project_key),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"unknown rejected project: {project_key}")
        current_state = row["current_job_state"]
        current_created_at = row["current_job_created_at"]
        if current_state in ("queued", "leased", "running") or (
            current_created_at is not None
            and float(current_created_at) > float(payload.get("rejected_at") or 0)
        ):
            return
        project = {
            "host": row["host"],
            "path": row["project_path"],
            "id": row["project_id"],
        }
        gl = watcher.GitLab(
            str(row["host"]),
            token_for_project(self.bot_tokens, project_key),
        )
        self.ensure_note(
            gl,
            project,
            {"kind": "issue", "iid": int(payload["issue_iid"])},
            int(item["outbox_id"]),
            f"⚠️ Hosted dispatch was not accepted.\n\nReason: {payload['reason']}",
        )
        if payload.get("set_failed_label", True):
            watcher.set_issue_agent_label(
                gl,
                project,
                str(payload["issue_iid"]),
                watcher.FAILED_LABEL,
            )

    def deliver(self, item: dict[str, Any]) -> None:
        payload = dict(item["payload"])
        if item["kind"] in ("job-completed", "runner-lost"):
            self.deliver_job(item, str(payload["job_id"]))
        elif item["kind"] == "job-started":
            self.deliver_started(str(payload["job_id"]))
        elif item["kind"] == "dispatch-rejected":
            self.deliver_rejection(item, payload)
        else:
            raise RuntimeError(f"unknown outbox kind: {item['kind']}")

    def run_once(self) -> int:
        items = self.store.claim_outbox(limit=20, lease_seconds=60)
        for item in items:
            outbox_id = int(item["outbox_id"])
            generation = int(item["lease_generation"])
            try:
                self.deliver(item)
            except Exception as error:  # noqa: BLE001 — retry durable side effects
                delay = min(300.0, float(2 ** min(int(item["attempt"]), 8)))
                self.store.mark_outbox_failed(
                    outbox_id,
                    generation,
                    str(error),
                    retry_at=time.time() + delay,
                )
            else:
                self.store.mark_outbox_delivered(outbox_id, generation)
        return len(items)
