"""eastwatch: a reconciling poller that turns GitLab board gestures into
headless claude/pi sessions.

One reconcile cycle per invocation; launchd re-invokes on an interval.
See RUNBOOK.md for operations.
"""

import concurrent.futures
import copy
import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import plistlib
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
import uuid
from pathlib import Path

import requests
import yaml

from eastwatch.collector import (
    LINE_BUFFER_BYTES,
    ClaudeStreamCollector,
    PiStreamCollector,
    RunJournal,
    StreamCollector,
)
from eastwatch.env import getenv
from eastwatch.paths import REPOSITORY_ROOT
from eastwatch.sessions import find_claude_session_file
from eastwatch.vault import (
    VAULT_DISPATCH_INTO,
    VAULT_DONE_STATUSES,
    VAULT_LABEL_TO_STATUS,
    VaultBoard,
)


def env_path(name: str, default: Path) -> Path:
    """Path from an env override, without touching the filesystem at import time."""
    raw = getenv(name)
    return Path(raw).expanduser() if raw else default


CONFIG_DIR = env_path("EASTWATCH_CONFIG_DIR", Path.home() / ".config/eastwatch")
CONFIG_PATH = env_path("EASTWATCH_CONFIG_PATH", CONFIG_DIR / "config.yaml")
STATE_DIR = env_path("EASTWATCH_STATE_DIR", Path.home() / ".local/state/eastwatch")
STATE_PATH = STATE_DIR / "state.json"
STATE_BAK_PATH = STATE_DIR / "state.json.bak"
LOG_DIR = env_path("EASTWATCH_LOG_DIR", STATE_DIR / "logs")
CONVOS_DIR = STATE_DIR / "convos"
CYCLE_LOCK = STATE_DIR / "cycle.lock"
PI_LOCK = STATE_DIR / "pi.lock"

_ZSHENV_PATH_LOADED = False
_ZSHENV_PATH_CACHE: str | None = None

TRIGGER_LABELS = ("agent::ready", "agent::ready-research")
WORKING_LABEL = "agent::working"
RESEARCHING_LABEL = "agent::researching"
PARKED_LABEL = "agent::parked"
MR_READY_LABEL = "agent::mr-ready"
FAILED_LABEL = "agent::failed"
FOR_HUMAN_LABEL = "agent::for-human"
# GitHub Projects v2 `Status` single-select option <-> shadow label. Bijective
# and FIXED by the pinned contract: on GitHub the canonical project's Status is
# authoritative and the only command channel; these `agent::*`/`triage::*` labels
# are a watcher-written searchable shadow, never a trigger. `triage::pending` and
# `triage::needs-info` are the two triage-scope shadow labels (Status Triage /
# Needs-info); the remaining eight are the agent lifecycle labels.
STATUS_TO_LABEL = {
    "Triage": "triage::pending",
    "Needs-info": "triage::needs-info",
    "Ready": TRIGGER_LABELS[0],
    "Ready-research": TRIGGER_LABELS[1],
    "Working": WORKING_LABEL,
    "Researching": RESEARCHING_LABEL,
    "Parked": PARKED_LABEL,
    "Review": MR_READY_LABEL,
    "Failed": FAILED_LABEL,
    "For-human": FOR_HUMAN_LABEL,
}
LABEL_TO_STATUS = {label: status for status, label in STATUS_TO_LABEL.items()}
# The full set the shadow writer clears together before adding the one mapped
# label (cross-scope full replacement: all `agent::*` + both `triage::*`).
SHADOW_LABELS = tuple(STATUS_TO_LABEL.values())
# Dispatch fires only on an observed transition INTO one of these Status options
# from one of GITHUB_DISPATCH_FROM (None == item previously tracked with no
# Status). A genuinely first-seen item is adopt-only regardless of its Status.
GITHUB_DISPATCH_INTO = {"Ready": TRIGGER_LABELS[0], "Ready-research": TRIGGER_LABELS[1]}
GITHUB_DISPATCH_FROM = frozenset({"Triage", "Needs-info", "Failed", None})
# Active (watcher is running the ticket) vs terminal (run finished / parked)
# lifecycle Status options. A terminal Status write must never replace a Status
# that is not one of the active ones — that would clobber a newer human drag.
GITHUB_ACTIVE_STATUSES = frozenset({"Working", "Researching"})
GITHUB_TERMINAL_STATUSES = frozenset({"Review", "Failed", "For-human", "Parked"})

APPROVE_EMOJI = {"white_check_mark", "thumbsup"}
APPROVAL_MESSAGE = "approved — proceed with your recommendation"

# GitLab's rich-text comment editor escapes markdown brackets, storing hints
# as \[pi:gpt-5.6-sol:medium\] — tolerate the backslashes.
HINT_RE = re.compile(r"\\?\[(claude|pi):([A-Za-z0-9._-]+?)(?::([a-z]+))?\\?\]")
STATUS_RE = re.compile(r"^\s*`?STATUS:\s*(done|parked)`?\s*$", re.IGNORECASE)
NOTE_GID_RE = re.compile(r"gid://gitlab/[A-Za-z]*Note/(\d+)")
MR_REF_RE = re.compile(r"(?:^|[\s(])!(\d+)\b")
MR_MARKER_RE = re.compile(
    r"<!--\s*(?:eastwatch|board-watcher):\s*(.*?)\s*-->",
    re.IGNORECASE | re.DOTALL,
)
MR_MARKER_FIELD_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^\s>]+)")
JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
JIRA_CUSTOM_FIELD_ID_RE = re.compile(r"customfield_\d+\Z")

PROTOCOL = (
    "Your final reply will be posted verbatim as a GitLab comment on the issue. "
    "Never post comments on the watched issue or any merge request yourself; return text here and the watcher posts it. "
    "Only your single final message is saved and posted — earlier messages you write while working are NOT posted, so put your "
    "COMPLETE answer in your final message; do not refer to an answer you wrote 'above' in an earlier message. "
    "End your reply with exactly one status line: `STATUS: done` if complete, or "
    "`STATUS: parked` if you need input — in which case state one concrete question "
    "AND your recommended default so a ✅ can approve it."
)
CHARTER_COMMON = (
    "Autonomy: you are unsupervised — no human sees your intermediate work and nobody will answer mid-run questions. "
    "Work the issue end-to-end. Park (STATUS: parked) only when a decision genuinely requires the owner, not for anything "
    "you can verify or decide yourself. Structure your own working loop to fit the task, and use the skills your harness "
    "provides (e.g. `/wf` under Pi) when the task warrants that rigor. You are a leaf worker in a managed fleet: never "
    "launch another agent CLI (`claude`, `pi`, `codex`, …) as a subprocess and never invoke orchestration skills, even if "
    "a skill's instructions suggest it — delegation decisions belong to the dispatcher, and cross-CLI children are "
    "invisible to the fleet's observability."
)
CHARTER_WORK = (
    "Before `finish`, verify your change (`/verify` if available, otherwise the repo's test suite) and state in your final "
    "reply what you ran and what it showed. If the card turns out to be too big for one context window, park with a proposed "
    "split — the board is how work fans out, not subagents."
)
CHARTER_RESEARCH = (
    "For broad sweeps, parallel read-only fan-out through your harness's native subagent mechanism is appropriate; merge "
    "all findings into your single resolution comment. Use pi-subagents conservatively — child teardown has a known "
    "upstream leak."
)

AWARDS_QUERY = """
query {
  project(fullPath: "%s") {
    workItems(state: opened, first: 100, sort: UPDATED_DESC) {
      nodes {
        iid
        widgets {
          ... on WorkItemWidgetAwardEmoji {
            awardEmoji(first: 20) { nodes { name user { username } } }
          }
          ... on WorkItemWidgetNotes {
            discussions(first: 50) {
              nodes {
                notes(first: 50) {
                  nodes {
                    id
                    system
                    awardEmoji(first: 20) { nodes { name user { username } } }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

log = logging.getLogger("eastwatch")


class SessionError(Exception):
    pass


class TransientMRFetchError(Exception):
    pass


class TransientDiscussionLookupError(Exception):
    pass


class StatePersistenceError(Exception):
    pass


class ConfigurationError(Exception):
    pass


RUN_TIMEOUT_SECONDS = 3 * 60 * 60
TERM_GRACE_SECONDS = 15
PI_LAUNCH_LOCK_SECONDS = 30
PI_SETTLED_EXIT_GRACE_SECONDS = 20
SUCCESS_TAIL_RETENTION_SECONDS = 7 * 24 * 60 * 60
FAILURE_TAIL_RETENTION_SECONDS = 30 * 24 * 60 * 60
ORPHAN_RUN_RETENTION_SECONDS = 30 * 24 * 60 * 60
RAW_CAPTURE_RETENTION_SECONDS = 14 * 24 * 60 * 60
SWEEP_INTERVAL_SECONDS = 24 * 60 * 60
DISCUSSION_LIST_PER_PAGE = 100
DISCUSSION_LIST_PAGE_CAP = 20


# --------------------------------------------------------------------------- gitlab


class GitLab:
    def __init__(self, host: str, token: str):
        self.base = f"https://{host}/api/v4"
        self.graphql_url = f"https://{host}/api/graphql"
        self.token = token
        self.s = requests.Session()
        self.s.headers["PRIVATE-TOKEN"] = token

    def get(self, path: str, **params):
        r = self.s.get(f"{self.base}/{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, **data):
        r = self.s.post(f"{self.base}/{path}", data=data, timeout=30)
        r.raise_for_status()
        return r.json()

    def put(self, path: str, **data):
        r = self.s.put(f"{self.base}/{path}", data=data, timeout=30)
        r.raise_for_status()
        return r.json()

    def graphql(self, query: str):
        r = requests.post(
            self.graphql_url,
            json={"query": query},
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=30,
        )
        r.raise_for_status()
        payload = r.json()
        if payload.get("errors"):
            raise RuntimeError(f"graphql errors: {payload['errors']}")
        return payload["data"]


def keychain_token(service: str, account: str) -> str:
    r = subprocess.run(
        ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout.strip()


# --------------------------------------------------------------------------- github

# One paginated page of the canonical project's items. Per the pinned wire
# contract this recurring scan selects ONLY the poller's fields: item node id +
# `updatedAt` (the observation generation), the linked content's id/number/state,
# and the item's `Status` name+optionId. Issue title/url/body are deliberately
# NOT fetched here — they are hydrated once, on dispatch (GITHUB_ISSUE_HYDRATE_QUERY),
# to keep the per-tick rate cost bounded. PRs, draft issues and unlinked items
# are skipped by the poller.
GITHUB_ITEMS_QUERY = """
query($project: ID!, $cursor: String) {
  rateLimit { cost remaining }
  node(id: $project) {
    ... on ProjectV2 {
      items(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          updatedAt
          fieldValueByName(name: "Status") {
            ... on ProjectV2ItemFieldSingleSelectValue { name optionId }
          }
          content {
            __typename
            ... on Issue { id number state }
          }
        }
      }
    }
  }
}
"""

# Issue title/url/body for the launch prompt — fetched only when a dispatch
# actually fires, not on every recurring scan.
GITHUB_ISSUE_HYDRATE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) { number title url body }
  }
}
"""

# The canonical project's `Status` field schema (field id + option ids). Renamed
# or deleted schema must fail closed, so every option id we write is validated
# against this before use.
GITHUB_STATUS_FIELD_QUERY = """
query($project: ID!) {
  node(id: $project) {
    ... on ProjectV2 {
      field(name: "Status") {
        ... on ProjectV2SingleSelectField {
          id
          options { id name }
        }
      }
    }
  }
}
"""

# One issue's membership in the canonical project (item id if present). Used by
# the Status writers and prune recovery to resolve/fence an item cheaply without
# paginating the whole board.
GITHUB_ISSUE_ITEM_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      id
      state
      projectItems(first: 20) {
        nodes { id project { id } }
      }
    }
  }
}
"""

# One item's current Status by node id — a targeted read for the shadow writer's
# pre-write recheck, so recheck costs one cheap query instead of re-paginating.
GITHUB_ITEM_STATUS_QUERY = """
query($item: ID!) {
  node(id: $item) {
    ... on ProjectV2Item {
      fieldValueByName(name: "Status") {
        ... on ProjectV2ItemFieldSingleSelectValue { name optionId }
      }
    }
  }
}
"""


class GitHubGraphQLError(RuntimeError):
    """A `gh api graphql` call returned a GraphQL `errors` payload."""


class GitHubProject:
    """Shells to ``gh`` for one canonical GitHub Projects v2 board.

    Mirrors how the watcher already shells to ``glab``: every remote call is a
    subprocess, transient failures retry with backoff, and the ``Status`` field
    schema is validated (fail closed) before any option id is written. This
    object is the single seam through which the poller, shadow writer and Status
    writers reach GitHub — nothing else in the watcher constructs ``gh`` argv.
    """

    def __init__(
        self,
        project_id: str,
        repo: str,
        *,
        gh_bin: str = "gh",
        max_retries: int = 3,
        backoff_base: float = 1.0,
    ):
        self.project_id = project_id
        self.repo = repo  # "owner/name"
        owner, _, name = repo.partition("/")
        self.owner = owner
        self.name = name
        self.gh_bin = gh_bin
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._status_field_id: str | None = None
        self._status_options: dict[str, str] | None = None  # option name -> id
        self.last_rate_cost = 0

    # -- subprocess plumbing -------------------------------------------------

    def _run(self, args: list[str], *, input_text: str | None = None) -> str:
        """Run ``gh`` with retry/backoff; return stdout, raise on final failure."""
        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                proc = subprocess.run(
                    [self.gh_bin, *args],
                    capture_output=True,
                    text=True,
                    input=input_text,
                    timeout=60,
                )
            except (OSError, subprocess.TimeoutExpired) as e:
                last_err = e
            else:
                if proc.returncode == 0:
                    return proc.stdout
                last_err = GitHubGraphQLError(
                    f"gh {' '.join(args[:2])} exited {proc.returncode}: "
                    f"{command_tail(proc.stdout, proc.stderr)}"
                )
            if attempt < self.max_retries:
                time.sleep(self.backoff_base * (2 ** (attempt - 1)))
        assert last_err is not None
        raise last_err

    def graphql(self, query: str, **variables) -> dict:
        args = ["api", "graphql", "-f", f"query={query}"]
        for key, value in variables.items():
            if value is None:
                continue  # omit -> the GraphQL variable defaults to null (e.g. first-page cursor)
            # -F coerces ints/bools/null; -f is a raw string. GraphQL numeric
            # vars (issue number) must go through -F.
            flag = "-F" if isinstance(value, (int, bool)) else "-f"
            args.extend([flag, f"{key}={value}"])
        payload = json.loads(self._run(args))
        if payload.get("errors"):
            raise GitHubGraphQLError(f"graphql errors: {payload['errors']}")
        data = payload.get("data") or {}
        rate = data.get("rateLimit") or {}
        if isinstance(rate.get("cost"), int):
            self.last_rate_cost = rate["cost"]
        return data

    # -- Status field schema (fail closed) -----------------------------------

    def load_status_schema(self) -> None:
        if self._status_field_id is not None:
            return
        data = self.graphql(GITHUB_STATUS_FIELD_QUERY, project=self.project_id)
        field = ((data.get("node") or {}).get("field")) or {}
        field_id = field.get("id")
        options = {o["name"]: o["id"] for o in field.get("options") or []}
        if not field_id or not options:
            raise ConfigurationError(
                f"github project {self.project_id}: no single-select `Status` field found; "
                "run `glab-board setup --board` to (re)provision the canonical project"
            )
        # Fail closed on the EXACT fixed contract: every one of the ten pinned
        # option names must be present. A renamed or deleted lifecycle option
        # would otherwise become an unmapped Status and make the shadow writer
        # strip every label. Extra options (e.g. Closed) are allowed.
        missing = set(STATUS_TO_LABEL) - set(options)
        if missing:
            raise ConfigurationError(
                f"github project {self.project_id}: `Status` field is missing required "
                f"option(s) {sorted(missing)} (have {sorted(options)}); the fixed "
                "Status contract changed — run `glab-board setup --board`"
            )
        self._status_field_id = field_id
        self._status_options = options

    @property
    def status_field_id(self) -> str:
        self.load_status_schema()
        assert self._status_field_id is not None
        return self._status_field_id

    def status_option_id(self, status_name: str) -> str:
        self.load_status_schema()
        assert self._status_options is not None
        option_id = self._status_options.get(status_name)
        if not option_id:
            raise ConfigurationError(
                f"github project {self.project_id}: `Status` option {status_name!r} is missing "
                f"(have {sorted(self._status_options)}); run `glab-board setup --board`"
            )
        return option_id

    def status_name_for_option(self, option_id: str | None) -> str | None:
        if not option_id:
            return None
        self.load_status_schema()
        assert self._status_options is not None
        for name, oid in self._status_options.items():
            if oid == option_id:
                return name
        raise ConfigurationError(
            f"github project {self.project_id}: item carries unknown Status optionId {option_id!r}; "
            "schema changed — run `glab-board setup --board`"
        )

    # -- reads ---------------------------------------------------------------

    def fetch_items(self) -> list[dict]:
        """All open, issue-backed items with their Status, paginated fully.

        Returns observation dicts. PRs, draft issues, unlinked and closed
        content are skipped; the Status optionId is validated against the
        schema so a renamed/deleted option fails closed.
        """
        self.load_status_schema()
        items: list[dict] = []
        cursor: str | None = None
        while True:
            data = self.graphql(
                GITHUB_ITEMS_QUERY, project=self.project_id, cursor=cursor
            )
            container = ((data.get("node") or {}).get("items")) or {}
            for node in container.get("nodes") or []:
                content = node.get("content") or {}
                if content.get("__typename") != "Issue":
                    continue  # skip PRs, draft issues, null content
                if (content.get("state") or "").upper() == "CLOSED":
                    continue
                status = node.get("fieldValueByName") or {}
                option_id = status.get("optionId")
                status_name = self.status_name_for_option(option_id)
                items.append(
                    {
                        "item_id": node["id"],
                        "updated_at": node.get("updatedAt"),
                        "content_id": content.get("id"),
                        "number": content.get("number"),
                        "state": content.get("state"),
                        "status_name": status_name,
                        "option_id": option_id,
                    }
                )
            page = container.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")
        return items

    def hydrate_issue(self, number: int) -> dict:
        """Issue title/url/body for the launch prompt — one query, on dispatch only."""
        data = self.graphql(
            GITHUB_ISSUE_HYDRATE_QUERY,
            owner=self.owner,
            name=self.name,
            number=int(number),
        )
        issue = ((data.get("repository") or {}).get("issue")) or {}
        return {
            "title": issue.get("title") or "",
            "url": issue.get("url"),
            "body": issue.get("body") or "",
        }

    def issue_item(self, number: int) -> dict:
        """Resolve this issue's item id in the canonical project (fenced add).

        Returns ``{"item_id", "content_id", "state"}``; ``item_id`` is None when
        the issue exists but is not yet a member of the project.
        """
        data = self.graphql(
            GITHUB_ISSUE_ITEM_QUERY,
            owner=self.owner,
            name=self.name,
            number=int(number),
        )
        issue = ((data.get("repository") or {}).get("issue")) or {}
        content_id = issue.get("id")
        item_id = None
        for node in (issue.get("projectItems") or {}).get("nodes") or []:
            if (node.get("project") or {}).get("id") == self.project_id:
                item_id = node.get("id")
                break
        return {
            "item_id": item_id,
            "content_id": content_id,
            "state": issue.get("state"),
        }

    def item_status_name(self, item_id: str) -> str | None:
        """Single item's current Status name (pre-write recheck / read-back).

        One targeted node query — not a full re-pagination — so the shadow
        writer's recheck stays cheap against the rate budget.
        """
        data = self.graphql(GITHUB_ITEM_STATUS_QUERY, item=item_id)
        status = (data.get("node") or {}).get("fieldValueByName") or {}
        return self.status_name_for_option(status.get("optionId"))

    def issue_labels(self, number: int) -> list[str]:
        out = self._run(
            ["issue", "view", str(number), "--repo", self.repo, "--json", "labels"]
        )
        data = json.loads(out or "{}")
        return [lbl["name"] for lbl in data.get("labels") or []]

    # -- writes --------------------------------------------------------------

    def add_item(self, content_id: str) -> str:
        """Add an issue to the canonical project; returns the (new) item id.

        Idempotent: ``addProjectV2ItemById`` returns the existing item when the
        content is already a member.
        """
        mutation = (
            "mutation($project: ID!, $content: ID!) {"
            " addProjectV2ItemById(input: {projectId: $project, contentId: $content})"
            " { item { id } } }"
        )
        data = self.graphql(mutation, project=self.project_id, content=content_id)
        return (((data.get("addProjectV2ItemById") or {}).get("item")) or {}).get("id")

    def set_status(self, item_id: str, status_name: str) -> None:
        option_id = self.status_option_id(status_name)
        mutation = (
            "mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {"
            " updateProjectV2ItemFieldValue(input: {projectId: $project, itemId: $item,"
            " fieldId: $field, value: {singleSelectOptionId: $option}}) { projectV2Item { id } } }"
        )
        self.graphql(
            mutation,
            project=self.project_id,
            item=item_id,
            field=self.status_field_id,
            option=option_id,
        )

    def set_status_fenced(
        self, item_id: str, status_name: str, *, attempts: int = 3, delay: float = 1.0
    ) -> bool:
        """Write Status and confirm it by read-back, re-applying if a workflow reverts it.

        Fences the Item-added->Triage project workflow (and any lost race): after
        each write we re-read the live Status and retry until it sticks or the
        attempt budget is exhausted. Returns True once read-back matches.
        """
        for attempt in range(1, attempts + 1):
            self.set_status(item_id, status_name)
            if self.item_status_name(item_id) == status_name:
                return True
            if attempt < attempts:
                time.sleep(delay)
        return False

    def set_labels(self, number: int, add=(), remove=()) -> None:
        args = ["issue", "edit", str(number), "--repo", self.repo]
        for label in add:
            args.extend(["--add-label", label])
        for label in remove:
            args.extend(["--remove-label", label])
        if len(args) > 4:
            self._run(args)

    def post_comment(self, number: int, body: str) -> dict:
        out = self._run(
            ["issue", "comment", str(number), "--repo", self.repo, "--body-file", "-"],
            input_text=body,
        )
        return {"id": (out or "").strip()}


# --------------------------------------------------------------------------- state


def validate_project_poll_cursors(
    state: dict,
    *,
    state_path: Path = STATE_PATH,
    state_bak_path: Path = STATE_BAK_PATH,
) -> None:
    projects = state.get("projects")
    if not isinstance(projects, dict):
        return
    for key, ps in projects.items():
        if not isinstance(ps, dict) or "last_event_id" not in ps:
            continue
        cursor = ps["last_event_id"]
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise StatePersistenceError(
                f"projects[{key!r}].last_event_id must be a non-negative integer, got {cursor!r}; "
                f"refusing to poll. Restore {state_bak_path} or edit {state_path}, then retry."
            )


def load_state() -> dict:
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text())
        validate_project_poll_cursors(state)
        return state
    return {"projects": {}}


def project_count(state: dict) -> int:
    projects = state.get("projects")
    return len(projects) if isinstance(projects, dict) else 0


def on_disk_project_count() -> int:
    if not STATE_PATH.exists():
        return 0
    return project_count(json.loads(STATE_PATH.read_text()))


def save_state(state: dict) -> None:
    if project_count(state) == 0:
        try:
            existing_projects = on_disk_project_count()
        except (json.JSONDecodeError, OSError) as e:
            log.error(
                "refusing to overwrite unreadable state file %s with zero-project state: %s",
                STATE_PATH,
                e,
            )
            raise StatePersistenceError(
                "refusing to overwrite unreadable state with zero-project state"
            ) from e
        if existing_projects > 0:
            log.error(
                "refusing to overwrite state file %s containing %d project(s) with zero-project state; "
                "leaving existing state intact (last backup: %s)",
                STATE_PATH,
                existing_projects,
                STATE_BAK_PATH,
            )
            raise StatePersistenceError(
                "refusing to overwrite state containing projects with zero-project state"
            )

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_name(f".{STATE_PATH.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2) + "\n")
        if STATE_PATH.exists():
            shutil.copy2(STATE_PATH, STATE_BAK_PATH)
        tmp.replace(STATE_PATH)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def project_state(state: dict, key: str) -> dict:
    ps = state["projects"].setdefault(
        key,
        {
            "bootstrapped": False,
            "last_event_id": 0,
            "consumed_label_event_ids": [],
            "award_keys": [],
            "conversations": {},
            "mr_index": {},
        },
    )
    # State files predate some keys; add them lazily instead of requiring a
    # manual migration while the launchd watcher is live.
    ps.setdefault("bootstrapped", False)
    ps.setdefault("last_event_id", 0)
    ps.setdefault("consumed_label_event_ids", [])
    ps.setdefault("award_keys", [])
    ps.setdefault("conversations", {})
    ps.setdefault("mr_index", {})
    ps.setdefault("pending_mr_comment_gestures", [])
    # GitHub Status-first projects only; empty and untouched for GitLab.
    # github_observations: item_id -> last observed generation record.
    # github_outbox: generation string -> durable dispatch command.
    ps.setdefault("github_observations", {})
    ps.setdefault("github_outbox", {})
    # Vault board projects only; empty and untouched for GitLab/GitHub.
    # vault_observations: item_id -> last observed status generation record.
    # vault_outbox: generation string -> durable dispatch command.
    ps.setdefault("vault_observations", {})
    ps.setdefault("vault_outbox", {})
    for conv_key, conv in ps["conversations"].items():
        conv.setdefault("anchor", "issue")
        if conv.get("anchor") == "issue":
            conv.setdefault("issue_iid", str(conv_key))
        conv.setdefault("mr_iids", [])
        conv.setdefault("current_run", None)
        conv.setdefault("last_run", None)
        conv.setdefault("reply_target", None)
        conv.setdefault("next_reply_target", None)
    return ps


# --------------------------------------------------------------------------- polling


def note_position_label(position: dict | None) -> str | None:
    if not position:
        return None
    new_path, new_line = position.get("new_path"), position.get("new_line")
    old_path, old_line = position.get("old_path"), position.get("old_line")
    new_label = f"{new_path}:{new_line}" if new_path and new_line is not None else None
    old_label = f"{old_path}:{old_line}" if old_path and old_line is not None else None
    if old_label and new_label and old_label != new_label:
        return f"{old_label} -> {new_label}"
    return new_label or old_label


def mr_resume_body(mr_iid: str, note: dict) -> str:
    body = note.get("body") or ""
    location = note_position_label(note.get("position"))
    if location:
        return f"Owner commented on merge request !{mr_iid} at `{location}` (line-level diff note):\n\n{body}"
    return f"Owner commented on merge request !{mr_iid}:\n\n{body}"


def agent_mention_question(body: str) -> str | None:
    stripped = (body or "").strip()
    if not stripped.lower().startswith("@agent"):
        return None
    return stripped[len("@agent") :].strip() or "(no question text)"


def note_reply_fields(target: dict, gesture: dict | None) -> dict:
    if not gesture:
        return target
    if gesture.get("note_id") is not None:
        target["note_id"] = gesture.get("note_id")
    if gesture.get("discussion_id"):
        target["discussion_id"] = gesture.get("discussion_id")
    return target


def issue_reply_target(iid: str, gesture: dict | None = None) -> dict:
    return note_reply_fields({"kind": "issue", "issue_iid": str(iid)}, gesture)


def mr_reply_target(mr_iid: str, gesture: dict | None = None) -> dict:
    return note_reply_fields({"kind": "mr", "mr_iid": str(mr_iid)}, gesture)


def discussion_path(proj: dict, surface: str, iid: str | int) -> str:
    resource = "merge_requests" if surface == "mr" else "issues"
    return f"projects/{proj['id']}/{resource}/{iid}/discussions"


def discussion_detail_path(
    proj: dict, surface: str, iid: str | int, discussion_id: str | int
) -> str:
    return f"{discussion_path(proj, surface, iid)}/{discussion_id}"


def noteable_state_from_event(event: dict, note: dict) -> str | None:
    for container in (
        note.get("noteable"),
        note.get("issue"),
        note.get("merge_request"),
        event.get("target"),
        event.get("issue"),
        event.get("merge_request"),
    ):
        if isinstance(container, dict) and container.get("state"):
            return str(container["state"])
    if event.get("state"):
        return str(event["state"])
    return None


def discussion_by_id(
    gl: GitLab,
    proj: dict,
    surface: str,
    iid: str | int,
    discussion_id: str | None,
) -> dict | None:
    if not discussion_id:
        return None
    try:
        return gl.get(discussion_detail_path(proj, surface, iid, discussion_id))
    except requests.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else None
        if status_code == 404:
            log.warning(
                "%s !%s: discussion %s lookup returned 404", surface, iid, discussion_id
            )
            return None
        log.warning(
            "%s !%s: could not fetch discussion %s: %s", surface, iid, discussion_id, e
        )
        raise TransientDiscussionLookupError(
            f"{surface} !{iid} discussion {discussion_id} lookup failed: {e}"
        ) from e
    except requests.RequestException as e:
        log.warning(
            "%s !%s: could not fetch discussion %s: %s", surface, iid, discussion_id, e
        )
        raise TransientDiscussionLookupError(
            f"{surface} !{iid} discussion {discussion_id} lookup failed: {e}"
        ) from e


def discussion_has_bot_note(
    gl: GitLab, proj: dict, surface: str, iid: str | int, discussion_id: str | None
) -> bool:
    bot_user_id = proj.get("bot_user_id")
    if bot_user_id is None:
        return False
    discussion = discussion_by_id(gl, proj, surface, iid, discussion_id)
    if discussion is None:
        return False
    return any(
        str((note.get("author") or {}).get("id")) == str(bot_user_id)
        for note in discussion.get("notes") or []
    )


def find_note_discussion_id(
    gl: GitLab,
    proj: dict,
    surface: str,
    iid: str | int,
    note_id: str | int | None,
    discussion_id: str | None = None,
) -> str | None:
    if not note_id and not discussion_id:
        return None
    note_id = str(note_id) if note_id else None
    discussion_id = str(discussion_id) if discussion_id else None
    if discussion_id:
        discussion = discussion_by_id(gl, proj, surface, iid, discussion_id)
        if discussion is None or discussion.get("individual_note") is True:
            return None
        return str(discussion.get("id") or discussion_id)

    path = discussion_path(proj, surface, iid)
    try:
        for page in range(1, DISCUSSION_LIST_PAGE_CAP + 1):
            discussions = gl.get(path, per_page=DISCUSSION_LIST_PER_PAGE, page=page)
            for discussion in discussions:
                note_matches = note_id and any(
                    str(note.get("id")) == note_id
                    for note in discussion.get("notes") or []
                )
                if not note_matches:
                    continue
                if discussion.get("individual_note") is True:
                    return None
                return discussion.get("id")
            if len(discussions) < DISCUSSION_LIST_PER_PAGE:
                return None
        log.warning(
            "%s !%s: discussion list scan for note %s reached page cap %d; result may be truncated",
            surface,
            iid,
            note_id,
            DISCUSSION_LIST_PAGE_CAP,
        )
        return None
    except requests.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else None
        if status_code == 404:
            log.warning(
                "%s !%s: discussions lookup for note %s returned 404",
                surface,
                iid,
                note_id,
            )
            return None
        log.warning(
            "%s !%s: could not fetch discussions for note %s: %s",
            surface,
            iid,
            note_id,
            e,
        )
        raise TransientDiscussionLookupError(
            f"{surface} !{iid} discussions lookup for note {note_id} failed: {e}"
        ) from e
    except requests.RequestException as e:
        log.warning(
            "%s !%s: could not fetch discussions for note %s: %s",
            surface,
            iid,
            note_id,
            e,
        )
        raise TransientDiscussionLookupError(
            f"{surface} !{iid} discussions lookup for note {note_id} failed: {e}"
        ) from e


def note_discussion_id(
    gl: GitLab, proj: dict, surface: str, iid: str | int, note: dict
) -> str | None:
    return find_note_discussion_id(
        gl, proj, surface, iid, note.get("id"), note.get("discussion_id")
    )


def capture_note_discussion_id(
    gl: GitLab, proj: dict, surface: str, iid: str | int, note: dict
) -> str | None:
    try:
        return note_discussion_id(gl, proj, surface, iid, note)
    except TransientDiscussionLookupError as e:
        log.warning(
            "%s !%s note %s: discussion lookup failed during comment capture; capturing unthreaded: %s",
            surface,
            iid,
            note.get("id"),
            e,
        )
        return None


def mr_comment_context(mr: dict, gesture: dict, question: str | None = None) -> str:
    mr_iid = str(mr.get("iid") or gesture.get("mr_iid") or "")
    lines = [
        f"Owner commented on merge request !{mr_iid}.",
        "",
        f"MR title: {mr.get('title') or '(untitled)'}",
        f"MR URL: {mr.get('web_url') or '(unknown)'}",
        f"Source branch: {mr.get('source_branch') or '(unknown)'}",
        f"Target branch: {mr.get('target_branch') or '(unknown)'}",
    ]
    description = (mr.get("description") or "").strip()
    if description:
        lines.extend(["", "MR description:", "", description[:6000]])
    location = note_position_label(gesture.get("position"))
    label = "Comment"
    if location:
        label += f" at `{location}` (line-level diff note)"
    body = question if question is not None else (gesture.get("comment_body") or "")
    lines.extend(["", f"{label}:", "", body or "(no comment text)"])
    return "\n".join(lines)


def poll_comments(gl: GitLab, proj: dict, ps: dict, owner: str) -> list[dict]:
    """Events API comment stream -> owner issue/MR comment gestures, cursor advanced."""
    events = gl.get(f"projects/{proj['id']}/events", action="commented", per_page=100)
    fresh = sorted(
        (e for e in events if e["id"] > ps["last_event_id"]), key=lambda e: e["id"]
    )
    if not ps["bootstrapped"]:
        if events:
            ps["last_event_id"] = max(e["id"] for e in events)
        log.info(
            "bootstrap: comment cursor set to %s (%d pre-existing events adopted, none processed)",
            ps["last_event_id"],
            len(fresh),
        )
        return []
    gestures = []
    for e in fresh:
        ps["last_event_id"] = max(ps["last_event_id"], e["id"])
        note = e.get("note") or {}
        author = e.get("author") or {}
        if (
            author.get("id") == proj["bot_user_id"]
            or author.get("username") == proj["bot_username"]
        ):
            continue  # our own bot comments
        if note.get("system"):
            continue
        if author.get("username") != owner:
            continue
        noteable_type = note.get("noteable_type")
        if noteable_type == "Issue":
            iid = str(note["noteable_iid"])
            gestures.append(
                {
                    "kind": "issue",
                    "iid": iid,
                    "body": note.get("body") or "",
                    "discussion_id": capture_note_discussion_id(
                        gl, proj, "issue", iid, note
                    ),
                    "note_id": note.get("id"),
                    "event_id": e["id"],
                    "issue_state": noteable_state_from_event(e, note),
                }
            )
            log.info(
                "comment gesture: issue !%s note %s by %s", iid, note.get("id"), owner
            )
        elif noteable_type == "MergeRequest":
            mr_iid = str(note["noteable_iid"])
            gestures.append(
                {
                    "kind": "mr",
                    "mr_iid": mr_iid,
                    "body": mr_resume_body(mr_iid, note),
                    "comment_body": note.get("body") or "",
                    "position": note.get("position"),
                    "discussion_id": capture_note_discussion_id(
                        gl, proj, "mr", mr_iid, note
                    ),
                    "note_id": note.get("id"),
                    "event_id": e["id"],
                    "mr_state": noteable_state_from_event(e, note),
                }
            )
            log.info(
                "comment gesture: merge request !%s note %s by %s",
                mr_iid,
                note.get("id"),
                owner,
            )
    return gestures


def poll_label(gl: GitLab, proj: dict, ps: dict, label: str) -> list[dict]:
    """Label-filtered issue list + per-issue resource_label_events watermark."""
    issues = gl.get(
        f"projects/{proj['id']}/issues", labels=label, state="opened", per_page=100
    )
    consumed = set(ps["consumed_label_event_ids"])
    fired = []
    for iss in issues:
        evs = gl.get(
            f"projects/{proj['id']}/issues/{iss['iid']}/resource_label_events",
            per_page=100,
        )
        adds = [
            ev
            for ev in evs
            if ev.get("action") == "add"
            and (ev.get("label") or {}).get("name") == label
        ]
        if not adds:
            continue
        newest = max(adds, key=lambda ev: ev["id"])
        if newest["id"] in consumed:
            continue
        consumed.add(newest["id"])
        if not ps["bootstrapped"]:
            log.info(
                "bootstrap: adopted pre-existing `%s` on issue !%s (label event %s) — "
                "NOT dispatched; re-add the label to trigger",
                label,
                iss["iid"],
                newest["id"],
            )
            continue
        log.info(
            "label gesture: `%s` on issue !%s (label event %s)",
            label,
            iss["iid"],
            newest["id"],
        )
        fired.append({"issue": iss, "label": label})
    ps["consumed_label_event_ids"] = sorted(consumed)[-1000:]
    return fired


def poll_awards(gl: GitLab, proj: dict, ps: dict) -> set[tuple[str, str, str]]:
    """One GraphQL snapshot of all awards on open work items, diffed vs state."""
    data = gl.graphql(AWARDS_QUERY % proj["path"])
    keys: set[tuple[str, str, str]] = set()
    for wi in data["project"]["workItems"]["nodes"]:
        for widget in wi["widgets"]:
            for a in (widget.get("awardEmoji") or {"nodes": []})["nodes"]:
                keys.add((f"wi:{wi['iid']}", a["name"], a["user"]["username"]))
            for disc in (widget.get("discussions") or {"nodes": []})["nodes"]:
                for n in disc["notes"]["nodes"]:
                    m = NOTE_GID_RE.match(n.get("id") or "")
                    if not m:
                        continue
                    for a in (n.get("awardEmoji") or {"nodes": []})["nodes"]:
                        keys.add(
                            (f"note:{m.group(1)}", a["name"], a["user"]["username"])
                        )
    prev = {tuple(k) for k in ps["award_keys"]}
    ps["award_keys"] = sorted(list(k) for k in keys)
    if not ps["bootstrapped"]:
        log.info("bootstrap: award snapshot recorded (%d keys), none fired", len(keys))
        return set()
    return keys - prev


# --------------------------------------------------------------------------- github status poller


def github_generation(project_id: str, item: dict) -> str:
    """Observation generation key `(project, item, option, updatedAt)`.

    Any Status change or item touch advances `updatedAt`, so a new key means a
    new observation to (re)consider; an unchanged key is skipped.
    """
    return f"{project_id}|{item['item_id']}|{item.get('option_id') or ''}|{item.get('updated_at') or ''}"


def github_issue_from_item(client: GitHubProject, item: dict) -> dict:
    """The issue shape `make_conversation`/`assemble` expect, hydrated on dispatch.

    The recurring scan doesn't carry issue text (rate budget); we fetch
    title/url/body here, only for the item that actually dispatched.
    """
    number = item["number"]
    try:
        hydrated = client.hydrate_issue(number)
    except Exception as e:  # noqa: BLE001 — text is best-effort, never blocks dispatch
        log.warning("github: issue #%s hydrate failed: %s", number, e)
        hydrated = {}
    return {
        "iid": number,
        "title": hydrated.get("title") or "",
        "web_url": hydrated.get("url"),
        "description": hydrated.get("body") or "",
    }


def poll_github_status(
    client: GitHubProject, proj: dict, ps: dict, items: list[dict], triggers=None
) -> dict:
    """Diff this tick's items against recorded observations.

    Mutates ``ps['github_observations']`` (new generations) and appends firing
    transitions to ``ps['github_outbox']`` (durably, ``dispatched=False``).
    Returns ``{'changed': [item_id...], 'present': {item_id...}}`` — ``changed``
    drives the shadow writer, ``present`` drives prune recovery. On a
    not-yet-bootstrapped project every item is adopt-only: recorded, never
    dispatched, never shadowed. A mapped transition only dispatches when its
    label is in the project's configured ``triggers`` (parity with GitLab's
    per-label enablement); ``None`` means "no gating" (all mapped kinds).
    """
    observations = ps["github_observations"]
    outbox = ps["github_outbox"]
    bootstrapping = not ps.get("bootstrapped")
    enabled = None if triggers is None else set(triggers)
    changed: list[str] = []
    present = set()

    for item in items:
        item_id = item["item_id"]
        present.add(item_id)
        generation = github_generation(client.project_id, item)
        prev = observations.get(item_id)
        record = {
            "generation": generation,
            "option_id": item.get("option_id"),
            "status_name": item.get("status_name"),
            "updated_at": item.get("updated_at"),
            "content_id": item.get("content_id"),
            "number": item.get("number"),
        }

        if bootstrapping:
            observations[item_id] = record  # adopt-only: no dispatch, no shadow
            continue
        if prev is None:
            # First sighting post-bootstrap (auto-added, restored, converted
            # draft): adopt-only regardless of current Status.
            observations[item_id] = record
            log.info(
                "github: adopted first-seen item %s (issue #%s) at Status %s — not dispatched",
                item_id,
                item.get("number"),
                item.get("status_name"),
            )
            continue
        if prev.get("generation") == generation:
            continue  # unchanged

        prev_status = prev.get("status_name")
        new_status = item.get("status_name")
        changed.append(item_id)
        label = GITHUB_DISPATCH_INTO.get(new_status)
        trigger_enabled = label is not None and (enabled is None or label in enabled)
        if trigger_enabled and prev_status in GITHUB_DISPATCH_FROM:
            outbox[generation] = {
                "number": item.get("number"),
                "content_id": item.get("content_id"),
                "item_id": item_id,
                "label": label,
                "issue": github_issue_from_item(client, item),
                "dispatched": False,
                "recorded_at": time.time(),
            }
            log.info(
                "github: dispatch queued for issue #%s (%s -> %s) generation %s",
                item.get("number"),
                prev_status,
                new_status,
                generation,
            )
        elif (
            label is not None
            and prev_status in GITHUB_DISPATCH_FROM
            and not trigger_enabled
        ):
            log.info(
                "github: issue #%s reached %s but trigger %s is not enabled for this project — no dispatch",
                item.get("number"),
                new_status,
                label,
            )
        else:
            log.info(
                "github: observed issue #%s transition %s -> %s (no dispatch)",
                item.get("number"),
                prev_status,
                new_status,
            )
        observations[item_id] = record

    return {"changed": changed, "present": present}


def github_dispatch_fires(ps: dict) -> list[dict]:
    """Undispatched outbox commands as `assemble`-style `{issue, label}` fires."""
    fires = []
    for gen in sorted(ps["github_outbox"]):
        entry = ps["github_outbox"][gen]
        if entry.get("dispatched"):
            continue
        fires.append(
            {"issue": entry["issue"], "label": entry["label"], "_generation": gen}
        )
    return fires


def github_mark_dispatched(ps: dict, fires: list[dict]) -> None:
    """Mark outbox commands consumed and trim old dispatched entries.

    Called in the same atomic state save as conversation creation, so a crash
    before the save re-fires exactly once and a crash after never re-fires.
    """
    outbox = ps["github_outbox"]
    for fire in fires:
        gen = fire.get("_generation")
        if gen in outbox:
            outbox[gen]["dispatched"] = True
    dispatched = [g for g, e in outbox.items() if e.get("dispatched")]
    for gen in sorted(dispatched, key=lambda g: outbox[g].get("recorded_at", 0))[:-200]:
        del outbox[gen]


def github_shadow_status(status_name: str | None) -> str | None:
    """Map a Status option name to its single shadow label (None if unmapped)."""
    return STATUS_TO_LABEL.get(status_name) if status_name else None


def github_shadow_write(
    client: GitHubProject, proj: dict, ps: dict, changed: list[str]
) -> None:
    """Mirror changed items' Status into the label shadow (full replacement).

    Single writer, cross-scope full replacement of all ten shadow labels with
    the one mapped label, a pre-write Status recheck (so a lost race to a verb
    cannot stamp a stale shadow) and a best-effort read-back. Only items whose
    generation changed this tick are mirrored.
    """
    observations = ps["github_observations"]
    for item_id in changed:
        record = observations.get(item_id) or {}
        number = record.get("number")
        if number is None:
            continue
        intended = record.get("status_name")
        # Pre-write recheck: re-read live Status; if it moved on, skip — a newer
        # observation (or a verb) owns the shadow now.
        try:
            live = client.item_status_name(item_id)
        except Exception as e:  # noqa: BLE001 — shadow is best-effort, never blocks
            log.warning("github: shadow recheck failed for issue #%s: %s", number, e)
            continue
        if live != intended:
            log.info(
                "github: shadow skipped for issue #%s — Status moved %s -> %s since observation",
                number,
                intended,
                live,
            )
            continue
        mapped = github_shadow_status(intended)
        if mapped is None:
            # Unmapped Status (e.g. Closed, or a schema option outside the fixed
            # ten): leave labels untouched rather than stripping the shadow.
            log.info(
                "github: issue #%s at unmapped Status %s — shadow left unchanged",
                number,
                intended,
            )
            continue
        add = [mapped]
        remove = [lbl for lbl in SHADOW_LABELS if lbl != mapped]
        try:
            client.set_labels(number, add=add, remove=remove)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "github: shadow label write failed for issue #%s: %s", number, e
            )
            continue
        try:
            labels = set(client.issue_labels(number))
        except Exception:  # noqa: BLE001 — read-back is advisory only
            continue
        stray = (labels & set(SHADOW_LABELS)) - set(add)
        if stray or (mapped and mapped not in labels):
            log.warning(
                "github: shadow read-back mismatch for issue #%s — want %s, stray %s",
                number,
                mapped,
                sorted(stray),
            )


def _conversation_shadow_label(conv: dict, ps: dict) -> str | None:
    """The lifecycle shadow label a conversation's current state maps to."""
    status = conv.get("status")
    if status == "parked":
        return PARKED_LABEL
    if status == "failed":
        return FAILED_LABEL
    if status == "done":
        return (
            MR_READY_LABEL
            if conversation_has_mr(ps, str(conv.get("issue_iid")))
            else FOR_HUMAN_LABEL
        )
    if status == "working":
        return active_label(conv)
    kind = conv.get("kind")
    return kind if kind in TRIGGER_LABELS else None


def github_conversation_status(ps: dict, number) -> str | None:
    """Status implied by the watcher's own conversation record, if any."""
    conv = ps["conversations"].get(str(number))
    if not conv:
        return None
    label = _conversation_shadow_label(conv, ps)
    return LABEL_TO_STATUS.get(label) if label else None


def github_prune_recovery(
    client: GitHubProject, proj: dict, ps: dict, present: set
) -> None:
    """Re-add open issues known to state but absent from the project.

    Pruning an item silently destroys its Status; we re-add the item and restore
    Status from the newest durable record (conversation state, then shadow
    label), then rekey the observation onto the new item id.
    """
    observations = ps["github_observations"]
    for old_item_id in list(observations):
        if old_item_id in present:
            continue
        record = observations[old_item_id]
        number = record.get("number")
        content_id = record.get("content_id")
        if number is None or not content_id:
            continue
        try:
            info = client.issue_item(number)
            if (info.get("state") or "").upper() == "CLOSED":
                observations.pop(old_item_id, None)  # closed is outside reconciliation
                continue
            new_item_id = info.get("item_id") or client.add_item(content_id)
            # Newest durable record wins: conversation state first, then the
            # shadow label physically on the issue, then Triage as the floor.
            status = ps.get("github_restore", {}).get(str(number))
            if status is None:
                shadow = next(
                    (
                        lbl
                        for lbl in client.issue_labels(number)
                        if lbl in LABEL_TO_STATUS
                    ),
                    None,
                )
                status = LABEL_TO_STATUS.get(shadow, "Triage")
            # Re-added item: fence the Item-added->Triage workflow and confirm.
            client.set_status_fenced(new_item_id, status)
        except Exception as e:  # noqa: BLE001 — prune recovery is best-effort per tick
            log.warning("github: prune recovery failed for issue #%s: %s", number, e)
            continue
        observations.pop(old_item_id, None)
        observations[new_item_id] = {
            **record,
            "item_id": new_item_id,
            "status_name": status,
            "option_id": client.status_option_id(status),
            "generation": None,  # force re-observation next tick, adopt-only
        }
        log.info(
            "github: prune recovery re-added issue #%s as item %s at Status %s",
            number,
            new_item_id,
            status,
        )


def fetch_github_inputs(
    client: GitHubProject, proj: dict, ps: dict, triggers=None
) -> dict:
    """Remote-poll one GitHub project: Status diff, shadow mirror, prune recovery.

    Runs in the parallel per-project fetch phase against an isolated poll-state
    snapshot; returns the same shape as :func:`fetch_project_inputs` so the
    shared commit/assemble path is unchanged. Dispatch fires are derived from
    the durable outbox in the commit phase, not here. ``triggers`` gates which
    mapped Status transitions may dispatch (parity with GitLab label enablement).
    """
    client.load_status_schema()
    items = client.fetch_items()
    diff = poll_github_status(client, proj, ps, items, triggers=triggers)
    if ps.get("bootstrapped"):
        github_prune_recovery(client, proj, ps, diff["present"])
        github_shadow_write(client, proj, ps, diff["changed"])
    else:
        log.info(
            "bootstrap: github project %s adopted %d item(s), none dispatched",
            proj.get("path"),
            len(items),
        )
    return {
        "poll_state": ps,
        "comments": [],
        "label_fires": [],
        "new_awards": set(),
    }


# --------------------------------------------------------------------------- vault status poller


def project_is_vault(proj: dict) -> bool:
    return proj.get("forge") == "vault"


def vault_client(proj: dict) -> VaultBoard:
    client = proj.get("_vault")
    if client is None:
        raise ConfigurationError(
            f"vault project {proj.get('path')!r} has no client; forge not initialised"
        )
    return client


def vault_generation(project_id: str, item: dict) -> str:
    """Observation key `(project, item, status)`.

    Status is the sole authority and is content-derived, so a status change is
    the only gesture that matters — mtime is deliberately excluded so iCloud
    touching a file without a content change never looks like a new observation.
    """
    return f"{project_id}|{item['item_id']}|{item.get('status') or ''}"


def vault_issue_from_item(item: dict) -> dict:
    """The `{iid,title,web_url,description}` shape `assemble`/`make_conversation`
    expect, built from the already-read note (no hydrate step for a local board)."""
    return {
        "iid": item["item_id"],
        "title": item["title"],
        "web_url": f"file://{item['abs_path']}",
        "description": item.get("body") or "",
        "note_path": item["note_path"],
        "abs_path": item["abs_path"],
        "model": item.get("model"),
        "session_id": item.get("session_id"),
    }


def vault_task_index(items: list[dict]) -> dict:
    """Map every way a `blockedBy` link can name a task -> its current status."""
    index: dict[str, str] = {}
    for it in items:
        rel = it["note_path"]
        rel_noext = rel[:-3] if rel.endswith(".md") else rel
        stem = rel_noext.rsplit("/", 1)[-1]
        for key in (rel, rel_noext, stem, it["title"]):
            index[key] = it["status"]
    return index


def vault_is_blocked(item: dict, index: dict) -> str | None:
    """The first unresolved blocker (a `blockedBy` target not yet done), else None."""
    for target in item.get("blocked_by") or []:
        t = target[:-3] if target.endswith(".md") else target
        stem = t.rsplit("/", 1)[-1]
        status = index.get(target) or index.get(t) or index.get(stem)
        if status is not None and status not in VAULT_DONE_STATUSES:
            return target
    return None


def vault_trigger_enabled(item: dict, enabled: set | None) -> bool:
    label = VAULT_DISPATCH_INTO.get(item.get("status"))
    return label is not None and (enabled is None or label in enabled)


def vault_queue_fire(outbox: dict, item: dict, generation: str) -> None:
    outbox[generation] = {
        "item_id": item["item_id"],
        "label": VAULT_DISPATCH_INTO[item["status"]],
        "issue": vault_issue_from_item(item),
        "dispatched": False,
        "recorded_at": time.time(),
    }


def poll_vault_status(
    client, proj: dict, ps: dict, items: list[dict], triggers=None
) -> dict:
    """Diff this tick's notes against recorded observations (mirror of
    :func:`poll_github_status`).

    Mutates ``ps['vault_observations']`` and appends firing transitions to
    ``ps['vault_outbox']`` (durably, ``dispatched=False``). A note becoming
    ``status: agent`` from any other status fires — unless it is adopt-only
    (bootstrap or first-seen) or blocked by an unfinished ``blockedBy`` task, in
    which case it is *deferred*: a blocked note has no transition of its own when
    its blocker finishes, so a deferred note is re-checked every tick and fires
    once unblocked (parity with the old sweep's per-cycle re-evaluation).
    """
    observations = ps["vault_observations"]
    outbox = ps["vault_outbox"]
    bootstrapping = not ps.get("bootstrapped")
    enabled = None if triggers is None else set(triggers)
    index = vault_task_index(items)
    changed: list[str] = []
    present = set()

    for item in items:
        item_id = item["item_id"]
        present.add(item_id)
        generation = vault_generation(client.project_id, item)
        prev = observations.get(item_id)
        record = {
            "generation": generation,
            "status": item.get("status"),
            "note_path": item.get("note_path"),
            "abs_path": item.get("abs_path"),
            "title": item.get("title"),
        }

        if bootstrapping:
            observations[item_id] = record  # adopt-only: no dispatch
            continue
        if prev is None:
            observations[item_id] = record
            log.info(
                "vault: adopted first-seen note %s at status %s — not dispatched",
                item.get("note_path"),
                item.get("status"),
            )
            continue
        if prev.get("generation") == generation:
            # Status unchanged. Re-check a note deferred while blocked; fire once
            # its blocker has finished (no transition of its own signals that).
            if (
                prev.get("deferred")
                and vault_trigger_enabled(item, enabled)
                and not vault_is_blocked(item, index)
            ):
                vault_queue_fire(outbox, item, generation)
                record["deferred"] = False
                observations[item_id] = record
                log.info(
                    "vault: deferred note %s now unblocked — dispatch queued",
                    item.get("note_path"),
                )
            continue

        prev_status = prev.get("status")
        new_status = item.get("status")
        changed.append(item_id)
        if vault_trigger_enabled(item, enabled) and prev_status != new_status:
            blocker = vault_is_blocked(item, index)
            if blocker:
                record["deferred"] = True  # re-checked each tick until unblocked
                log.info(
                    "vault: note %s reached `agent` but is blocked by %s — deferred",
                    item.get("note_path"),
                    blocker,
                )
            else:
                vault_queue_fire(outbox, item, generation)
                log.info(
                    "vault: dispatch queued for note %s (%s -> %s)",
                    item.get("note_path"),
                    prev_status,
                    new_status,
                )
        else:
            log.info(
                "vault: observed note %s transition %s -> %s (no dispatch)",
                item.get("note_path"),
                prev_status,
                new_status,
            )
        observations[item_id] = record

    return {"changed": changed, "present": present}


def vault_dispatch_fires(ps: dict) -> list[dict]:
    """Undispatched outbox commands as `assemble`-style `{issue, label}` fires."""
    fires = []
    for gen in sorted(ps["vault_outbox"]):
        entry = ps["vault_outbox"][gen]
        if entry.get("dispatched"):
            continue
        fires.append(
            {"issue": entry["issue"], "label": entry["label"], "_generation": gen}
        )
    return fires


def vault_mark_dispatched(ps: dict, fires: list[dict]) -> None:
    """Mark outbox commands consumed and trim old entries (crash-exactly-once,
    same guarantee as :func:`github_mark_dispatched`)."""
    outbox = ps["vault_outbox"]
    for fire in fires:
        gen = fire.get("_generation")
        if gen in outbox:
            outbox[gen]["dispatched"] = True
    dispatched = [g for g, e in outbox.items() if e.get("dispatched")]
    for gen in sorted(dispatched, key=lambda g: outbox[g].get("recorded_at", 0))[:-200]:
        del outbox[gen]


def fetch_vault_inputs(client, proj: dict, ps: dict, triggers=None) -> dict:
    """Local-poll one vault board: read notes, diff status, queue fires.

    Returns the same shape as :func:`fetch_project_inputs` so the shared
    commit/assemble path is unchanged. Fires are derived from the durable outbox
    in the commit phase, not here.
    """
    items = client.fetch_items()
    poll_vault_status(client, proj, ps, items, triggers=triggers)
    if not ps.get("bootstrapped"):
        log.info(
            "bootstrap: vault %s adopted %d note(s), none dispatched",
            proj.get("path"),
            len(items),
        )
    return {
        "poll_state": ps,
        "comments": [],
        "label_fires": [],
        "new_awards": set(),
    }


# --------------------------------------------------------------------------- conversations


def parse_hint(*texts) -> dict | None:
    """First [provider:model(:effort)] bracket hint found across texts, in order."""
    for t in texts:
        if not t:
            continue
        m = HINT_RE.search(t)
        if m:
            return {"provider": m.group(1), "model": m.group(2), "effort": m.group(3)}
    return None


def spec_from_string(s: str) -> dict:
    parts = s.split(":")
    return {
        "provider": parts[0],
        "model": parts[1],
        "effort": parts[2] if len(parts) > 2 else None,
    }


def fetch_issue_context(gl: GitLab, proj: dict, iid) -> str:
    """Best-effort snapshot of the issue thread + linked items for the launch prompt."""
    if proj.get("forge") == "github":
        # Slice 1: GitHub issue body is carried on the dispatch fire; richer
        # thread context is a later slice. Skip the GitLab-only notes/links API.
        return ""
    try:
        notes = gl.get(
            f"projects/{proj['id']}/issues/{iid}/notes", sort="asc", per_page=100
        )
        thread = [
            f"[{n['author']['username']}] {n['body'][:1500]}"
            for n in notes
            if not n.get("system")
        ][-20:]
        links = gl.get(f"projects/{proj['id']}/issues/{iid}/links", per_page=20)
        linked = [
            f"- {li['title']} ({li['state']}) {li['web_url']}" for li in links[:10]
        ]
    except Exception as e:  # noqa: BLE001 — context is best-effort, never blocks dispatch
        log.warning("issue !%s context fetch failed: %s", iid, e)
        return ""
    parts = []
    if thread:
        parts.append(
            "Existing issue comments (oldest first):\n\n" + "\n\n".join(thread)
        )
    if linked:
        parts.append("Linked issues:\n" + "\n".join(linked))
    return "\n\n".join(parts)[:8000]


def merged_jira_config(proj: dict, jira_defaults: dict | None) -> dict:
    cfg = dict(jira_defaults or {})
    project_cfg = proj.get("jira")
    if project_cfg is False:
        cfg["enabled"] = False
    elif isinstance(project_cfg, dict):
        cfg.update(project_cfg)
    return cfg


def jira_keys_from_texts(*texts: str | None, max_keys: int = 3) -> list[str]:
    keys = []
    for text in texts:
        if not text:
            continue
        # Match case-sensitively: real Jira keys are uppercase (PROJ-123),
        # so upper-casing first would promote lowercase branch tokens like
        # issue-102 into bogus keys (ISSUE-102) that 404 on fetch.
        for match in JIRA_KEY_RE.finditer(text):
            keys.append(match.group(1))
    return list(dict.fromkeys(keys))[:max_keys]


def jira_helper_path(raw: str | None) -> Path:
    helper = Path(raw or "scripts/jira-board").expanduser()
    if not helper.is_absolute():
        helper = REPOSITORY_ROOT / helper
    return helper


def valid_jira_base_url(raw) -> bool:
    if not isinstance(raw, str) or not raw.strip():
        return False
    if "`" in raw or any(character.isspace() for character in raw):
        return False
    parsed = urllib.parse.urlparse(raw)
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
    )


def valid_jira_custom_label(raw) -> bool:
    return (
        isinstance(raw, str)
        and bool(raw.strip())
        and len(raw) <= 80
        and "=" not in raw
        and "`" not in raw
        and all(character.isprintable() for character in raw)
    )


def valid_jira_field_id(raw) -> bool:
    return isinstance(raw, str) and JIRA_CUSTOM_FIELD_ID_RE.fullmatch(raw) is not None


def valid_jira_custom_fields_shape(raw) -> bool:
    return isinstance(raw, dict) and all(
        valid_jira_custom_label(label)
        and isinstance(field_id, str)
        and bool(field_id.strip())
        for label, field_id in raw.items()
    )


def valid_jira_custom_fields(raw) -> bool:
    return valid_jira_custom_fields_shape(raw) and all(
        valid_jira_field_id(field_id) for field_id in raw.values()
    )


def fetch_jira_context(proj: dict, issue: dict, jira_defaults: dict | None) -> str:
    """Best-effort, read-only Jira context for issues that mention Jira keys."""
    cfg = merged_jira_config(proj, jira_defaults)
    if not cfg.get("enabled"):
        return ""
    base_url = cfg.get("base_url")
    if not valid_jira_base_url(base_url):
        log.warning("Jira context is enabled but `base_url` is not a safe HTTP(S) URL")
        return ""
    base_url = base_url.rstrip("/")
    custom_fields = cfg.get("custom_fields") or {}
    development_field = cfg.get("development_field")
    if not valid_jira_custom_fields(custom_fields) or (
        development_field is not None and not valid_jira_field_id(development_field)
    ):
        log.warning("Jira context custom field configuration is invalid")
        return ""
    max_keys = int(cfg.get("max_issues") or 3)
    keys = jira_keys_from_texts(
        issue.get("title"), issue.get("description"), max_keys=max_keys
    )
    if not keys:
        return ""
    helper_raw = cfg.get("helper")
    if helper_raw:
        helper = jira_helper_path(helper_raw)
        if not helper.exists():
            log.warning("Jira context helper missing: %s", helper)
            return ""
        helper_command = [sys.executable, str(helper)]
    else:
        wrapper = REPOSITORY_ROOT / "scripts" / "jira-board"
        helper_command = (
            [sys.executable, str(wrapper)]
            if wrapper.is_file()
            else [sys.executable, "-m", "eastwatch.jira.core"]
        )
    sections = cfg.get("sections") or ["essentials", "status", "custom", "attachments"]
    if isinstance(sections, str):
        sections_arg = sections
    else:
        sections_arg = ",".join(str(section) for section in sections)
    timeout = int(cfg.get("timeout_seconds") or 25)
    command_base = [
        *helper_command,
        "prompt",
        "__KEY__",
        "--base-url",
        str(base_url),
        "--section",
        sections_arg,
        "--comments-limit",
        str(cfg.get("comments_limit", 3)),
        "--remote-link-limit",
        str(cfg.get("remote_link_limit", 10)),
        "--body-limit",
        str(cfg.get("body_limit", 2000)),
    ]
    keychain = cfg.get("keychain") or {}
    if keychain.get("service"):
        command_base.extend(["--keychain-service", str(keychain["service"])])
    if keychain.get("account"):
        command_base.extend(["--keychain-account", str(keychain["account"])])
    for label, field_id in custom_fields.items():
        command_base.extend(["--custom-field", f"{label}={field_id}"])
    if development_field:
        command_base.extend(["--development-field", str(development_field)])

    parts = ["Jira context (read-only; fetched because the issue references Jira):"]
    parts.append(
        "For more Jira context, agents may run: "
        f"`uv run python scripts/jira-board linked-open <KEY> --base-url {base_url}`, "
        f"`uv run python scripts/jira-board project-open <PROJECT> --base-url {base_url}`, "
        f"`uv run python scripts/jira-board search '<JQL>' --base-url {base_url}`, or "
        "`uv run python scripts/jira-board download-attachments <KEY> "
        f"--base-url {base_url} --out /tmp/jira-<KEY>`."
    )
    for key in keys:
        cmd = [key if item == "__KEY__" else item for item in command_base]
        try:
            proc = subprocess.run(
                cmd,
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except Exception as e:  # noqa: BLE001 — Jira context is optional.
            log.warning("Jira context fetch failed for %s: %s", key, e)
            parts.append(
                f"Jira issue: {key}\nFetch failed; use {base_url}/browse/{key}."
            )
            continue
        if proc.returncode == 0 and proc.stdout.strip():
            parts.append(proc.stdout.strip())
        else:
            log.warning(
                "Jira context fetch failed for %s: %s",
                key,
                (proc.stderr or proc.stdout)[-500:],
            )
            parts.append(
                f"Jira issue: {key}\nFetch failed; use {base_url}/browse/{key}."
            )
    return "\n\n".join(parts)[:12000]


def apply_hint(conv: dict, spec: dict) -> bool:
    """Apply a provider/model/effort hint. Return True when it changes provider."""
    old_provider = conv.get("provider")
    provider_changed = old_provider is not None and old_provider != spec["provider"]
    conv["provider"] = spec["provider"]
    conv["model"] = spec["model"]
    conv["effort"] = spec.get("effort")
    if provider_changed:
        # Provider-specific session handles are not portable. Keep the same
        # conversation record/cwd/session_dir, but make the next dispatch launch
        # a fresh session for the newly selected provider.
        conv["session_id"] = None
        conv["session_file"] = None
    return provider_changed


def vault_model_hint(model: str | None) -> str | None:
    """Normalise a note's bare ``model:`` into a ``[provider:model]`` bracket hint
    so ``parse_hint`` (which requires a ``claude|pi`` provider) can consume it."""
    if not model:
        return None
    m = str(model).strip()
    if not m:
        return None
    return f"[{m}]" if ":" in m else f"[claude:{m}]"


def make_vault_conversation(
    proj: dict, issue: dict, kind: str, hint_texts: list, defaults: dict
) -> dict:
    """A conversation for a vault task note — built from the note itself (no API,
    no worktree; the worker runs in the vault so it loads AGENTS.md natively)."""
    hints = [h for h in (vault_model_hint(issue.get("model")), *hint_texts) if h]
    # A note's own `model:` wins; else this project's `default_spec`; else the
    # global default for the trigger; else claude:sonnet.
    fallback = proj.get("default_spec") or defaults.get(kind) or "claude:sonnet"
    spec = parse_hint(*hints) or spec_from_string(fallback)
    slug = proj["path"].replace("/", "-")
    session_dir = CONVOS_DIR / f"{slug}-{issue['iid']}"
    session_dir.mkdir(parents=True, exist_ok=True)
    cwd = str(Path(proj["vault_path"]).expanduser())
    return {
        "provider": spec["provider"],
        "model": spec["model"],
        "effort": spec.get("effort"),
        # Rename-resume: a session-id persisted in the note reattaches the same
        # claude session even though a rename gave the note a new item_id.
        "session_id": issue.get("session_id"),
        "session_file": None,
        "cwd": cwd,
        "session_dir": str(session_dir),
        "host": proj["host"],
        "project_path": proj["path"],
        "checkout": cwd,
        "briefing": proj.get("worker_briefing"),
        "thread_context": "",
        "jira_context": "",
        "status": "new",
        "kind": kind,
        "anchor": "issue",
        "forge": "vault",
        "note_path": issue.get("note_path"),
        "abs_path": issue.get("abs_path"),
        "reply_target": None,
        "issue_iid": str(issue["iid"]),
        "issue_title": issue["title"],
        "issue_url": issue.get("web_url"),
        "issue_desc": (issue.get("description") or "")[:6000],
        "pending": [],
        "mr_iids": [],
        "parked_note_id": None,
        "last_note_id": None,
        "last_reply_body_hash": None,
    }


def make_conversation(
    gl: GitLab,
    proj: dict,
    issue: dict,
    kind: str,
    hint_texts: list,
    defaults: dict,
    jira_defaults: dict | None = None,
) -> dict:
    if project_is_vault(proj):
        return make_vault_conversation(proj, issue, kind, hint_texts, defaults)
    spec = parse_hint(*hint_texts) or spec_from_string(defaults[kind])
    slug = proj["path"].replace("/", "-")
    session_dir = CONVOS_DIR / f"{slug}-{issue['iid']}"
    session_dir.mkdir(parents=True, exist_ok=True)
    checkout = proj.get("local_checkout")
    if checkout:
        checkout = str(Path(checkout).expanduser())
    # Workers run in the checkout when it exists (claude then loads the repo's
    # CLAUDE.md natively); otherwise in the scratch dir, with the prompt telling
    # them to clone the configured path first.
    cwd = checkout if checkout and Path(checkout).is_dir() else str(session_dir)
    if checkout and cwd != checkout:
        log.warning("checkout %s missing; worker will be told to clone it", checkout)
    return {
        "provider": spec["provider"],
        "model": spec["model"],
        "effort": spec.get("effort"),
        "session_id": None,
        "session_file": None,
        "cwd": cwd,
        "session_dir": str(session_dir),
        "host": proj["host"],
        "project_path": proj["path"],
        "checkout": checkout,
        "briefing": proj.get("worker_briefing"),
        "thread_context": fetch_issue_context(gl, proj, issue["iid"]),
        "jira_context": fetch_jira_context(proj, issue, jira_defaults),
        "status": "new",
        "kind": kind,
        "anchor": "issue",
        "reply_target": None,
        "issue_iid": str(issue["iid"]),
        "issue_title": issue["title"],
        "issue_url": issue["web_url"],
        "issue_desc": (issue.get("description") or "")[:6000],
        "pending": [],
        "mr_iids": [],
        "parked_note_id": None,
        "last_note_id": None,
        "last_reply_body_hash": None,
    }


def make_mr_conversation(
    proj: dict, mr: dict, key: str, kind: str, hint_texts: list, defaults: dict
) -> dict:
    spec = parse_hint(*hint_texts) or spec_from_string(defaults[kind])
    slug = proj["path"].replace("/", "-")
    session_dir = CONVOS_DIR / f"{slug}-mr-{mr['iid']}-{key.replace(':', '-')}"
    session_dir.mkdir(parents=True, exist_ok=True)
    checkout = proj.get("local_checkout")
    if checkout:
        checkout = str(Path(checkout).expanduser())
    cwd = checkout if checkout and Path(checkout).is_dir() else str(session_dir)
    if checkout and cwd != checkout:
        log.warning("checkout %s missing; worker will be told to clone it", checkout)
    return {
        "provider": spec["provider"],
        "model": spec["model"],
        "effort": spec.get("effort"),
        "session_id": None,
        "session_file": None,
        "cwd": cwd,
        "session_dir": str(session_dir),
        "host": proj["host"],
        "project_path": proj["path"],
        "checkout": checkout,
        "briefing": proj.get("worker_briefing"),
        "thread_context": "",
        "status": "new",
        "kind": kind,
        "anchor": "mr",
        "reply_target": None,
        "issue_iid": None,
        "issue_title": None,
        "issue_url": None,
        "issue_desc": "",
        "mr_iid": str(mr["iid"]),
        "mr_title": mr.get("title") or "",
        "mr_url": mr.get("web_url"),
        "mr_desc": (mr.get("description") or "")[:6000],
        "mr_source_branch": mr.get("source_branch"),
        "mr_target_branch": mr.get("target_branch"),
        "pending": [],
        "mr_iids": [str(mr["iid"])],
        "parked_note_id": None,
        "last_note_id": None,
        "last_reply_body_hash": None,
    }


def parse_mr_iids(text: str, proj: dict) -> list[str]:
    """MR iids referenced in worker text via project MR URL or guarded !N."""
    if not text:
        return []
    iids: set[str] = set()
    url_re = re.compile(r"https?://[^\s>)]+/-/merge_requests/(\d+)")
    expected_path = f"/{proj['path']}/-/merge_requests/"
    for m in url_re.finditer(text):
        if expected_path in m.group(0):
            iids.add(m.group(1))
    for m in MR_REF_RE.finditer(text):
        iids.add(m.group(1))
    return sorted(iids, key=int)


def fetch_mr(
    gl: GitLab, proj: dict, mr_iid: str, *, raise_transient: bool = False
) -> dict | None:
    try:
        return gl.get(f"projects/{proj['id']}/merge_requests/{mr_iid}")
    except requests.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else None
        if status_code == 404:
            log.warning("merge request !%s fetch returned 404", mr_iid)
            return None
        if raise_transient:
            raise TransientMRFetchError(
                f"merge request !{mr_iid} fetch failed: {e}"
            ) from e
        log.warning("merge request !%s fetch failed: %s", mr_iid, e)
        return None
    except requests.RequestException as e:
        if raise_transient:
            raise TransientMRFetchError(
                f"merge request !{mr_iid} fetch failed: {e}"
            ) from e
        log.warning("merge request !%s fetch failed: %s", mr_iid, e)
        return None


def fetch_issue(gl: GitLab, proj: dict, iid: str) -> dict | None:
    try:
        return gl.get(f"projects/{proj['id']}/issues/{iid}")
    except requests.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else None
        if status_code == 404:
            log.warning("issue !%s fetch returned 404", iid)
            return None
        log.warning("issue !%s fetch failed: %s", iid, e)
        return None
    except requests.RequestException as e:
        log.warning("issue !%s fetch failed: %s", iid, e)
        return None


def surface_state(value: str | None) -> str | None:
    return str(value).lower() if value is not None else None


def issue_is_open_for_plain_resume(
    gl: GitLab, proj: dict, iid: str, gesture: dict
) -> bool:
    state = surface_state(gesture.get("issue_state") or gesture.get("state"))
    if state is None:
        issue = fetch_issue(gl, proj, iid)
        if issue is None:
            log.warning(
                "issue !%s plain owner comment ignored: could not determine issue state",
                iid,
            )
            return False
        state = surface_state(issue.get("state"))
    if state == "closed":
        log.info("issue !%s plain owner comment ignored: issue is closed", iid)
        return False
    if state not in {"opened", "open"}:
        log.warning(
            "issue !%s plain owner comment ignored: unknown issue state %r", iid, state
        )
        return False
    return True


def mr_is_closed_or_merged(mr: dict) -> bool:
    state = surface_state(mr.get("state"))
    return state in {"closed", "merged"} or bool(mr.get("merged_at"))


def mr_is_open_for_plain_resume(
    gl: GitLab,
    proj: dict,
    mr_iid: str,
    gesture: dict,
    mr: dict | None,
) -> tuple[bool, dict | None]:
    state = surface_state(
        gesture.get("mr_state") or gesture.get("state") or ((mr or {}).get("state"))
    )
    if state in {"closed", "merged"} or (mr is not None and mr.get("merged_at")):
        log.info(
            "merge request !%s plain owner comment ignored: merge request is closed/merged",
            mr_iid,
        )
        return False, mr
    if state in {"opened", "open"}:
        return True, mr
    if mr is None:
        mr = fetch_mr(gl, proj, mr_iid, raise_transient=True)
    if mr is None:
        log.warning(
            "merge request !%s plain owner comment ignored: could not determine merge request state",
            mr_iid,
        )
        return False, None
    if mr_is_closed_or_merged(mr):
        log.info(
            "merge request !%s plain owner comment ignored: merge request is closed/merged",
            mr_iid,
        )
        return False, mr
    state = surface_state(mr.get("state"))
    if state not in {"opened", "open"}:
        log.warning(
            "merge request !%s plain owner comment ignored: unknown merge request state %r",
            mr_iid,
            state,
        )
        return False, mr
    return True, mr


def record_mr_mapping(
    ps: dict, conversation_key: str, mr: dict, mapped_from: str
) -> bool:
    conversation_key = str(conversation_key)
    conv = ps["conversations"].get(conversation_key)
    if conv is None:
        return False
    mr_iid = str(mr["iid"])
    existing = ps.setdefault("mr_index", {}).get(mr_iid) or {}
    existing_key = existing.get("conversation_key")
    if existing_key is not None and str(existing_key) != conversation_key:
        log.warning(
            "refusing to remap merge request !%s from issue !%s to issue !%s via %s",
            mr_iid,
            existing_key,
            conversation_key,
            mapped_from,
        )
        return False
    entry = {
        "issue_iid": conv.get("issue_iid") or conversation_key,
        "conversation_key": conversation_key,
        "mapped_from": mapped_from,
        "id": mr.get("id"),
        "web_url": mr.get("web_url"),
        "source_branch": mr.get("source_branch"),
        "target_branch": mr.get("target_branch"),
    }
    ps["mr_index"][mr_iid] = entry
    mr_iids = conv.setdefault("mr_iids", [])
    if mr_iid not in mr_iids:
        mr_iids.append(mr_iid)
        mr_iids.sort(key=int)
    log.info(
        "mapped merge request !%s -> issue !%s via %s",
        mr_iid,
        conversation_key,
        mapped_from,
    )
    return True


def parse_mr_marker(description: str | None, proj: dict) -> str | None:
    for marker in MR_MARKER_RE.finditer(description or ""):
        fields = dict(MR_MARKER_FIELD_RE.findall(marker.group(1)))
        source_project = fields.get("source_project")
        source_issue_iid = fields.get("source_issue_iid")
        valid_sources = {proj["path"], f"{proj['host']}/{proj['path']}"}
        if (
            source_project not in valid_sources
            or not source_issue_iid
            or not source_issue_iid.isdigit()
        ):
            continue
        issue_iid = str(int(source_issue_iid))
        conversation_key = fields.get("conversation_key")
        if conversation_key is not None and (
            not conversation_key.isdigit() or str(int(conversation_key)) != issue_iid
        ):
            continue
        return issue_iid
    return None


def indexed_issue_iid(indexed: dict) -> str | None:
    for key in ("conversation_key", "issue_iid"):
        value = indexed.get(key)
        if value is not None and str(value).isdigit():
            return str(int(value))
    return None


def resolve_mr_mapping(
    gl: GitLab, proj: dict, ps: dict, mr_iid: str
) -> tuple[dict | None, dict | None]:
    mr_iid = str(mr_iid)
    index = ps.setdefault("mr_index", {})
    indexed = index.get(mr_iid) or {}
    indexed_iid = indexed_issue_iid(indexed)
    mr = None
    if indexed_iid and indexed_iid in ps["conversations"]:
        return {
            "issue_iid": indexed_iid,
            "conversation_key": indexed_iid,
            "mapped_from": indexed.get("mapped_from") or "state_index",
            "conversation_exists": True,
        }, mr

    mr = fetch_mr(gl, proj, mr_iid, raise_transient=True)
    if mr is None:
        return None, None

    conversation_key = parse_mr_marker(mr.get("description"), proj)
    if conversation_key:
        exists = conversation_key in ps["conversations"]
        if exists:
            record_mr_mapping(ps, conversation_key, mr, "description_marker")
        return {
            "issue_iid": conversation_key,
            "conversation_key": conversation_key,
            "mapped_from": "description_marker",
            "conversation_exists": exists,
        }, mr

    return None, mr


def capture_mrs_from_reply(
    gl: GitLab, proj: dict, ps: dict, iid: str, conv: dict, reply: str
) -> None:
    """Primary MR mapping path: worker final reply references an MR it opened."""
    if proj.get("forge") == "github":
        # GitHub PR mapping is a later slice; the terminal split still routes a
        # done GitHub conversation to Status Review vs For-human via mr_index,
        # which stays empty here (no MR capture in slice 1).
        return
    for mr_iid in parse_mr_iids(reply, proj):
        if mr_iid in conv.setdefault("mr_iids", []) or mr_iid in ps.setdefault(
            "mr_index", {}
        ):
            continue
        mr = fetch_mr(gl, proj, mr_iid)
        if mr is not None and parse_mr_marker(mr.get("description"), proj) == str(iid):
            record_mr_mapping(ps, str(iid), mr, "final_reply")


def body_hash(text: str) -> str:
    normalized = text.replace("\r\n", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def find_mr_thread_conversation(gl, proj, convs, mr_iid, gesture):
    """Locate the standalone MR Q&A conversation this comment is a threaded reply to.

    A standalone MR conversation is keyed ``mr:<iid>:note:<id>`` and carries no
    issue mapping, so resolve_mr_mapping can never find it — every owner comment
    on such an MR is judged in isolation. That is why a plain follow-up under an
    answered @agent thread was dropped: nothing linked the reply back to the
    conversation that produced the answer.

    We reconnect them through the discussion the owner replied into: it contains
    a bot note (the agent's own answer thread) and one of the conversation's
    anchor notes — either the note the answer was posted as (``last_note_id`` /
    ``parked_note_id``) or the originating @agent note encoded in the key. Returns
    ``(conv_key, conv)`` or None. May raise TransientDiscussionLookupError so the
    caller can defer the gesture instead of silently dropping it.
    """
    discussion_id = gesture.get("discussion_id")
    if not discussion_id:
        return None
    prefix = f"mr:{mr_iid}:"
    candidates = [
        (key, conv)
        for key, conv in convs.items()
        if key.startswith(prefix) and conv.get("anchor") == "mr"
    ]
    if not candidates:
        return None
    discussion = discussion_by_id(gl, proj, "mr", mr_iid, discussion_id)
    if discussion is None:
        return None
    notes = discussion.get("notes") or []
    bot_user_id = proj.get("bot_user_id")
    if not any(
        str((n.get("author") or {}).get("id")) == str(bot_user_id) for n in notes
    ):
        return None
    note_ids = {str(n.get("id")) for n in notes}
    for key, conv in candidates:
        anchors = {str(conv.get("last_note_id")), str(conv.get("parked_note_id"))}
        if ":note:" in key:
            anchors.add(key.split(":note:", 1)[1])
        anchors.discard("None")
        if anchors & note_ids:
            return key, conv
    return None


def resume_mr_thread(
    gl, proj, conv_key, conv, mr_iid, gesture, question, retry_mr_gestures
):
    """Queue a threaded reply onto an existing standalone MR Q&A conversation.

    Mirrors the mapped-conversation resume path (echo guard, open-state check for
    plain replies, resume hints, pending append) so a reply in the agent's thread
    behaves exactly like a follow-up on a mapped MR — no @agent re-tag needed.
    """
    last_reply_body_hash = conv.get("last_reply_body_hash")
    if last_reply_body_hash:
        raw_body = gesture.get("comment_body") or ""
        status_stripped_body, _, _ = split_status(raw_body)
        if last_reply_body_hash in {
            body_hash(raw_body),
            body_hash(status_stripped_body),
        }:
            log.info(
                "echo guard: dropped owner comment on merge request !%s note %s matching last posted reply",
                mr_iid,
                gesture.get("note_id"),
            )
            return
    mr = None
    if question is None:
        try:
            is_open, mr = mr_is_open_for_plain_resume(gl, proj, mr_iid, gesture, None)
        except TransientMRFetchError as e:
            retry_mr_gestures.append(gesture)
            log.warning(
                "merge request !%s note %s deferred after transient fetch failure: %s",
                mr_iid,
                gesture.get("note_id"),
                e,
            )
            return
        if not is_open:
            return
    if mr is None:
        mr = fetch_mr(gl, proj, mr_iid)
    hint = parse_hint(gesture.get("comment_body"))
    if hint:
        provider_changed = apply_hint(conv, hint)
        log.info(
            "resume hint: merge request !%s -> %s %s:%s:%s%s",
            mr_iid,
            conv_key,
            conv["provider"],
            conv["model"],
            conv["effort"],
            " (fresh provider session)" if provider_changed else "",
        )
    conv["next_reply_target"] = mr_reply_target(mr_iid, gesture)
    if question is not None:
        conv["pending"].append(
            mr_comment_context(mr, gesture, question) if mr is not None else question
        )
    else:
        conv["pending"].append(
            mr_comment_context(mr, gesture) if mr is not None else gesture["body"]
        )
    log.info(
        "resume: merge request !%s note %s -> conversation %s (thread reply)",
        mr_iid,
        gesture.get("note_id"),
        conv_key,
    )


def assemble(
    gl,
    proj,
    ps,
    comments,
    label_fires,
    new_awards,
    owner,
    defaults,
    triggers,
    jira_defaults=None,
):
    """Merge this cycle's gestures into per-conversation pending queues (coalescing)."""
    convs = ps["conversations"]
    retry_mr_gestures = []
    seen_comment_keys = set()
    all_comments = []
    for g in [*ps.get("pending_mr_comment_gestures", []), *comments]:
        key = (
            g.get("kind"),
            str(g.get("event_id") or ""),
            str(g.get("note_id") or ""),
            str(g.get("mr_iid") or g.get("iid") or ""),
        )
        if key in seen_comment_keys:
            continue
        seen_comment_keys.add(key)
        all_comments.append(g)

    for g in all_comments:
        if g.get("kind") == "mr":
            mr_iid = str(g["mr_iid"])
            question = (
                agent_mention_question(g.get("comment_body") or "")
                if "mention" in triggers
                else None
            )

            # A reply inside an already-answered standalone MR thread continues
            # that same conversation — no @agent re-tag. Issue-mapped MRs are
            # resolved below via resolve_mr_mapping (which already handles their
            # thread replies), so this only fires for mr:<iid>:note:<id> convs.
            try:
                thread_hit = find_mr_thread_conversation(gl, proj, convs, mr_iid, g)
            except TransientDiscussionLookupError as e:
                retry_mr_gestures.append(g)
                log.warning(
                    "merge request !%s note %s deferred after discussion lookup failure: %s",
                    mr_iid,
                    g.get("note_id"),
                    e,
                )
                continue
            if thread_hit is not None:
                thread_key, thread_conv = thread_hit
                resume_mr_thread(
                    gl,
                    proj,
                    thread_key,
                    thread_conv,
                    mr_iid,
                    g,
                    question,
                    retry_mr_gestures,
                )
                continue

            try:
                mapping, mr = resolve_mr_mapping(gl, proj, ps, mr_iid)
            except TransientMRFetchError as e:
                retry_mr_gestures.append(g)
                log.warning(
                    "merge request !%s note %s deferred after transient fetch failure: %s",
                    mr_iid,
                    g.get("note_id"),
                    e,
                )
                continue

            if mapping is None:
                if question is None:
                    log.warning(
                        "merge request !%s comment ignored: no mapped conversation "
                        "(owner can start a fresh MR Q&A by beginning the comment with @agent)",
                        mr_iid,
                    )
                    continue
                if mr is None:
                    log.warning(
                        "merge request !%s @agent comment ignored: MR could not be fetched",
                        mr_iid,
                    )
                    continue
                conv_key = f"mr:{mr_iid}:note:{g.get('note_id') or g.get('event_id') or uuid.uuid4().hex[:8]}"
                conv = make_mr_conversation(
                    proj,
                    mr,
                    conv_key,
                    "qa",
                    [g.get("comment_body"), mr.get("description")],
                    defaults,
                )
                conv["next_reply_target"] = mr_reply_target(mr_iid, g)
                conv["pending"].append(mr_comment_context(mr, g, question))
                convs[conv_key] = conv
                log.info(
                    "new qa conversation on merge request !%s (%s:%s)",
                    mr_iid,
                    conv["provider"],
                    conv["model"],
                )
                continue

            if question is not None and mr is None:
                try:
                    mr = fetch_mr(gl, proj, mr_iid, raise_transient=True)
                except TransientMRFetchError as e:
                    retry_mr_gestures.append(g)
                    log.warning(
                        "merge request !%s note %s deferred after transient fetch failure: %s",
                        mr_iid,
                        g.get("note_id"),
                        e,
                    )
                    continue

            iid = str(mapping["issue_iid"])
            conv = convs.get(iid)
            if conv is None:
                if question is None:
                    log.info(
                        "merge request !%s plain owner comment ignored: mapped issue !%s has no conversation",
                        mr_iid,
                        iid,
                    )
                    continue
                issue = fetch_issue(gl, proj, iid)
                if issue is None:
                    if mr is not None:
                        conv_key = f"mr:{mr_iid}:note:{g.get('note_id') or g.get('event_id') or uuid.uuid4().hex[:8]}"
                        conv = make_mr_conversation(
                            proj,
                            mr,
                            conv_key,
                            "qa",
                            [g.get("comment_body"), mr.get("description")],
                            defaults,
                        )
                        conv["next_reply_target"] = mr_reply_target(mr_iid, g)
                        conv["pending"].append(mr_comment_context(mr, g, question))
                        convs[conv_key] = conv
                        log.info(
                            "new qa conversation on merge request !%s after mapped issue !%s fetch failed (%s:%s)",
                            mr_iid,
                            iid,
                            conv["provider"],
                            conv["model"],
                        )
                    continue
                conv = make_conversation(
                    gl,
                    proj,
                    issue,
                    "qa",
                    [g.get("comment_body"), issue.get("description")],
                    defaults,
                    jira_defaults,
                )
                convs[iid] = conv
                if mr is not None:
                    record_mr_mapping(
                        ps, iid, mr, mapping.get("mapped_from") or "mr_comment"
                    )
                log.info(
                    "new qa conversation on issue !%s from merge request !%s (%s:%s)",
                    iid,
                    mr_iid,
                    conv["provider"],
                    conv["model"],
                )
            last_reply_body_hash = conv.get("last_reply_body_hash")
            if last_reply_body_hash:
                raw_body = g.get("comment_body") or ""
                status_stripped_body, _, _ = split_status(raw_body)
                incoming_hashes = {body_hash(raw_body), body_hash(status_stripped_body)}
                if last_reply_body_hash in incoming_hashes:
                    log.info(
                        "echo guard: dropped owner comment on merge request !%s note %s "
                        "matching last posted reply",
                        mr_iid,
                        g.get("note_id"),
                    )
                    continue
            if question is None:
                try:
                    is_open, mr = mr_is_open_for_plain_resume(gl, proj, mr_iid, g, mr)
                except TransientMRFetchError as e:
                    retry_mr_gestures.append(g)
                    log.warning(
                        "merge request !%s note %s deferred after transient fetch failure: %s",
                        mr_iid,
                        g.get("note_id"),
                        e,
                    )
                    continue
                if not is_open:
                    continue
                try:
                    has_bot_note = discussion_has_bot_note(
                        gl, proj, "mr", mr_iid, g.get("discussion_id")
                    )
                except TransientDiscussionLookupError as e:
                    retry_mr_gestures.append(g)
                    log.warning(
                        "merge request !%s note %s deferred after discussion lookup failure: %s",
                        mr_iid,
                        g.get("note_id"),
                        e,
                    )
                    continue
                if not has_bot_note:
                    log.info(
                        "merge request !%s plain owner comment ignored: note %s is not in a bot-authored discussion",
                        mr_iid,
                        g.get("note_id"),
                    )
                    continue
            hint = parse_hint(g.get("comment_body"))
            if hint:
                provider_changed = apply_hint(conv, hint)
                log.info(
                    "resume hint: merge request !%s -> issue !%s %s:%s:%s%s",
                    mr_iid,
                    iid,
                    conv["provider"],
                    conv["model"],
                    conv["effort"],
                    " (fresh provider session)" if provider_changed else "",
                )
            conv["next_reply_target"] = mr_reply_target(mr_iid, g)
            if question is not None:
                conv["pending"].append(
                    mr_comment_context(mr, g, question) if mr is not None else question
                )
            else:
                conv["pending"].append(
                    mr_comment_context(mr, g) if mr is not None else g["body"]
                )
            continue

        iid = str(g["iid"])
        conv = convs.get(iid)
        question = (
            agent_mention_question(g.get("body") or "")
            if "mention" in triggers
            else None
        )
        if conv is not None:
            last_reply_body_hash = conv.get("last_reply_body_hash")
            if last_reply_body_hash:
                status_stripped_body, _, _ = split_status(g["body"])
                incoming_hashes = {
                    body_hash(g["body"]),
                    body_hash(status_stripped_body),
                }
                if last_reply_body_hash in incoming_hashes:
                    log.info(
                        "echo guard: dropped owner comment on issue !%s note %s matching last posted reply",
                        iid,
                        g.get("note_id"),
                    )
                    continue
            if question is None:
                if not issue_is_open_for_plain_resume(gl, proj, iid, g):
                    continue
                try:
                    has_bot_note = discussion_has_bot_note(
                        gl, proj, "issue", iid, g.get("discussion_id")
                    )
                except TransientDiscussionLookupError as e:
                    retry_mr_gestures.append(g)
                    log.warning(
                        "issue !%s note %s deferred after discussion lookup failure: %s",
                        iid,
                        g.get("note_id"),
                        e,
                    )
                    continue
                if not has_bot_note:
                    log.info(
                        "issue !%s plain owner comment ignored: note %s is not in a bot-authored discussion",
                        iid,
                        g.get("note_id"),
                    )
                    continue
            hint = parse_hint(g["body"])
            if hint:
                provider_changed = apply_hint(conv, hint)
                log.info(
                    "resume hint: issue !%s -> %s:%s:%s%s",
                    iid,
                    conv["provider"],
                    conv["model"],
                    conv["effort"],
                    " (fresh provider session)" if provider_changed else "",
                )
            conv["next_reply_target"] = issue_reply_target(iid, g)
            conv["pending"].append(question if question is not None else g["body"])
        else:
            if question is None:
                continue
            issue = fetch_issue(gl, proj, iid)
            if issue is None:
                continue
            conv = make_conversation(
                gl,
                proj,
                issue,
                "qa",
                [g["body"], issue.get("description")],
                defaults,
                jira_defaults,
            )
            conv["next_reply_target"] = issue_reply_target(iid, g)
            conv["pending"].append(question)
            convs[iid] = conv
            log.info(
                "new qa conversation on issue !%s (%s:%s)",
                iid,
                conv["provider"],
                conv["model"],
            )

    ps["pending_mr_comment_gestures"] = retry_mr_gestures[-1000:]

    for f in label_fires:
        issue, label = f["issue"], f["label"]
        iid = str(issue["iid"])
        conv = convs.get(iid)
        if conv is not None:
            conv["next_reply_target"] = issue_reply_target(iid)
            conv["pending"].append(
                f"The issue has been labeled `{label}` again. Pick the work back up per the issue."
            )
        else:
            conv = make_conversation(
                gl,
                proj,
                issue,
                label,
                [issue.get("description")],
                defaults,
                jira_defaults,
            )
            conv["next_reply_target"] = issue_reply_target(iid)
            convs[iid] = conv
            log.info(
                "new %s conversation on issue !%s (%s:%s:%s)",
                label,
                iid,
                conv["provider"],
                conv["model"],
                conv["effort"],
            )

    parked_notes = {
        str(c["parked_note_id"]): iid
        for iid, c in convs.items()
        if c.get("parked_note_id") and c["status"] == "parked"
    }
    # ✅ on a done conversation's final answer = approved, act on your recommendation.
    answer_notes = {
        str(c["last_note_id"]): iid
        for iid, c in convs.items()
        if c.get("last_note_id") and c["status"] == "done"
    }
    for target, emoji, username in new_awards:
        if username != owner or emoji not in APPROVE_EMOJI:
            continue
        if not target.startswith("note:"):
            continue
        note_id = target.removeprefix("note:")
        iid = parked_notes.get(note_id) or answer_notes.get(note_id)
        if iid is not None:
            convs[iid]["next_reply_target"] = issue_reply_target(iid)
            convs[iid]["pending"].append(APPROVAL_MESSAGE)
            log.info("approval emoji (:%s:) on note -> resume issue !%s", emoji, iid)


# --------------------------------------------------------------------------- sessions / worker artifacts


def atomic_write_json(path: str | Path, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)


def read_json_file(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())


def utc_run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:12]


def pid_alive(pid: int | str | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True


def read_pid(path: str | Path | None) -> int | None:
    if not path:
        return None
    try:
        text = Path(path).read_text().strip()
    except FileNotFoundError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def terminate_process_group(
    pid: int | str | None, grace: int = TERM_GRACE_SECONDS
) -> None:
    if not pid:
        return
    pid = int(pid)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return
        except PermissionError:
            log.warning("cannot signal process group %s", pid)
            return
        except OSError:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                return
            except OSError as e:
                log.warning("cannot signal pid %s: %s", pid, e)
                return
        if sig == signal.SIGTERM:
            deadline = time.time() + grace
            while time.time() < deadline:
                if not pid_alive(pid):
                    return
                time.sleep(0.5)


def zshenv_path(base_env: dict[str, str]) -> str | None:
    """Return PATH after zsh has sourced ~/.zshenv, as launchd does not do this."""
    global _ZSHENV_PATH_LOADED, _ZSHENV_PATH_CACHE
    if _ZSHENV_PATH_LOADED:
        return _ZSHENV_PATH_CACHE
    _ZSHENV_PATH_LOADED = True
    try:
        result = subprocess.run(
            ["/bin/zsh", "-lc", 'print -r -- "$PATH"'],
            env=base_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    _ZSHENV_PATH_CACHE = lines[-1]
    return _ZSHENV_PATH_CACHE


# --------------------------------------------------------------------------- tmux
# Workers run in named tmux sessions so a human can `tmux attach` and watch the
# agent think turn-by-turn; a live session is also the liveness signal that
# separates a working run from a crashed one (a quiet stdout artifact looks the
# same either way). tmux is an enhancement, not a hard dependency: when it is
# absent, launch falls back to a bare detached process.

_TMUX_BIN_RESOLVED = False
_TMUX_BIN_CACHE: str | None = None


def tmux_bin() -> str | None:
    """Resolve tmux; launchd starts the watcher with a thin PATH, so probe the
    usual Homebrew/system prefixes when it is not already on PATH."""
    global _TMUX_BIN_RESOLVED, _TMUX_BIN_CACHE
    if _TMUX_BIN_RESOLVED:
        return _TMUX_BIN_CACHE
    _TMUX_BIN_RESOLVED = True
    found = shutil.which("tmux")
    if not found:
        for cand in ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux"):
            if os.path.exists(cand):
                found = cand
                break
    _TMUX_BIN_CACHE = found
    return found


def tmux_session_name(conv: dict) -> str | None:
    """Stable, globally-unique, tmux-safe session name for a conversation.

    The session-dir basename is already unique per conversation (slug + iid);
    tmux targets treat ':' and '.' specially, so fold anything outside
    [A-Za-z0-9_-] to a dash."""
    session_dir = conv.get("session_dir")
    if not session_dir:
        return None
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", Path(session_dir).name)
    return f"task-{safe}" if safe else None


def tmux_has_session(name: str | None) -> bool:
    """True if a session has a live pane.

    Successful worker panes use remain-on-exit so operators can inspect them.
    A retained dead pane is attachable but is not a live worker. The '=' prefix
    forces an exact-match target so task-repo-6 does not match task-repo-62.
    """
    tb = tmux_bin()
    if not tb or not name:
        return False
    try:
        result = subprocess.run(
            [tb, "list-panes", "-t", f"={name}", "-F", "#{pane_dead}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and any(
        line.strip() == "0" for line in result.stdout.splitlines()
    )


def tmux_kill_session(name: str | None) -> None:
    tb = tmux_bin()
    if not tb or not name:
        return
    try:
        subprocess.run(
            [tb, "kill-session", "-t", f"={name}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def tmux_launch_worker(
    session_name: str, cwd: str, worker_argv: list[str], env: dict
) -> subprocess.CompletedProcess:
    """Start the worker wrapper detached in a named tmux session.

    The wrapper runs through a login shell (`zsh -lc 'exec …'`) so PATH is sourced
    the way an interactive login would resolve it — the tmux server's own
    environment is unreliable for PATH (a thin launchd server env wins over
    new-session -e). Pi config paths are passed explicitly because an existing
    tmux server does not import arbitrary client environment variables. The
    login shell also receives those values as positional arguments so it can
    restore them after startup files run. Before `exec`, the pane enables
    remain-on-exit and records its worktree pointer so agent-link remains
    deterministic across chat resumes. This preserves completed output without
    adding a hold process."""
    tb = tmux_bin()
    worker = " ".join(shlex.quote(a) for a in worker_argv)
    config_names = [
        name for name in ("XDG_CONFIG_HOME", "PI_CODING_AGENT_DIR") if name in env
    ]
    restore_config = "".join(
        f'export {name}="${index}"; ' for index, name in enumerate(config_names, 1)
    )
    inner = (
        restore_config
        + f'{shlex.quote(tb)} set-option -pt "$TMUX_PANE" remain-on-exit on; '
        f'{shlex.quote(tb)} set-option -pt "$TMUX_PANE" @agent_worktree {shlex.quote(cwd)}; '
        f"exec {worker}"
    )
    login_cmd = shlex.join(
        [
            "/bin/zsh",
            "-lc",
            inner,
            "eastwatch-worker",
            *(env[name] for name in config_names),
        ]
    )
    command = [tb, "new-session", "-d", "-s", session_name, "-c", cwd]
    for name in config_names:
        command.extend(["-e", f"{name}={env[name]}"])
    command.append(login_cmd)
    tmux_kill_session(session_name)  # clear any stale same-named session first
    return subprocess.run(
        command,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=15,
        check=False,
    )


def worker_env(conv: dict) -> dict:
    """Worker env: include Pi config, ~/.zshenv PATH, and the issue host."""
    env = dict(os.environ)
    home = env.get("HOME")
    if home:
        env.setdefault("XDG_CONFIG_HOME", os.path.join(home, ".config"))
    xdg_config_home = env.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        env.setdefault(
            "PI_CODING_AGENT_DIR", os.path.join(xdg_config_home, "pi", "agent")
        )
    shell_path = zshenv_path(env)
    if shell_path:
        env["PATH"] = shell_path
    if conv.get("host"):
        env["GITLAB_HOST"] = conv["host"]
    return env


class WorkerCommandError(SessionError):
    def __init__(self, kind: str, message: str, exit_code: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.exit_code = exit_code


def append_text(path: str | Path, text: str) -> None:
    if not text:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(text)
        if not text.endswith("\n"):
            f.write("\n")


def command_tail(stdout: str, stderr: str, max_chars: int = 500) -> str:
    streams = [
        (label, text.strip())
        for label, text in (("stdout", stdout), ("stderr", stderr))
        if text.strip()
    ]
    if not streams:
        return ""
    separator = "; "
    overhead = sum(len(label) + 2 for label, _ in streams) + len(separator) * (
        len(streams) - 1
    )
    text_budget = max(0, max_chars - overhead)
    per_stream, remainder = divmod(text_budget, len(streams))
    parts = []
    for index, (label, text) in enumerate(streams):
        budget = per_stream + (1 if index < remainder else 0)
        tail = text[-budget:] if budget else ""
        parts.append(f"{label}: {tail}")
    return separator.join(parts)


def pi_session_observed(req: dict, sid: str | None, launched_at: float) -> bool:
    session_dir = Path(req.get("session_dir") or req["cwd"])
    if req["is_new"] and sid:
        return any(session_dir.glob(f"*_{sid}.jsonl"))
    session_file = req.get("session_file")
    if not session_file:
        return False
    try:
        return Path(session_file).stat().st_mtime >= launched_at
    except OSError:
        return False


def pi_launch_observation_request(req: dict, cmd: list[str]) -> dict:
    """Describe the session mode of this argv, independent of the logical request."""
    option_args = cmd[:-1]
    attempt_req = req.copy()
    if "--session" in option_args:
        attempt_req["is_new"] = False
        attempt_req["session_file"] = option_args[option_args.index("--session") + 1]
    elif "--session-id" in option_args:
        attempt_req["is_new"] = True
        attempt_req["session_file"] = None
    return attempt_req


def wait_for_pi_launch_window(
    proc: subprocess.Popen,
    req: dict,
    sid: str | None,
    launched_at: float,
    stdout_chunks: list[bytes],
    stderr_chunks: list[bytes],
    launch_deadline: float,
) -> None:
    """Hold pi.lock only until launch is past auth, then let the run continue."""
    selector = selectors.DefaultSelector()
    streams = []
    for stream, chunks in (
        (proc.stdout, stdout_chunks),
        (proc.stderr, stderr_chunks),
    ):
        if stream is None:
            continue
        streams.append(stream)
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, chunks)
    try:
        while time.time() < launch_deadline:
            if pi_session_observed(req, sid, launched_at):
                return
            if proc.poll() is not None:
                return
            timeout = min(0.2, max(0.0, launch_deadline - time.time()))
            for key, _ in selector.select(timeout):
                chunks = key.data
                try:
                    data = key.fileobj.read()
                except (BlockingIOError, InterruptedError):
                    continue
                if data:
                    chunks.append(data)
                    return
                if data == b"":
                    try:
                        selector.unregister(key.fileobj)
                    except KeyError:
                        pass
    finally:
        for stream in streams:
            try:
                selector.unregister(stream)
            except KeyError:
                pass
            try:
                os.set_blocking(stream.fileno(), True)
            except OSError:
                pass
        selector.close()


def stream_sinks() -> tuple:
    """Echo provider output to the hosting tmux pane (this wrapper's own stdout/
    stderr) only when actually running inside a session. Outside tmux the wrapper's
    stdout is a plain log file that append_text already owns, so echoing would
    double-write it."""
    if os.environ.get("TMUX"):
        return sys.stdout, sys.stderr
    return None, None


# How much of a tool call's salient arg (bash command, path) to show in the pane.
# Generous: the interesting part of `→ bash <cmd>` is the command, and the pane
# wraps a long line to a couple of visual rows. Only guards against a pathological
# multi-KB arg (e.g. an inline heredoc). The compact fleet-status table truncates
# far shorter on its own — it wants one line per conversation.
PANE_DETAIL_MAX = 400


def render_stream_line(line: str) -> str | None:
    """Render one claude stream-json event to a readable pane line, mirroring the
    vault dispatcher's jq filter (assistant text/thinking/tool_use + the final
    result). `--verbose` in text mode does NOT stream — realtime output requires
    --output-format stream-json — but raw JSONL in the pane is unreadable, so the
    pane gets rendered lines while the collector projects bounded state. Non-events
    (system, rate_limit_event, …) and unparseable lines are dropped from the pane."""
    line = line.strip()
    if not line:
        return None
    try:
        ev = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(ev, dict):
        return None
    etype = ev.get("type")
    if etype == "assistant":
        out = []
        for block in (ev.get("message") or {}).get("content", []) or []:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "text" and block.get("text"):
                out.append(block["text"].rstrip())
            elif bt == "thinking":
                out.append("· thinking…")
            elif bt == "tool_use":
                arg = block.get("input") or {}
                detail = (
                    arg.get("command") or arg.get("file_path") or arg.get("path") or ""
                )
                out.append(
                    f"→ {block.get('name', '?')} {str(detail)[:PANE_DETAIL_MAX]}".rstrip()
                )
        return "\n".join(out) + "\n" if out else None
    if etype == "result":
        return f"── {ev.get('subtype', 'done')}\n"
    return None


def pi_tool_detail(args) -> str:
    """Pick the most salient arg from a pi tool call to show after the name, so a
    row reads `→ bash pwd && git status` not a bare `→ bash`. Mirrors the claude
    renderer's command/path precedence."""
    if not isinstance(args, dict):
        return ""
    detail = (
        args.get("command")
        or args.get("file_path")
        or args.get("path")
        or args.get("file")
        or args.get("pattern")
        or args.get("query")
        or args.get("url")
        or args.get("subject")
        or args.get("action")
        or ""
    )
    return str(detail).replace("\n", " ")[:PANE_DETAIL_MAX]


def render_pi_line(line: str) -> str | None:
    """Render one pi `--mode json` NDJSON event for the pane. pi streams token
    deltas, thinking, and tool calls as events; text_delta fragments are written
    inline (no newline) so the reply reads naturally, other events as their own
    lines. Unrecognized events drop from the pane; optional raw capture is handled
    separately by the collector.

    Tool calls are rendered from the top-level `tool_execution_start` event (which
    carries the resolved `toolName` + full `args`), NOT from `message_update`'s
    incremental `toolcall_start`/`toolcall_delta`/`toolcall_end` — those stream the
    argument JSON character-by-character (~1200 events for a busy turn) and would
    flood the pane with bare `→ tool` lines with no name or detail."""
    line = line.strip()
    if not line:
        return None
    try:
        ev = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(ev, dict):
        return None
    etype = ev.get("type")
    if etype == "message_update":
        ame = ev.get("assistantMessageEvent") or {}
        at = ame.get("type")
        if at == "thinking_start":
            return "· thinking…\n"
        if at == "text_delta":
            return ame.get("delta") or None
        if at == "text_end":
            return "\n"
        return (
            None  # toolcall_* deltas are noise; tool_execution_start renders the call
        )
    if etype == "tool_execution_start":
        name = ev.get("toolName") or "tool"
        detail = pi_tool_detail(ev.get("args"))
        return f"→ {name} {detail}".rstrip() + "\n"
    if etype == "agent_end":
        return "── done\n"
    return None


class PaneWriter:
    """Echo provider output to tmux without retaining unbounded partial lines."""

    def __init__(self, sink, render=None, max_buffer_chars: int = LINE_BUFFER_BYTES):
        self.sink = sink
        self.render = render
        self.max_buffer_chars = max_buffer_chars
        self.buf = ""
        self.discarding_line = False

    def _emit(self, text: str) -> None:
        if self.sink is None or not text:
            return
        try:
            self.sink.write(text)
            self.sink.flush()
        except (OSError, ValueError):
            pass

    def feed(self, text: str) -> None:
        if self.sink is None or not text:
            return
        if self.render is None:
            self._emit(text)
            return

        offset = 0
        rendered = []
        while offset < len(text):
            if self.discarding_line:
                newline = text.find("\n", offset)
                if newline < 0:
                    break
                self.discarding_line = False
                offset = newline + 1
                continue
            newline = text.find("\n", offset)
            end = len(text) if newline < 0 else newline
            segment = text[offset:end]
            if len(self.buf) + len(segment) > self.max_buffer_chars:
                self.buf = ""
                self.discarding_line = newline < 0
            else:
                self.buf += segment
                if newline >= 0:
                    output = self.render(self.buf)
                    if output is not None:
                        rendered.append(output)
                    self.buf = ""
            if newline < 0:
                break
            offset = newline + 1
        if rendered:
            self._emit("".join(rendered))

    def close(self) -> None:
        if self.render is not None and not self.discarding_line and self.buf.strip():
            rendered = self.render(self.buf)
            if rendered is not None:
                self._emit(rendered)
        self.buf = ""
        self.discarding_line = False


def kill_process_group(proc: subprocess.Popen) -> None:
    """SIGTERM the process group, then SIGKILL if any group member survives."""
    pgid = proc.pid

    def group_exists() -> bool:
        proc.poll()  # Reap the group leader so a lone zombie does not look alive.
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        proc.terminate()
    deadline = time.time() + TERM_GRACE_SECONDS
    while time.time() < deadline:
        if not group_exists():
            return
        time.sleep(0.2)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        proc.kill()


def run_journal(req: dict) -> RunJournal:
    return RunJournal(req.get("journal_path"), str(req.get("run_id") or "unknown"))


def raw_capture_path(req: dict) -> str | None:
    # make_run_request records the dispatch-time flag decision. Trust that
    # capability path because an existing tmux server may not inherit a later
    # EASTWATCH_RAW_CAPTURE environment value.
    return req.get("raw_capture_path")


def write_stderr_tail(req: dict, collector: StreamCollector) -> None:
    path = req.get("stderr_path")
    if not path:
        return
    try:
        Path(path).write_text(collector.stderr_text)
    except OSError:
        pass


def drain_process(
    proc: subprocess.Popen,
    req: dict,
    timeout_seconds: int,
    stdout_prefix: bytes = b"",
    stderr_prefix: bytes = b"",
    render=None,
    pi_settled_exit_grace_seconds: float | None = None,
    collector: StreamCollector | None = None,
) -> tuple[int, StreamCollector]:
    """Drain a provider process into bounded collector state and the live pane."""
    if collector is None:
        journal = run_journal(req)
        if pi_settled_exit_grace_seconds is not None:
            collector = PiStreamCollector(
                journal,
                grace_seconds=pi_settled_exit_grace_seconds,
                raw_capture_path=raw_capture_path(req),
            )
            collector.start_attempt("pi", 1, 1)
        else:
            collector = ClaudeStreamCollector(
                journal, raw_capture_path=raw_capture_path(req)
            )

    out_sink, err_sink = stream_sinks()
    out_pane = PaneWriter(out_sink, render)
    err_pane = PaneWriter(err_sink, None)

    def tee(pane: PaneWriter, data: bytes, *, stdout: bool = False) -> None:
        pane.feed(data.decode(errors="replace"))
        if stdout:
            collector.feed(data)
        else:
            collector.feed_stderr(data)

    if stdout_prefix:
        tee(out_pane, stdout_prefix, stdout=True)
    if stderr_prefix:
        tee(err_pane, stderr_prefix)

    selector = selectors.DefaultSelector()
    streams = []
    for stream, pane, is_stdout in (
        (proc.stdout, out_pane, True),
        (proc.stderr, err_pane, False),
    ):
        if stream is None:
            continue
        streams.append(stream)
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, (pane, is_stdout))

    def close_streams() -> None:
        for stream in streams:
            try:
                stream.close()
            except OSError:
                pass

    open_streams = len(streams)
    deadline = time.time() + max(1, timeout_seconds)
    timed_out = False
    settled_exit = False

    def discover_provider_session() -> None:
        if not collector.session_id:
            return
        if isinstance(collector, PiStreamCollector):
            collector.discover_session(find_pi_session_file(req, collector.session_id))
        elif isinstance(collector, ClaudeStreamCollector):
            collector.discover_session(find_claude_session_file(collector.session_id))

    try:
        while open_streams > 0:
            discover_provider_session()
            now = time.time()
            remaining = deadline - now
            if remaining <= 0:
                timed_out = True
                break
            wait = min(0.5, remaining)
            if isinstance(collector, PiStreamCollector):
                settled_remaining = collector.remaining(now)
                if settled_remaining is not None:
                    if settled_remaining <= 0:
                        settled_exit = True
                        break
                    wait = min(wait, settled_remaining)
            for key, _ in selector.select(timeout=wait):
                pane, is_stdout = key.data
                try:
                    data = key.fileobj.read()
                except (BlockingIOError, InterruptedError):
                    continue
                if data == b"":
                    try:
                        selector.unregister(key.fileobj)
                    except KeyError:
                        pass
                    open_streams -= 1
                    continue
                if data:
                    tee(pane, data, stdout=is_stdout)
    finally:
        for stream in streams:
            try:
                selector.unregister(stream)
            except KeyError:
                pass
            try:
                os.set_blocking(stream.fileno(), True)
            except OSError:
                pass
        selector.close()

    if timed_out or settled_exit:
        if settled_exit and isinstance(collector, PiStreamCollector):
            marker = (
                "[pi-settled-safeguard] agent settled with willRetry=false but the process remained alive "
                f"for {pi_settled_exit_grace_seconds:g}s; terminating attempt "
                f"(outcome={collector.outcome or 'unknown'})\n"
            ).encode()
            collector.mark_guard_kill()
            tee(err_pane, marker)
        kill_process_group(proc)
        for stream, pane, is_stdout in (
            (proc.stdout, out_pane, True),
            (proc.stderr, err_pane, False),
        ):
            if stream is None:
                continue
            try:
                rest = stream.read()
            except (OSError, ValueError):
                rest = None
            if rest:
                tee(pane, rest, stdout=is_stdout)
        close_streams()
        out_pane.close()
        err_pane.close()
        discover_provider_session()
        if timed_out:
            collector.mark_timeout(timeout_seconds)
            write_stderr_tail(req, collector)
            raise WorkerCommandError(
                "timeout", f"command exceeded {timeout_seconds}s timeout"
            )
        proc.wait()
        code = (
            0
            if isinstance(collector, PiStreamCollector)
            and collector.outcome == "success"
            else (proc.returncode or 1)
        )
        collector.mark_exit(code)
        write_stderr_tail(req, collector)
        return code, collector

    proc.wait()
    close_streams()
    out_pane.close()
    discover_provider_session()
    err_pane.close()
    code = proc.returncode
    if (
        isinstance(collector, PiStreamCollector)
        and collector.deadline is not None
        and collector.outcome == "failure"
    ):
        code = code or 1
    collector.mark_exit(code)
    write_stderr_tail(req, collector)
    return code, collector


def finish_provider_process(
    proc: subprocess.Popen,
    req: dict,
    timeout_seconds: int,
    stdout_prefix: bytes = b"",
    stderr_prefix: bytes = b"",
    collector: PiStreamCollector | None = None,
) -> tuple[int, PiStreamCollector] | tuple[int, str, str]:
    grace = float(
        req.get("pi_settled_exit_grace_seconds", PI_SETTLED_EXIT_GRACE_SECONDS)
    )
    owns_collector = collector is None
    if collector is None:
        journal = run_journal(req)
        journal.emit(
            "run_started",
            model=req.get("model"),
            effort=req.get("effort"),
            provider="pi",
        )
        collector = PiStreamCollector(
            journal,
            grace_seconds=grace,
            raw_capture_path=raw_capture_path(req),
        )
        collector.start_attempt("pi", 1, 1)
    code, drained = drain_process(
        proc,
        req,
        timeout_seconds,
        stdout_prefix,
        stderr_prefix,
        render=render_pi_line,
        pi_settled_exit_grace_seconds=grace,
        collector=collector,
    )
    if owns_collector:
        return code, drained.stdout_text, drained.stderr_text
    return code, drained


def run_provider_command(
    req: dict,
    cmd: list[str],
    timeout_seconds: int,
    collector: ClaudeStreamCollector | None = None,
) -> tuple[int, ClaudeStreamCollector]:
    if collector is None:
        journal = run_journal(req)
        journal.emit(
            "run_started",
            model=req.get("model"),
            effort=req.get("effort"),
            provider="claude",
        )
        collector = ClaudeStreamCollector(
            journal, raw_capture_path=raw_capture_path(req)
        )
    proc = subprocess.Popen(
        cmd,
        cwd=req["cwd"],
        env=worker_env(req),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        close_fds=True,
    )
    Path(req["child_pid_path"]).write_text(f"{proc.pid}\n")
    code, drained = drain_process(
        proc,
        req,
        timeout_seconds,
        render=render_stream_line,
        collector=collector,
    )
    return code, drained


MR_EVIDENCE_RE = re.compile(r"https?://[^\s)>]+/merge_requests/\d+")
COMMIT_EVIDENCE_RE = re.compile(
    r"(?im)\b(?:commit(?:\s+(?:hash|sha))?|sha)\s*[:=]?\s*`?([0-9a-f]{7,40})\b"
)


def reply_evidence(reply: str) -> dict:
    mr_match = MR_EVIDENCE_RE.search(reply or "")
    commit_match = COMMIT_EVIDENCE_RE.search(reply or "")
    return {
        "mr_url": mr_match.group(0).rstrip("`.,") if mr_match else None,
        "commit_sha": commit_match.group(1) if commit_match else None,
    }


def run_claude_request(req: dict) -> dict:
    cmd = ["claude", "-p"]
    fmt = ["--output-format", "stream-json", "--verbose"]
    if req["is_new"]:
        cmd += [req["text"], "--model", req["model"], *fmt]
        if req.get("effort"):
            cmd += ["--effort", req["effort"]]
    else:
        cmd += [
            "--resume",
            req["session_id"],
            req["text"],
            "--model",
            req["model"],
            *fmt,
        ]
        if req.get("effort"):
            cmd += ["--effort", req["effort"]]

    journal = run_journal(req)
    journal.emit(
        "run_started",
        model=req.get("model"),
        effort=req.get("effort"),
        provider="claude",
    )
    collector = ClaudeStreamCollector(journal, raw_capture_path=raw_capture_path(req))
    try:
        code, collector = run_provider_command(
            req,
            cmd,
            int(req.get("timeout_seconds", RUN_TIMEOUT_SECONDS)),
            collector,
        )
        if code != 0:
            raise WorkerCommandError(
                "exit",
                f"claude exited {code}: {command_tail(collector.stdout_text, collector.stderr_text)}",
                code,
            )
        try:
            result = collector.result()
        except (IndexError, AttributeError) as e:
            raise WorkerCommandError("parse", f"cannot parse claude output: {e}") from e
        reply = result.get("result") or ""
        # The transcript is only fully written once claude exits, so resolve it
        # here as well as in the drain loop: this is the path Fleet shows for a
        # finished row.
        session_file = find_claude_session_file(result["session_id"])
        collector.discover_session(session_file)
        return {
            "ok": True,
            "reply": reply,
            "session_id": result["session_id"],
            "session_file": session_file,
            "model": req.get("model"),
            "effort": req.get("effort"),
            "pi_provider": None,
            **reply_evidence(reply),
            "completed_at": time.time(),
            "exit_code": code,
        }
    finally:
        write_stderr_tail(req, collector)
        collector.close()


def acquire_pi_lock(deadline: float):
    lockf = open(PI_LOCK, "w")
    while True:
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lockf
        except BlockingIOError:
            if time.time() >= deadline:
                lockf.close()
                raise WorkerCommandError("timeout", "timed out waiting for pi.lock")
            time.sleep(1)


def run_pi_provider_command(
    req: dict,
    cmd: list[str],
    timeout_seconds: int,
    sid: str | None,
    collector: PiStreamCollector | None = None,
) -> tuple[int, PiStreamCollector] | tuple[int, str, str]:
    owns_collector = collector is None
    if collector is None:
        journal = run_journal(req)
        journal.emit(
            "run_started",
            model=req.get("model"),
            effort=req.get("effort"),
            provider="pi",
        )
        collector = PiStreamCollector(
            journal,
            grace_seconds=float(
                req.get("pi_settled_exit_grace_seconds", PI_SETTLED_EXIT_GRACE_SECONDS)
            ),
            raw_capture_path=raw_capture_path(req),
        )
        provider = cmd[cmd.index("--provider") + 1] if "--provider" in cmd else "pi"
        collector.start_attempt(provider, 1, 1)

    deadline = time.time() + max(1, timeout_seconds)
    lockf = acquire_pi_lock(deadline)
    proc = None
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    observation_req = req
    try:
        launched_at = time.time()
        proc = subprocess.Popen(
            cmd,
            cwd=req["cwd"],
            env=worker_env(req),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
        )
        Path(req["child_pid_path"]).write_text(f"{proc.pid}\n")
        launch_deadline = min(deadline, time.time() + PI_LAUNCH_LOCK_SECONDS)
        observation_req = pi_launch_observation_request(req, cmd)
        wait_for_pi_launch_window(
            proc,
            observation_req,
            sid,
            launched_at,
            stdout_chunks,
            stderr_chunks,
            launch_deadline,
        )
    finally:
        lockf.close()
    remaining = int(deadline - time.time())
    if proc is None:
        raise WorkerCommandError("launch", "pi process was not started")
    collector.session_id = sid
    discovered = observation_req.get("session_file") or find_pi_session_file(
        observation_req, sid
    )
    collector.discover_session(discovered)
    result = finish_provider_process(
        proc,
        req,
        remaining,
        stdout_prefix=b"".join(stdout_chunks),
        stderr_prefix=b"".join(stderr_chunks),
        collector=collector,
    )
    code, drained = result
    if owns_collector:
        drained.close()
        return code, drained.stdout_text, drained.stderr_text
    return code, drained


PI_HEADROOM_PROVIDER = "headroom-copilot"
PI_DIRECT_PROVIDER = "github-copilot"
PI_FALLBACK_CONTINUATION = (
    "Continue from this session after a provider failure. Inspect the current repository and session state first, "
    "do not repeat completed work, and finish the user's request."
)


def pi_prompt_arg(text: str) -> str:
    """Return a pi message argv that cannot be mistaken for an @file token."""
    if text.startswith("@"):
        return " " + text
    return text


def pi_provider_order(model: str) -> tuple[str, ...]:
    if model.startswith("gpt-"):
        return PI_HEADROOM_PROVIDER, PI_DIRECT_PROVIDER
    return (PI_DIRECT_PROVIDER,)


def pi_model_arg(req: dict) -> str:
    return req["model"] + (f":{req['effort']}" if req.get("effort") else "")


def build_pi_argv(
    req: dict,
    provider: str,
    model: str,
    prompt: str,
    sid: str | None,
    session_file: str | None,
) -> list[str]:
    """Build one provider attempt without changing the logical model selection."""
    cmd = ["pi", "-p", "--provider", provider, "--model", model, "--mode", "json"]
    if session_file:
        return [*cmd, "--session", session_file, pi_prompt_arg(prompt)]
    return [
        *cmd,
        "--session-dir",
        req.get("session_dir") or req["cwd"],
        "--session-id",
        sid,
        pi_prompt_arg(prompt),
    ]


def find_pi_session_file(req: dict, sid: str | None) -> str | None:
    if not sid:
        return None
    files = sorted(Path(req.get("session_dir") or req["cwd"]).glob(f"*_{sid}.jsonl"))
    return str(files[-1]) if files else None


def snapshot_pi_session_file(session_file: str | None) -> tuple[int, int, str] | None:
    """Capture enough of a session file to detect any Headroom-side mutation."""
    if not session_file:
        return None
    digest = hashlib.sha256()
    try:
        path = Path(session_file)
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        stat = path.stat()
    except OSError:
        return None
    return stat.st_size, stat.st_mtime_ns, digest.hexdigest()


def pi_session_changed(
    session_file: str | None, before: tuple[int, int, str] | None
) -> bool:
    after = snapshot_pi_session_file(session_file)
    return after is not None and after != before


def pi_failure_message(provider: str, code: int, stdout: str, stderr: str) -> str:
    return f"pi {provider} exited {code}: {command_tail(stdout, stderr, max_chars=400)}"


def collector_note(collector: PiStreamCollector, message: str) -> None:
    collector.feed_stderr((message.rstrip("\n") + "\n").encode())


def run_pi_attempt(
    req: dict,
    provider: str,
    cmd: list[str],
    deadline: float,
    sid: str | None,
    collector: PiStreamCollector,
) -> tuple[int, PiStreamCollector]:
    """Run one logical provider attempt, retaining direct Copilot's auth-race retries."""
    attempts = 3 if provider == PI_DIRECT_PROVIDER else 1
    code = 1
    for attempt in range(attempts):
        remaining = int(deadline - time.time())
        if remaining <= 0:
            collector.mark_timeout(int(req.get("timeout_seconds", RUN_TIMEOUT_SECONDS)))
            raise WorkerCommandError("timeout", "pi command exceeded timeout")
        collector.start_attempt(provider, attempt + 1, attempts)
        collector_note(
            collector, f"[pi-provider] {provider} attempt {attempt + 1}/{attempts}"
        )
        provider_result = run_pi_provider_command(req, cmd, remaining, sid, collector)
        if len(provider_result) == 3:
            code, stdout, stderr = provider_result
            collector.feed(stdout.encode())
            collector.feed_stderr(stderr.encode())
            collector.mark_exit(code)
        else:
            code, collector = provider_result
        if code == 0:
            collector_note(collector, f"[pi-provider] {provider} success")
            collector.journal.emit(
                "provider_attempt",
                provider=provider,
                attempt=attempt + 1,
                total=attempts,
                outcome="success",
            )
            return code, collector
        collector.journal.emit(
            "provider_attempt",
            provider=provider,
            attempt=attempt + 1,
            total=attempts,
            outcome="failure",
            exit_code=code,
        )
        if (
            provider == PI_DIRECT_PROVIDER
            and collector.auth_error_seen
            and attempt < attempts - 1
        ):
            collector_note(
                collector,
                f"pi copilot auth race (attempt {attempt + 1}/3), sleeping 25s",
            )
            sleep_for = min(25, max(0, deadline - time.time()))
            if sleep_for:
                time.sleep(sleep_for)
            continue
        break
    collector_note(collector, f"[pi-provider] {provider} failed")
    return code, collector


def pi_result(
    req: dict,
    provider: str,
    sid: str | None,
    session_file: str | None,
    code: int,
    collector: PiStreamCollector,
) -> dict:
    if not session_file:
        session_file = find_pi_session_file(req, sid)
    if not session_file:
        raise WorkerCommandError("parse", "pi session file not found after launch")
    collector.discover_session(session_file)
    reply = collector.extract_reply()
    return {
        "ok": True,
        "reply": reply,
        "session_id": sid,
        "session_file": session_file,
        "model": req.get("model"),
        "effort": req.get("effort"),
        "pi_provider": provider,
        **reply_evidence(reply),
        "completed_at": time.time(),
        "exit_code": code,
    }


def run_pi_request(req: dict) -> dict:
    journal = run_journal(req)
    journal.emit(
        "run_started",
        model=req.get("model"),
        effort=req.get("effort"),
        provider="pi",
    )
    collector = PiStreamCollector(
        journal,
        grace_seconds=float(
            req.get("pi_settled_exit_grace_seconds", PI_SETTLED_EXIT_GRACE_SECONDS)
        ),
        raw_capture_path=raw_capture_path(req),
    )
    try:
        model = pi_model_arg(req)
        original_prompt = req["text"]
        sid = req.get("planned_session_id") or req.get("session_id")
        if req["is_new"] and not sid:
            sid = str(uuid.uuid4()).lower()
        session_file = None if req["is_new"] else req.get("session_file")
        collector.discover_session(session_file)
        providers = pi_provider_order(req["model"])
        deadline = time.time() + int(req.get("timeout_seconds", RUN_TIMEOUT_SECONDS))

        first_provider = providers[0]
        before = (
            snapshot_pi_session_file(session_file)
            if first_provider == PI_HEADROOM_PROVIDER
            else None
        )
        cmd = build_pi_argv(
            req, first_provider, model, original_prompt, sid, session_file
        )
        code, collector = run_pi_attempt(
            req, first_provider, cmd, deadline, sid, collector
        )
        if code == 0:
            return pi_result(req, first_provider, sid, session_file, code, collector)

        first_failure = pi_failure_message(
            first_provider,
            code,
            collector.stdout_text,
            collector.stderr_text,
        )
        if len(providers) == 1:
            raise WorkerCommandError("exit", first_failure, code)

        recovered_file = (
            find_pi_session_file(req, sid) if req["is_new"] else session_file
        )
        collector.discover_session(recovered_file)
        changed = pi_session_changed(recovered_file, before)
        tool_started = collector.attempt_tool_started
        if changed:
            fallback_prompt = PI_FALLBACK_CONTINUATION
            fallback_file = recovered_file
            fallback_sid = sid
            fallback_kind = "resume changed session"
        elif tool_started or collector.attempt_safety_unknown:
            reason = (
                "tool started"
                if tool_started
                else "malformed or oversized provider output made tool activity uncertain"
            )
            collector_note(
                collector,
                f"[pi-fallback] blocked: {reason} without recoverable session",
            )
            raise WorkerCommandError(
                "exit",
                f"{first_failure}; direct replay blocked because {reason}",
                code,
            )
        else:
            fallback_prompt = original_prompt
            fallback_file = session_file
            fallback_sid = sid
            fallback_kind = "retry original prompt"
            if req["is_new"]:
                fallback_sid = str(uuid.uuid4()).lower()

        if time.time() >= deadline:
            collector.mark_timeout(int(req.get("timeout_seconds", RUN_TIMEOUT_SECONDS)))
            raise WorkerCommandError("timeout", "pi command exceeded timeout")
        collector_note(
            collector,
            f"[pi-fallback] {first_provider} -> {PI_DIRECT_PROVIDER}: {fallback_kind}",
        )
        fallback_cmd = build_pi_argv(
            req,
            PI_DIRECT_PROVIDER,
            model,
            fallback_prompt,
            fallback_sid,
            fallback_file,
        )
        try:
            direct_code, collector = run_pi_attempt(
                req,
                PI_DIRECT_PROVIDER,
                fallback_cmd,
                deadline,
                fallback_sid,
                collector,
            )
        except WorkerCommandError as e:
            message = f"{e}; preceding {first_failure}"
            raise WorkerCommandError(e.kind, message, e.exit_code) from e
        if direct_code != 0:
            direct_failure = pi_failure_message(
                PI_DIRECT_PROVIDER,
                direct_code,
                collector.stdout_text,
                collector.stderr_text,
            )
            raise WorkerCommandError(
                "exit", f"{direct_failure}; preceding {first_failure}", direct_code
            )
        try:
            return pi_result(
                req,
                PI_DIRECT_PROVIDER,
                fallback_sid,
                fallback_file,
                direct_code,
                collector,
            )
        except WorkerCommandError as e:
            raise WorkerCommandError(
                e.kind, f"{e}; preceding {first_failure}", e.exit_code
            ) from e
    finally:
        write_stderr_tail(req, collector)
        collector.close()


def write_worker_error(
    req: dict, kind: str, message: str, exit_code: int | None = None
) -> None:
    atomic_write_json(
        req["error_path"],
        {
            "ok": False,
            "kind": kind,
            "message": message[-1000:],
            "exit_code": exit_code,
            "completed_at": time.time(),
        },
    )


def worker_main(request_path: str) -> int:
    req = read_json_file(request_path)
    Path(req["wrapper_pid_path"]).write_text(f"{os.getpid()}\n")
    try:
        if req["provider"] == "claude":
            result = run_claude_request(req)
        elif req["provider"] == "pi":
            result = run_pi_request(req)
        else:
            raise WorkerCommandError("parse", f"unknown provider: {req['provider']}")
        atomic_write_json(req["result_path"], result)
        return 0
    except WorkerCommandError as e:
        write_worker_error(req, e.kind, str(e), e.exit_code)
        return 1
    except Exception as e:  # noqa: BLE001 — normalize all wrapper failures into artifacts
        write_worker_error(req, e.__class__.__name__, str(e))
        return 1


def response_protocol(conv: dict) -> str:
    if conv.get("forge") == "vault":
        return (
            "Your final reply will be written verbatim into the task note's `## Result` section by "
            "the watcher — you do not edit the note yourself. Only your single final message is saved, "
            "so put your COMPLETE answer there; do not refer to an answer you wrote 'above' in an "
            "earlier message. End your reply with exactly one status line: `STATUS: done` (the card "
            "moves to `review`) or `STATUS: parked` (the card moves to `needs-input`) — for parked, "
            "state one concrete question AND your recommended default."
        )
    target = conv.get("reply_target") or {}
    destination = "merge request thread" if target.get("kind") == "mr" else "issue"
    return (
        f"Your final reply will be posted verbatim as a GitLab comment on the {destination}. "
        "Never post comments on the watched issue or any merge request yourself; return text here and the watcher posts it. "
        "Only your single final message is saved and posted — earlier messages you write while working are NOT posted, so put your "
        "COMPLETE answer in your final message; do not refer to an answer you wrote 'above' in an earlier message. "
        "End your reply with exactly one status line: `STATUS: done` if complete, or "
        "`STATUS: parked` if you need input — in which case state one concrete question "
        "AND your recommended default so a ✅ can approve it."
    )


def forge_board_path() -> Path:
    return env_path(
        "EASTWATCH_GLAB_BOARD",
        Path.home() / ".agents/skills/forge/scripts/glab-board",
    )


def prepare_issue_workspace(conv: dict) -> bool:
    """Run Forge onboarding before the first issue worker launch.

    Failure is non-fatal: the launch prompt carries the exact error and tells the
    worker to park rather than edit the shared checkout. A successful start
    changes the worker cwd to the returned worktree, avoiding Pi's fixed-cwd
    trap entirely.
    """
    if conv.get("workspace_prepared"):
        return True
    if conv.get("forge") == "vault":
        return True  # the vault IS the checkout; no Forge worktree onboarding
    if conv.get("anchor") != "issue" or conv.get("kind") not in TRIGGER_LABELS:
        return False
    checkout = conv.get("checkout")
    if not checkout or not Path(checkout).is_dir():
        return False
    command = forge_board_path()
    if not command.is_file():
        conv["workspace_error"] = f"Forge command is unavailable at {command}"
        return False
    mode = "research" if conv["kind"] == "agent::ready-research" else "work"
    try:
        result = subprocess.run(
            [str(command), "start", str(conv["issue_iid"]), mode, "--json"],
            cwd=checkout,
            env=worker_env(conv),
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        conv["workspace_error"] = f"Forge start failed: {e}"
        return False
    if result.returncode != 0:
        conv["workspace_error"] = (
            f"Forge start failed: {command_tail(result.stdout, result.stderr)}"
        )
        return False
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        worktree = Path(payload["worktree"]).expanduser()
        branch = str(payload["branch"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
        conv["workspace_error"] = f"Forge start returned invalid JSON: {e}"
        return False
    if not worktree.is_dir():
        conv["workspace_error"] = f"Forge worktree does not exist: {worktree}"
        return False
    conv["cwd"] = str(worktree)
    conv["worktree"] = str(worktree)
    conv["worktree_branch"] = branch
    conv["workspace_prepared"] = True
    conv.pop("workspace_error", None)
    log.info(
        "forge: prepared issue %s workspace %s (%s)",
        conv["issue_iid"],
        worktree,
        branch,
    )
    return True


def workspace_prompt(conv: dict) -> str | None:
    if conv.get("forge") == "vault":
        return (
            f"Workspace: you are running inside the Obsidian vault at `{conv['cwd']}`. Read "
            "AGENTS.md / CLAUDE.md and obey the vault rules. Work directly in the vault; do not "
            "create git worktrees or branches, and do not run any board/dispatch tooling."
        )
    if not conv.get("checkout"):
        return None
    command = forge_board_path()
    if conv.get("workspace_prepared"):
        iid = conv.get("issue_iid")
        ws = (
            f"Workspace: Forge onboarding is complete. You are running in issue worktree "
            f"`{conv['cwd']}` on branch `{conv.get('worktree_branch')}`. Read AGENTS.md / "
            "CLAUDE.md and obey the repo rules; do not run `start` again and never push main."
        )
        if conv.get("kind") == "agent::ready":
            ws += (
                " After implementation, verification, and a normal commit, run "
                f"`{command} finish {iid}` from this worktree. Normally add "
                "`--description-file <path>` with your concise summary and verification notes; "
                "omit it to let Forge derive the body from commits. Mention the verified MR URL "
                "or `!iid` in your final reply."
            )
        else:
            ws += " This is a research deliverable; report findings without creating an MR unless the issue explicitly asks for code."
        return ws
    exists = Path(conv["checkout"]).is_dir()
    ws = (
        f"Workspace: this project ({conv['project_path']} on {conv['host']}) has a local "
        f"checkout at `{conv['checkout']}`"
    )
    if exists and conv.get("anchor") == "issue" and conv.get("kind") in TRIGGER_LABELS:
        mode = " research" if conv.get("kind") == "agent::ready-research" else ""
        ws += (
            ". Forge onboarding did not complete automatically. Do not edit the shared checkout. "
            f"Run `{command} start {conv['issue_iid']}{mode} --json`, then use the returned absolute "
            "worktree as cwd for every command because Pi's cwd does not follow shell `cd`."
        )
        if conv.get("kind") == "agent::ready":
            ws += (
                f" After committing, run `{command} finish {conv['issue_iid']}`; normally pass "
                "`--description-file <path>` with concise summary and verification notes."
            )
        if conv.get("workspace_error"):
            ws += f" Automatic onboarding error: {conv['workspace_error']}"
    elif exists:
        ws += " — you are running in it. Read AGENTS.md / CLAUDE.md and obey the repo rules."
    else:
        ws += (
            f" which does not exist on this machine yet. Clone it first: "
            f"`git clone https://{conv['host']}/{conv['project_path']}.git {conv['checkout']}` "
            "— then read its AGENTS.md / CLAUDE.md and obey the repo rules. If the clone "
            "fails, end with STATUS: parked and say what you need."
        )
    return ws


def build_launch_prompt(conv: dict, msgs: list[str]) -> str:
    if conv.get("anchor") == "mr":
        parts = [
            f"You are answering an owner question on GitLab merge request {conv.get('mr_url')}: "
            f"!{conv.get('mr_iid')} {conv.get('mr_title')}."
        ]
        parts.append(
            "MR context:\n\n"
            f"Title: {conv.get('mr_title') or '(untitled)'}\n"
            f"Source branch: {conv.get('mr_source_branch') or '(unknown)'}\n"
            f"Target branch: {conv.get('mr_target_branch') or '(unknown)'}"
        )
        if conv.get("mr_desc"):
            parts.append(f"MR description:\n\n{conv['mr_desc']}")
        parts.append(
            "Answer the following question about this merge request:\n\n"
            + "\n\n".join(msgs)
        )
    elif conv.get("forge") == "vault":
        parts = [
            f"You are working an Obsidian task note at `{conv.get('note_path')}` in this vault: "
            f"**{conv['issue_title']}**. Do the task end-to-end."
        ]
        parts.append(
            "The watcher owns this card. Do NOT edit the note's frontmatter or `status:`, do NOT "
            "write a `## Result` section, and do NOT run `/work-task` or any board/dispatch tooling. "
            "Return your COMPLETE result as your final message — the watcher writes it into the note's "
            "`## Result` and moves the card. If the task changed other files in the vault, leave them "
            "uncommitted for the owner to review; do NOT git-commit."
        )
        parts.append(CHARTER_COMMON)
    else:
        parts = [
            f"You are working GitLab issue {conv['issue_url']}: {conv['issue_title']}."
        ]
        if conv["kind"] == "agent::ready":
            parts.append(
                "The issue has been labeled `agent::ready`: implement what the issue asks."
            )
            issue_iid = (
                conv.get("issue_iid") or conv["issue_url"].rstrip("/").split("/")[-1]
            )
            parts.append(
                "If you bypass `glab-board finish` and open a merge request manually, include this "
                "hidden marker in the MR description: "
                f"`<!-- eastwatch: source_project={conv['project_path']} source_issue_iid={issue_iid} "
                f"conversation_key={issue_iid} -->`. Mention the MR URL or `!iid` in your final reply."
            )
        elif conv["kind"] == "agent::ready-research":
            parts.append(
                "The issue has been labeled `agent::ready-research`: research the question in the "
                "issue and report your findings."
            )
        else:
            parts.append(
                "Answer the following question about this issue:\n\n"
                + "\n\n".join(msgs)
            )
        if conv["kind"] in TRIGGER_LABELS:
            parts.append(CHARTER_COMMON)
            parts.append(
                CHARTER_WORK if conv["kind"] == "agent::ready" else CHARTER_RESEARCH
            )
    ws = workspace_prompt(conv)
    if ws:
        parts.append(ws)
    if conv.get("briefing"):
        parts.append(conv["briefing"])
    if conv.get("issue_desc"):
        parts.append(f"Issue description:\n\n{conv['issue_desc']}")
    if conv.get("jira_context"):
        parts.append(conv["jira_context"])
    if conv.get("thread_context"):
        parts.append(conv["thread_context"])
    if conv["kind"] != "qa" and msgs:
        parts.append("Additional context:\n\n" + "\n\n".join(msgs))
    parts.append(response_protocol(conv))
    return "\n\n".join(parts)


def build_resume_message(msgs: list[str]) -> str:
    joined = "\n\n---\n\n".join(msgs)
    return f"{joined}\n\n(Reminder: end your reply with `STATUS: done` or `STATUS: parked` as before.)"


def split_status(text: str) -> tuple[str, str, bool]:
    """Strip the trailing STATUS line. Returns (body, status, had_status_line)."""
    lines = text.rstrip().splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if not lines[i].strip():
            continue
        m = STATUS_RE.match(lines[i])
        if m:
            return "\n".join(lines[:i]).rstrip(), m.group(1).lower(), True
        break
    return text.rstrip(), "done", False


def resume_footer(conv: dict) -> str:
    provider = conv.get("provider") or "unknown"
    model = conv.get("model") or "unknown"
    effort = conv.get("effort")
    answered_by = f"{provider}:{model}"
    if effort is not None:
        answered_by += f":{effort}"

    if provider == "claude":
        return (
            f"\n\n---\nmodel: [{answered_by}]\n"
            f"```\nclaude --resume {conv.get('session_id')}\n```\n"
            f"cwd: `{conv.get('cwd')}`"
        )

    session_file = str(conv.get("session_file"))
    try:
        session_file = str(Path("~") / Path(session_file).relative_to(Path.home()))
    except ValueError:
        pass
    return f"\n\n---\nmodel: [{answered_by}]\n```\npi --session {session_file}\n```"


# --------------------------------------------------------------------------- dispatch


def project_is_github(proj: dict) -> bool:
    return proj.get("forge") == "github"


def github_client(proj: dict) -> GitHubProject:
    client = proj.get("_github")
    if client is None:
        raise ConfigurationError(
            f"github project {proj.get('path')} has no client; forge not initialised"
        )
    return client


def github_write_status(client: GitHubProject, number, status_name: str) -> None:
    """Authoritative, guarded lifecycle Status write (no label write).

    Guards (pinned "every Status writer" contract):
    - resolve/ensure project membership; a freshly added item is fenced against
      the Item-added->Triage workflow (:meth:`set_status_fenced`);
    - re-read live Status immediately before mutating; a terminal write refuses
      to replace anything that is not an active Status, so a newer human drag is
      never clobbered;
    - confirm the write by read-back.

    It does NOT touch labels: the single shadow writer (:func:`github_shadow_write`,
    driven by the poller) mirrors this Status into the label shadow within one
    tick, so there is exactly one label writer on GitHub.
    """
    info = client.issue_item(number)
    item_id = info.get("item_id")
    added = False
    if item_id is None:
        content_id = info.get("content_id")
        if not content_id:
            raise ConfigurationError(
                f"github issue #{number} not found in {client.repo}"
            )
        item_id = client.add_item(content_id)
        added = True
    if not added:
        live = client.item_status_name(item_id)
        if live == status_name:
            return  # idempotent, no-op
        if (
            status_name in GITHUB_TERMINAL_STATUSES
            and live not in GITHUB_ACTIVE_STATUSES
        ):
            log.warning(
                "github: refusing terminal Status %s for issue #%s — live Status %s is not active "
                "(newer non-active transition wins)",
                status_name,
                number,
                live,
            )
            return
    if not client.set_status_fenced(item_id, status_name):
        log.warning(
            "github: Status %s for issue #%s not confirmed on read-back (workflow/verb race?)",
            status_name,
            number,
        )


def set_issue_labels(gl: GitLab, proj: dict, iid: str, add=(), remove=()):
    if project_is_vault(proj):
        return  # vault has no labels; the note's `status:` field is the authority
    if project_is_github(proj):
        # On GitHub the poller's single shadow writer owns every `agent::*` /
        # `triage::*` label. No other path writes lifecycle labels, so this is a
        # deliberate no-op — the shadow reconciles from Status within one tick.
        log.debug(
            "github: skipping direct label write on issue #%s (shadow owned by poller)",
            iid,
        )
        return
    params = {}
    if add:
        params["add_labels"] = ",".join(add)
    if remove:
        params["remove_labels"] = ",".join(remove)
    if params:
        gl.put(f"projects/{proj['id']}/issues/{iid}", **params)


def vault_write_status(proj: dict, conv: dict | None, label: str) -> None:
    """Move a task note's ``status:`` to the vault status a lifecycle label maps
    to. The working transition takes ownership (``agent`` -> ``in-progress``);
    terminal transitions are guarded so a human drag mid-run is not clobbered,
    and the settled note is committed once."""
    status = VAULT_LABEL_TO_STATUS.get(label)
    if status is None or conv is None:
        return
    abs_path = conv.get("abs_path")
    if not abs_path or not Path(abs_path).exists():
        log.warning(
            "vault: note for %s missing; cannot write status %s",
            conv.get("issue_iid"),
            status,
        )
        return
    board = vault_client(proj)
    if label in (WORKING_LABEL, RESEARCHING_LABEL):
        board.set_status(abs_path, status)  # take ownership; transient, uncommitted
    elif board.set_status_fenced(abs_path, status):
        board.git_commit(
            abs_path, f"agent({(conv.get('issue_title') or '')[:60]}): {status}"
        )


def set_issue_agent_label(
    gl: GitLab, proj: dict, iid: str, label: str, conv: dict | None = None
) -> None:
    """Set the sole workflow-state label, including on forges without scopes.

    On GitHub the label is a shadow, not the command: this writes the mapped
    authoritative Status (guarded); the poller's shadow writer mirrors it. On the
    vault the ``status:`` field is itself the authority, so this writes it.
    """
    if project_is_vault(proj):
        vault_write_status(proj, conv, label)
        return
    if project_is_github(proj):
        github_write_status(github_client(proj), int(iid), LABEL_TO_STATUS[label])
        return
    set_issue_labels(
        gl,
        proj,
        iid,
        add=[label],
        remove=[candidate for candidate in SHADOW_LABELS if candidate != label],
    )


def active_label(conv: dict) -> str:
    return (
        RESEARCHING_LABEL
        if conv.get("kind") == "agent::ready-research"
        else WORKING_LABEL
    )


def conversation_has_mr(ps: dict, conv_key: str) -> bool:
    return any(
        indexed_issue_iid(entry) == str(conv_key)
        for entry in ps.get("mr_index", {}).values()
    )


def label_issue_iid(conv: dict) -> str | None:
    iid = conv.get("issue_iid")
    return str(iid) if iid else None


def post_conversation_note(gl: GitLab, proj: dict, conv: dict, body: str) -> dict:
    target = conv.get("reply_target") or {}
    if project_is_vault(proj):
        # Write-back into the note: append the worker's reply as `## Result` and
        # stamp the session-id (rename-resume). The `status:` move + git commit
        # happen in the terminal `set_issue_agent_label` that follows.
        board = vault_client(proj)
        abs_path = conv.get("abs_path")
        if not abs_path or not Path(abs_path).exists():
            raise ValueError(
                f"vault conversation {conv.get('issue_iid')} note path missing: {abs_path}"
            )
        note = board.append_result_section(abs_path, body)
        if conv.get("session_id"):
            board.write_frontmatter_field(abs_path, "session-id", conv["session_id"])
        return note
    if project_is_github(proj):
        # GitHub conversations are issue-anchored; the worker's resolution is a
        # plain issue comment (no discussion threads / MR notes in slice 1).
        iid = str(target.get("issue_iid") or label_issue_iid(conv) or "")
        if not iid:
            raise ValueError("conversation has no issue reply target")
        return github_client(proj).post_comment(int(iid), body)
    if target.get("kind") == "mr":
        mr_iid = str(target["mr_iid"])
        discussion_id = target.get("discussion_id")
        if not discussion_id:
            try:
                discussion_id = find_note_discussion_id(
                    gl, proj, "mr", mr_iid, target.get("note_id")
                )
            except TransientDiscussionLookupError as e:
                log.warning(
                    "merge request !%s: could not resolve discussion for note %s; posting top-level note: %s",
                    mr_iid,
                    target.get("note_id"),
                    e,
                )
        if discussion_id:
            try:
                return gl.post(
                    f"projects/{proj['id']}/merge_requests/{mr_iid}/discussions/{discussion_id}/notes",
                    body=body,
                )
            except requests.RequestException as e:
                log.warning(
                    "merge request !%s: could not reply to discussion %s; posting top-level note: %s",
                    mr_iid,
                    discussion_id,
                    e,
                )
        return gl.post(
            f"projects/{proj['id']}/merge_requests/{mr_iid}/notes", body=body
        )

    iid = str(target.get("issue_iid") or label_issue_iid(conv) or "")
    if not iid:
        raise ValueError("conversation has no issue reply target")
    discussion_id = target.get("discussion_id")
    if not discussion_id:
        try:
            discussion_id = find_note_discussion_id(
                gl, proj, "issue", iid, target.get("note_id")
            )
        except TransientDiscussionLookupError as e:
            log.warning(
                "issue !%s: could not resolve discussion for note %s; posting top-level note: %s",
                iid,
                target.get("note_id"),
                e,
            )
    if discussion_id:
        try:
            return gl.post(
                f"projects/{proj['id']}/issues/{iid}/discussions/{discussion_id}/notes",
                body=body,
            )
        except requests.RequestException as e:
            log.warning(
                "issue !%s: could not reply to discussion %s; posting top-level note: %s",
                iid,
                discussion_id,
                e,
            )
    return gl.post(f"projects/{proj['id']}/issues/{iid}/notes", body=body)


def failure_body(
    failure_class: str, dropped_pending_count: int = 0, *, has_issue: bool = True
) -> str:
    review_sentence = (
        "This issue has been moved to `agent::failed` for review. "
        "Reply in the agent's thread (or use @agent) to retry."
        if has_issue
        else "Reply in the agent's thread (or use @agent) to retry."
    )
    body = (
        "⚠️ Agent session failed before it could post a final answer.\n\n"
        f"Failure class: `{failure_class}`.\n\n"
        "No worker output is included here. " + review_sentence
    )
    if dropped_pending_count:
        body += (
            "\n\n"
            f"Note: {dropped_pending_count} queued owner comment(s) arrived while the failed run "
            "was active and were dropped with the failed session; repost anything that should be "
            "included in a retry."
        )
    return body


def archive_current_run(conv: dict) -> None:
    if conv.get("current_run"):
        conv["last_run"] = dict(conv["current_run"])


def mark_failed(
    gl: GitLab, proj: dict, conv_key: str, conv: dict, failure_class: str
) -> None:
    dropped_pending_count = len(conv.get("pending") or [])
    issue_iid = label_issue_iid(conv)
    conv["status"] = "failed"
    archive_current_run(conv)
    conv["current_run"] = None
    conv["pending"] = []
    conv["parked_note_id"] = None
    conv["last_reply_body_hash"] = None
    note_id = None
    try:
        note = post_conversation_note(
            gl,
            proj,
            conv,
            failure_body(
                failure_class, dropped_pending_count, has_issue=bool(issue_iid)
            ),
        )
        note_id = note["id"]
        conv["last_note_id"] = note_id
    except (requests.RequestException, ValueError) as e:
        log.warning("conversation %s: could not post failure note: %s", conv_key, e)
    if issue_iid:
        try:
            set_issue_agent_label(gl, proj, issue_iid, FAILED_LABEL, conv)
        except requests.RequestException as e:
            log.warning("issue !%s: could not update failure labels: %s", issue_iid, e)
    log.info(
        "conversation %s: failed (%s) — posted note %s",
        conv_key,
        failure_class,
        note_id,
    )


def collect_success(
    gl: GitLab,
    proj: dict,
    ps: dict,
    conv_key: str,
    conv: dict,
    result: dict,
    state: dict,
) -> None:
    run = conv.get("current_run") or {}
    if run.get("reply_target"):
        conv["reply_target"] = run["reply_target"]
    issue_iid = label_issue_iid(conv)
    if result.get("session_id"):
        conv["session_id"] = result["session_id"]
    if result.get("session_file"):
        conv["session_file"] = result["session_file"]
    reply = result.get("reply") or ""
    body, status, had_status = split_status(reply)
    if not had_status:
        log.warning(
            "conversation %s: reply missing/garbled STATUS line — treating as done",
            conv_key,
        )
    if status == "parked" and conv.get("anchor") == "mr":
        status = "done"
        log.info(
            "conversation %s: MR-anchored parked Q&A treated as done; owner can resume with fresh @agent",
            conv_key,
        )
    if not body.strip():
        body = "(agent produced no reply text)"
    if issue_iid:
        capture_mrs_from_reply(gl, proj, ps, issue_iid, conv, reply)
    save_state(state)
    note = post_conversation_note(gl, proj, conv, body + resume_footer(conv))
    conv["last_note_id"] = note["id"]
    conv["last_reply_body_hash"] = body_hash(body)
    run["completed_at"] = float(result.get("completed_at") or time.time())
    archive_current_run(conv)
    conv["current_run"] = None
    if status == "parked":
        conv["status"] = "parked"
        conv["parked_note_id"] = note["id"]
        terminal_label = PARKED_LABEL
    else:
        conv["status"] = "done"
        conv["parked_note_id"] = None
        terminal_label = (
            MR_READY_LABEL if conversation_has_mr(ps, conv_key) else FOR_HUMAN_LABEL
        )
    if issue_iid:
        try:
            set_issue_agent_label(gl, proj, issue_iid, terminal_label, conv)
        except requests.RequestException as e:
            log.warning(
                "issue !%s: posted note %s but could not update labels: %s",
                issue_iid,
                note["id"],
                e,
            )
    log.info("conversation %s: %s — posted note %s", conv_key, status, note["id"])


def collect_failure(
    gl: GitLab, proj: dict, conv_key: str, conv: dict, error: dict
) -> None:
    kind = error.get("kind") or "worker_error"
    message = error.get("message") or ""
    log.error(
        "worker failed for conversation %s (%s): %s", conv_key, kind, message[-500:]
    )
    tmux_kill_session((conv.get("current_run") or {}).get("tmux_session"))
    mark_failed(gl, proj, conv_key, conv, kind)


def recover_interrupted_launch(
    gl: GitLab, proj: dict, conv_key: str, conv: dict, run: dict
) -> None:
    messages = run.get("messages") or []
    conv["pending"] = messages + conv.get("pending", [])
    conv["status"] = run.get("previous_status") or "new"
    archive_current_run(conv)
    conv["current_run"] = None
    issue_iid = label_issue_iid(conv)
    if issue_iid:
        try:
            set_issue_labels(gl, proj, issue_iid, remove=[active_label(conv)])
        except requests.RequestException as e:
            log.warning(
                "issue !%s: could not clear interrupted launch label: %s", issue_iid, e
            )
    log.warning(
        "conversation %s: recovered interrupted launch; messages requeued", conv_key
    )


def synthesize_error(
    run: dict, kind: str, message: str, exit_code: int | None = None
) -> dict:
    error = {
        "ok": False,
        "kind": kind,
        "message": message,
        "exit_code": exit_code,
        "completed_at": time.time(),
    }
    if run.get("error_path"):
        atomic_write_json(run["error_path"], error)
    return error


def run_wrapper_pid(run: dict) -> int | None:
    return run.get("wrapper_pid") or read_pid(run.get("wrapper_pid_path"))


def collect_terminal_artifact(
    gl: GitLab,
    proj: dict,
    ps: dict,
    state: dict,
    iid: str,
    conv: dict,
    run: dict,
) -> bool:
    """Collect result/error if present. Corrupt artifacts are terminal failures."""
    result_path = Path(run["result_path"])
    error_path = Path(run["error_path"])
    if result_path.exists():
        try:
            result = read_json_file(result_path)
        except (json.JSONDecodeError, OSError) as e:
            log.warning(
                "issue !%s: corrupt result artifact for run %s: %s",
                iid,
                run.get("run_id"),
                e,
            )
            collect_failure(
                gl,
                proj,
                iid,
                conv,
                {
                    "kind": e.__class__.__name__,
                    "message": f"could not read result.json: {e}",
                },
            )
            return True
        collect_success(gl, proj, ps, iid, conv, result, state)
        return True
    if error_path.exists():
        try:
            error = read_json_file(error_path)
        except (json.JSONDecodeError, OSError) as e:
            log.warning(
                "issue !%s: corrupt error artifact for run %s: %s",
                iid,
                run.get("run_id"),
                e,
            )
            collect_failure(
                gl,
                proj,
                iid,
                conv,
                {
                    "kind": e.__class__.__name__,
                    "message": f"could not read error.json: {e}",
                },
            )
            return True
        collect_failure(gl, proj, iid, conv, error)
        return True
    return False


def collect_terminal_artifact_safely(
    gl: GitLab,
    proj: dict,
    ps: dict,
    state: dict,
    iid: str,
    conv: dict,
    run: dict,
) -> bool:
    try:
        collected = collect_terminal_artifact(gl, proj, ps, state, iid, conv, run)
    except (requests.RequestException, ValueError) as e:
        log.warning(
            "conversation %s: could not collect run %s: %s", iid, run.get("run_id"), e
        )
        return True
    if collected:
        save_state(state)
        return True
    return False


def collect_and_heal_runs(gl: GitLab, proj: dict, ps: dict, state: dict) -> None:
    """Collect detached worker artifacts; working is active, not stale-by-default."""
    for iid, conv in list(ps["conversations"].items()):
        run = conv.get("current_run")
        if not run:
            if conv.get("status") == "working":
                mark_failed(gl, proj, iid, conv, "missing_current_run")
                save_state(state)
            continue
        if collect_terminal_artifact_safely(gl, proj, ps, state, iid, conv, run):
            continue

        pid = run_wrapper_pid(run)
        # For a tmux-hosted run, a live session — not a pid — is the liveness signal:
        # a quiet stdout artifact looks identical whether the worker is thinking or
        # dead, but the session exists only while the worker runs and self-exits when
        # it finishes. Recompute the expected name so a session that outlived a
        # launch-time crash (before state recorded it) is adopted, not double-run.
        expected_session = run.get("tmux_session") or tmux_session_name(conv)
        session_alive = tmux_has_session(expected_session)
        if session_alive and not run.get("tmux_session"):
            run["tmux_session"] = expected_session
            run["launch_state"] = "working"
            save_state(state)
        hosted = bool(run.get("tmux_session"))

        if run.get("launch_state") == "launching" and not pid and not session_alive:
            recover_interrupted_launch(gl, proj, iid, conv, run)
            save_state(state)
            continue
        if pid and run.get("wrapper_pid") != pid:
            run["wrapper_pid"] = pid
            if not hosted:
                run["launch_state"] = "working"
            save_state(state)
        deadline = float(run.get("deadline_at") or 0)
        if deadline and time.time() >= deadline:
            if collect_terminal_artifact_safely(gl, proj, ps, state, iid, conv, run):
                continue
            terminate_process_group(pid)
            child_pid = read_pid(run.get("child_pid_path"))
            if child_pid:
                terminate_process_group(child_pid)
            if hosted:
                tmux_kill_session(run.get("tmux_session"))
            if collect_terminal_artifact_safely(gl, proj, ps, state, iid, conv, run):
                continue
            collect_failure(
                gl,
                proj,
                iid,
                conv,
                synthesize_error(run, "timeout", "worker deadline exceeded"),
            )
            save_state(state)
            continue
        alive = session_alive if hosted else (bool(pid) and pid_alive(pid))
        if alive:
            conv["status"] = "working"
            continue
        if collect_terminal_artifact_safely(gl, proj, ps, state, iid, conv, run):
            continue
        child_pid = read_pid(run.get("child_pid_path"))
        if child_pid:
            terminate_process_group(child_pid)
        if collect_terminal_artifact_safely(gl, proj, ps, state, iid, conv, run):
            continue
        reason = (
            "tmux session ended without result"
            if hosted
            else "wrapper pid exited without result"
        )
        collect_failure(
            gl, proj, iid, conv, synthesize_error(run, "disappeared", reason)
        )
        save_state(state)


def make_run_request(
    conv: dict, msgs: list[str], is_new: bool, text: str, run_dir: Path, run_id: str
) -> dict:
    request_path = run_dir / "request.json"
    result_path = run_dir / "result.json"
    error_path = run_dir / "error.json"
    wrapper_pid_path = run_dir / "wrapper.pid"
    child_pid_path = run_dir / "child.pid"
    req = {
        "schema_version": 1,
        "run_id": run_id,
        "provider": conv["provider"],
        "model": conv["model"],
        "effort": conv.get("effort"),
        "is_new": is_new,
        "text": text,
        "messages": msgs,
        "cwd": conv["cwd"],
        "session_dir": conv.get("session_dir"),
        "session_id": conv.get("session_id"),
        "session_file": conv.get("session_file"),
        "host": conv.get("host"),
        "project_path": conv.get("project_path"),
        "reply_target": conv.get("reply_target"),
        "timeout_seconds": RUN_TIMEOUT_SECONDS,
        "request_path": str(request_path),
        "result_path": str(result_path),
        "error_path": str(error_path),
        "wrapper_pid_path": str(wrapper_pid_path),
        "child_pid_path": str(child_pid_path),
        "stderr_path": str(run_dir / "stderr.log"),
        "journal_path": str(run_dir / "run.jsonl"),
        "raw_capture_path": (
            str(run_dir / "stream.log")
            if getenv("EASTWATCH_RAW_CAPTURE") == "1"
            else None
        ),
    }
    if conv["provider"] == "pi" and is_new:
        req["planned_session_id"] = str(uuid.uuid4()).lower()
    return req


def start_one(gl: GitLab, proj: dict, ps: dict, conv_key: str, state: dict) -> bool:
    conv = ps["conversations"][conv_key]
    if conv.get("current_run"):
        return False
    msgs, conv["pending"] = conv.get("pending", []), []
    run_reply_target = conv.pop("next_reply_target", None) or conv.get("reply_target")
    conv["reply_target"] = run_reply_target
    is_new = not (conv.get("session_id") or conv.get("session_file"))
    if is_new:
        prepare_issue_workspace(conv)
    text = build_launch_prompt(conv, msgs) if is_new else build_resume_message(msgs)
    run_id = utc_run_id()
    run_dir = Path(conv["session_dir"]) / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    req = make_run_request(conv, msgs, is_new, text, run_dir, run_id)
    atomic_write_json(req["request_path"], req)
    started_at = time.time()
    previous_status = conv.get("status")
    conv["current_run"] = {
        "run_id": run_id,
        "provider": conv["provider"],
        "is_new": is_new,
        "messages": msgs,
        "reply_target": run_reply_target,
        "started_at": started_at,
        "deadline_at": started_at + RUN_TIMEOUT_SECONDS,
        "wrapper_pid": None,
        "launch_state": "launching",
        "previous_status": previous_status,
        "run_dir": str(run_dir),
        "request_path": req["request_path"],
        "result_path": req["result_path"],
        "error_path": req["error_path"],
        "wrapper_pid_path": req["wrapper_pid_path"],
        "child_pid_path": req["child_pid_path"],
        "stderr_path": req["stderr_path"],
        "journal_path": req["journal_path"],
        "raw_capture_path": req["raw_capture_path"],
        "tmux_session": None,
    }
    conv["status"] = "working"
    issue_iid = label_issue_iid(conv)
    if issue_iid:
        set_issue_agent_label(gl, proj, issue_iid, active_label(conv), conv)
    save_state(state)
    log.info(
        "dispatch: conversation %s %s %s:%s:%s run %s",
        conv_key,
        "launch" if is_new else "resume",
        conv["provider"],
        conv["model"],
        conv.get("effort"),
        run_id,
    )
    entrypoint = REPOSITORY_ROOT / "eastwatch"
    worker_argv = [
        sys.executable,
        str(entrypoint) if entrypoint.is_file() else "-m",
    ]
    if not entrypoint.is_file():
        worker_argv.append("eastwatch.watcher")
    worker_argv.extend(["--worker", req["request_path"]])

    def fail_launch(message: str) -> bool:
        synthesize_error(conv["current_run"], "launch", message)
        collect_failure(
            gl, proj, conv_key, conv, read_json_file(conv["current_run"]["error_path"])
        )
        save_state(state)
        return False

    session_name = tmux_session_name(conv)
    if tmux_bin() and session_name:
        try:
            result = tmux_launch_worker(
                session_name, conv["cwd"], worker_argv, worker_env(conv)
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return fail_launch(f"tmux launch failed: {e}")
        if result.returncode != 0:
            return fail_launch(
                f"tmux new-session exited {result.returncode}: {command_tail(result.stdout, result.stderr)}"
            )
        # The wrapper writes its own pid to wrapper_pid_path; the heal loop reads it.
        # A live session — not a pid — is the liveness signal for a hosted run.
        conv["current_run"]["tmux_session"] = session_name
        conv["current_run"]["launch_state"] = "working"
        save_state(state)
        log.info(
            "dispatch: conversation %s hosted in tmux session %s",
            conv_key,
            session_name,
        )
        return True

    # Fallback: no tmux available — provider output still flows through the
    # collector; discard the otherwise-unused wrapper stdout.
    err = open(req["stderr_path"], "a")
    try:
        proc = subprocess.Popen(
            worker_argv,
            cwd=conv["cwd"],
            env=worker_env(conv),
            stdout=subprocess.DEVNULL,
            stderr=err,
            start_new_session=True,
            close_fds=True,
        )
    except Exception as e:  # noqa: BLE001 — surface launch failures through normal failure path
        err.close()
        return fail_launch(str(e))
    finally:
        err.close()
    conv["current_run"]["wrapper_pid"] = proc.pid
    conv["current_run"]["launch_state"] = "working"
    Path(req["wrapper_pid_path"]).write_text(f"{proc.pid}\n")
    save_state(state)
    return True


def active_counts(state: dict) -> tuple[int, int]:
    active_total = 0
    active_pi = 0
    for ps in state.get("projects", {}).values():
        for conv in ps.get("conversations", {}).values():
            run = conv.get("current_run")
            if not run:
                continue
            active_total += 1
            if run.get("provider") == "pi":
                active_pi += 1
    return active_total, active_pi


def run_directory(run: dict) -> Path | None:
    if run.get("run_dir"):
        return Path(run["run_dir"])
    if run.get("request_path"):
        return Path(run["request_path"]).parent
    return None


def run_completed_timestamp(run: dict) -> float:
    try:
        if run.get("completed_at"):
            return float(run["completed_at"])
    except (TypeError, ValueError):
        pass
    for key in ("result_path", "error_path"):
        raw_path = run.get(key)
        if not raw_path:
            continue
        path = Path(raw_path)
        try:
            artifact = json.loads(path.read_text())
            if artifact.get("completed_at"):
                return float(artifact["completed_at"])
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
        try:
            return path.stat().st_mtime
        except OSError:
            pass
    directory = run_directory(run)
    if directory is not None:
        try:
            return directory.stat().st_mtime
        except OSError:
            pass
    return 0.0


def run_succeeded(run: dict) -> bool:
    raw_path = run.get("result_path")
    if not raw_path:
        return False
    path = Path(raw_path)
    try:
        result = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    return isinstance(result, dict) and result.get("ok") is True


def _unlink_retained(path: Path, counts: dict, kind: str) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return
    counts[kind] = counts.get(kind, 0) + 1


def _raw_capture_candidates(directory: Path) -> tuple[Path, ...]:
    return directory / "stream.log", directory / "stream.log.gz"


def sweep_artifacts(
    state: dict,
    *,
    now: float | None = None,
    include_legacy: bool = True,
) -> dict:
    """Apply bounded artifact retention without touching active worker runs.

    Automatic sweeps only manage journal-era runs. Legacy bulk reclamation is
    deliberately reserved for the explicit ``sweep`` command.
    """
    current_time = time.time() if now is None else now
    active_dirs: set[Path] = set()
    retained_runs: dict[Path, dict] = {}
    for project in state.get("projects", {}).values():
        for conv in project.get("conversations", {}).values():
            current = conv.get("current_run")
            current_dir = run_directory(current) if current else None
            if current_dir is not None:
                active_dirs.add(current_dir.resolve())
            last = conv.get("last_run")
            last_dir = run_directory(last) if last else None
            if last_dir is not None:
                retained_runs[last_dir.resolve()] = last

    counts = {"tails": 0, "raw_captures": 0, "orphans": 0}
    if not CONVOS_DIR.is_dir():
        return counts

    for directory in CONVOS_DIR.glob("*/runs/*"):
        if not directory.is_dir():
            continue
        resolved = directory.resolve()
        if resolved in active_dirs:
            continue
        run = retained_runs.get(resolved)
        journal_path = (
            Path(run["journal_path"])
            if run and run.get("journal_path")
            else directory / "run.jsonl"
        )
        if not include_legacy and not journal_path.is_file():
            continue
        try:
            directory_mtime = directory.stat().st_mtime
        except OSError:
            continue

        for raw_path in _raw_capture_candidates(directory):
            try:
                raw_age = current_time - raw_path.stat().st_mtime
            except OSError:
                continue
            if raw_age >= RAW_CAPTURE_RETENTION_SECONDS:
                _unlink_retained(raw_path, counts, "raw_captures")

        if run is not None:
            completed_at = run_completed_timestamp(run)
            retention = (
                SUCCESS_TAIL_RETENTION_SECONDS
                if run_succeeded(run)
                else FAILURE_TAIL_RETENTION_SECONDS
            )
            if completed_at and current_time - completed_at >= retention:
                candidates = {
                    directory / "stdout.log",
                    directory / "stderr.log",
                }
                for key in ("stdout_path", "stderr_path"):
                    if run.get(key):
                        candidates.add(Path(run[key]))
                for path in candidates:
                    _unlink_retained(path, counts, "tails")
            continue

        orphan_age = current_time - directory_mtime
        if orphan_age >= ORPHAN_RUN_RETENTION_SECONDS:
            try:
                shutil.rmtree(directory)
            except OSError:
                continue
            counts["orphans"] += 1

    return counts


def maybe_sweep_artifacts(state: dict, *, now: float | None = None) -> bool:
    current_time = time.time() if now is None else now
    maintenance = state.setdefault("maintenance", {})
    try:
        last_sweep = float(maintenance.get("last_sweep_at") or 0)
    except (TypeError, ValueError):
        last_sweep = 0
    if current_time - last_sweep < SWEEP_INTERVAL_SECONDS:
        return False
    counts = sweep_artifacts(state, now=current_time, include_legacy=False)
    maintenance["last_sweep_at"] = current_time
    log.info(
        "artifact sweep: %d tail(s), %d raw capture(s), %d orphan run(s)",
        counts["tails"],
        counts["raw_captures"],
        counts["orphans"],
    )
    return True


# --------------------------------------------------------------------------- cycle


def project_config_key(proj: dict) -> str:
    return f"{proj['host']}/{proj['path']}"


def validate_unique_project_keys(projects: list[dict]) -> None:
    seen = set()
    for proj in projects:
        key = project_config_key(proj)
        if key in seen:
            raise ConfigurationError(f"duplicate project config for {key}")
        seen.add(key)


def validate_github_projects(projects: list[dict]) -> None:
    """`forge: github` projects need a canonical project node id — fail closed.

    Title-based lookup is retired; absence or a malformed id refuses to start
    with a setup instruction rather than silently guessing a board.
    """
    for proj in projects:
        if not project_is_github(proj):
            continue
        pid = proj.get("github_project_id")
        if not (isinstance(pid, str) and pid.startswith("PVT_")):
            raise ConfigurationError(
                f"project {proj.get('path')!r}: forge: github requires a canonical "
                f"`github_project_id` (a PVT_... project node id), got {pid!r}; "
                "run `glab-board setup --board` to create-or-find it"
            )
        if "/" not in (proj.get("path") or ""):
            raise ConfigurationError(
                f"project {proj.get('path')!r}: forge: github requires `path: owner/name`"
            )


def validate_vault_projects(projects: list[dict], *, check_fs: bool = False) -> None:
    """`forge: vault` projects need a `vault_path` and only the `agent::ready` trigger.

    The string/trigger checks are I/O-free (safe for preflight); ``check_fs``
    additionally verifies the path is a directory (and warns if commits are on
    but it is not a git repo) — the runtime-only pass.
    """
    for proj in projects:
        if not project_is_vault(proj):
            continue
        vault_path = proj.get("vault_path")
        if not (isinstance(vault_path, str) and vault_path.strip()):
            raise ConfigurationError(
                f"project {proj.get('path')!r}: forge: vault requires a non-empty `vault_path`"
            )
        triggers = proj.get("triggers", []) or []
        unknown = [t for t in triggers if t != TRIGGER_LABELS[0]]
        if unknown:
            raise ConfigurationError(
                f"project {proj.get('path')!r}: forge: vault supports only "
                f"triggers [{TRIGGER_LABELS[0]}], got {triggers}"
            )
        if check_fs:
            resolved = Path(vault_path).expanduser()
            if not resolved.is_dir():
                raise ConfigurationError(
                    f"project {proj.get('path')!r}: vault_path {vault_path!r} is not a directory"
                )
            if proj.get("commit_results", True) and not (resolved / ".git").exists():
                log.warning(
                    "project %s: commit_results is on but %s is not a git repo — commits will be skipped",
                    proj.get("path"),
                    vault_path,
                )


def project_poll_state_snapshot(ps: dict) -> dict:
    """Copy only the fields remote pollers mutate, leaving live state untouched."""
    return {
        "bootstrapped": bool(ps.get("bootstrapped", False)),
        "last_event_id": ps.get("last_event_id", 0),
        "consumed_label_event_ids": list(ps.get("consumed_label_event_ids", [])),
        "award_keys": [list(k) for k in ps.get("award_keys", [])],
        # GitHub Status poller state. `github_restore` is a read-only map of the
        # Status implied by each open conversation, computed here (single-thread)
        # so prune recovery in the parallel fetch never touches live conversations.
        "github_observations": copy.deepcopy(ps.get("github_observations", {})),
        "github_outbox": copy.deepcopy(ps.get("github_outbox", {})),
        "github_restore": github_restore_map(ps),
        # Vault status poller state (local board); empty for GitLab/GitHub.
        "vault_observations": copy.deepcopy(ps.get("vault_observations", {})),
        "vault_outbox": copy.deepcopy(ps.get("vault_outbox", {})),
        "conversations": ps.get("conversations", {}),
    }


def github_restore_map(ps: dict) -> dict:
    """Number -> durable Status implied by the watcher's own conversation record."""
    out = {}
    for key in ps.get("conversations", {}):
        status = github_conversation_status(ps, key)
        if status:
            out[str(key)] = status
    return out


def apply_project_poll_state(ps: dict, poll_state: dict) -> None:
    ps["last_event_id"] = poll_state.get("last_event_id", ps.get("last_event_id", 0))
    ps["consumed_label_event_ids"] = list(
        poll_state.get(
            "consumed_label_event_ids", ps.get("consumed_label_event_ids", [])
        )
    )
    ps["award_keys"] = list(poll_state.get("award_keys", ps.get("award_keys", [])))
    if "github_observations" in poll_state:
        ps["github_observations"] = poll_state["github_observations"]
    if "github_outbox" in poll_state:
        ps["github_outbox"] = poll_state["github_outbox"]
    if "vault_observations" in poll_state:
        ps["vault_observations"] = poll_state["vault_observations"]
    if "vault_outbox" in poll_state:
        ps["vault_outbox"] = poll_state["vault_outbox"]


def build_forge_client(proj: dict, cfg: dict):
    """The client for a project: VaultBoard, GitHubProject or GitLab, by `forge`."""
    if project_is_vault(proj):
        client = VaultBoard(
            proj["vault_path"],
            proj.get("tasks_glob", "inbox/tasks/*.md"),
            commit_results=proj.get("commit_results", True),
        )
        proj["_vault"] = client  # seam for the forge-aware status/result writers
        return client
    if project_is_github(proj):
        client = GitHubProject(proj["github_project_id"], proj["path"])
        proj["_github"] = client  # seam for the forge-aware Status/label writers
        return client
    # Project access tokens are scoped to one project; a project block may carry
    # its own keychain entry, falling back to the top-level one.
    kc = proj.get("keychain") or cfg["keychain"]
    token = keychain_token(kc["service"], kc["account"])
    return GitLab(proj["host"], token)


def prepare_project_context(cfg: dict, state: dict, proj: dict) -> dict:
    key = project_config_key(proj)
    ps = project_state(state, key)
    gl = build_forge_client(proj, cfg)
    triggers = list(proj.get("triggers", []))
    bootstrapping = not ps["bootstrapped"]

    # This can mutate local state and post terminal worker results, so keep it
    # serialized before the parallel remote-poll phase.
    collect_and_heal_runs(gl, proj, ps, state)
    return {
        "key": key,
        "proj": proj,
        "ps": ps,
        "gl": gl,
        "triggers": triggers,
        "bootstrapping": bootstrapping,
    }


def fetch_project_inputs(
    gl, proj: dict, ps_snapshot: dict, owner: str, triggers: list[str]
) -> dict:
    """Fetch remote project inputs against an isolated poll-state snapshot."""
    if project_is_vault(proj):
        return fetch_vault_inputs(gl, proj, ps_snapshot, triggers=triggers)
    if project_is_github(proj):
        return fetch_github_inputs(gl, proj, ps_snapshot, triggers=triggers)
    comments = poll_comments(gl, proj, ps_snapshot, owner)
    label_fires = []
    for label in TRIGGER_LABELS:
        if label in triggers:
            label_fires.extend(poll_label(gl, proj, ps_snapshot, label))
    new_awards = poll_awards(gl, proj, ps_snapshot) if "emoji" in triggers else set()
    return {
        "poll_state": ps_snapshot,
        "comments": comments,
        "label_fires": label_fires,
        "new_awards": new_awards,
    }


def commit_project_inputs(cfg: dict, state: dict, ctx: dict, fetched: dict) -> None:
    staged_ps = copy.deepcopy(ctx["ps"])
    apply_project_poll_state(staged_ps, fetched["poll_state"])
    # GitHub dispatch fires come from the durable outbox (applied above), not the
    # fetch result — so a crash between observe and dispatch neither loses nor
    # repeats work. Marking consumed happens in the SAME atomic save as the
    # conversation `assemble` creates below.
    vault_fires = (
        vault_dispatch_fires(staged_ps) if project_is_vault(ctx["proj"]) else []
    )
    github_fires = (
        github_dispatch_fires(staged_ps) if project_is_github(ctx["proj"]) else []
    )
    label_fires = vault_fires or github_fires or fetched["label_fires"]
    assemble(
        ctx["gl"],
        ctx["proj"],
        staged_ps,
        fetched["comments"],
        label_fires,
        fetched["new_awards"],
        cfg["owner"],
        cfg["defaults"],
        ctx["triggers"],
        cfg.get("jira"),
    )
    if github_fires:
        github_mark_dispatched(staged_ps, github_fires)
    if vault_fires:
        vault_mark_dispatched(staged_ps, vault_fires)
    if ctx["bootstrapping"]:
        staged_ps["bootstrapped"] = True

    staged_state = dict(state)
    staged_state["projects"] = dict(state["projects"])
    staged_state["projects"][ctx["key"]] = staged_ps
    save_state(staged_state)

    state["projects"][ctx["key"]] = staged_ps
    ctx["ps"] = staged_ps
    if ctx["bootstrapping"]:
        log.info("bootstrap complete for %s", ctx["key"])


def configured_project_poll_workers(cfg: dict) -> int | None:
    configured = cfg.get("project_poll_workers")
    if configured is None:
        return None
    if (
        isinstance(configured, bool)
        or not isinstance(configured, int)
        or configured < 1
    ):
        raise ConfigurationError("project_poll_workers must be a positive integer")
    return configured


def project_poll_worker_count(cfg: dict, project_count: int) -> int:
    configured = configured_project_poll_workers(cfg)
    return min(project_count, configured) if configured is not None else project_count


def reconcile_projects(cfg: dict, state: dict) -> list[dict]:
    validate_unique_project_keys(cfg["projects"])
    validate_github_projects(cfg["projects"])
    validate_vault_projects(cfg["projects"], check_fs=True)
    configured_project_poll_workers(cfg)
    contexts = []
    for proj in cfg["projects"]:
        try:
            contexts.append(prepare_project_context(cfg, state, proj))
        except Exception:
            log.exception("cycle failed for project %s", proj.get("path"))
    if not contexts:
        return []

    committed_by_index = {}
    workers = project_poll_worker_count(cfg, len(contexts))
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="project-poll"
    ) as executor:
        future_by_index = {}
        for index, ctx in enumerate(contexts):
            snapshot = project_poll_state_snapshot(ctx["ps"])
            future = executor.submit(
                fetch_project_inputs,
                ctx["gl"],
                ctx["proj"],
                snapshot,
                cfg["owner"],
                ctx["triggers"],
            )
            future_by_index[future] = index
        for future in concurrent.futures.as_completed(future_by_index):
            index = future_by_index[future]
            ctx = contexts[index]
            try:
                fetched = future.result()
            except Exception:
                log.exception("cycle failed for project %s", ctx["proj"].get("path"))
                continue
            try:
                commit_project_inputs(cfg, state, ctx, fetched)
            except Exception:
                log.exception("cycle failed for project %s", ctx["proj"].get("path"))
                continue
            committed_by_index[index] = ctx

    return [committed_by_index[index] for index in sorted(committed_by_index)]


def conversation_sort_key(key: str) -> tuple[int, int | str]:
    try:
        return (0, int(key))
    except ValueError:
        return (1, key)


def dispatch_pending(cfg: dict, state: dict, contexts: list[dict]) -> None:
    cap = int(cfg.get("concurrency_cap", 3))
    active_total, _ = active_counts(state)
    for ctx in contexts:
        proj, ps, gl = ctx["proj"], ctx["ps"], ctx["gl"]
        for conv_key in sorted(ps["conversations"], key=conversation_sort_key):
            conv = ps["conversations"][conv_key]
            if conv.get("current_run"):
                continue
            if not conv.get("pending") and conv.get("status") != "new":
                continue
            if active_total >= cap:
                log.info(
                    "global concurrency cap (%d) reached — deferring conversation %s",
                    cap,
                    conv_key,
                )
                return
            try:
                started = start_one(gl, proj, ps, conv_key, state)
            except Exception:
                log.exception(
                    "dispatch failed for project %s conversation %s",
                    proj.get("path"),
                    conv_key,
                )
                save_state(state)
                continue
            if not started:
                continue
            active_total += 1


def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    filehandler = RotatingFileHandler(
        LOG_DIR / "watcher.log", maxBytes=2 * 1024 * 1024, backupCount=3
    )
    filehandler.setFormatter(fmt)
    log.setLevel(logging.INFO)
    log.addHandler(stream)
    log.addHandler(filehandler)


VALID_TRIGGERS = (*TRIGGER_LABELS, "mention", "emoji")


def validate_jira_projects(cfg: dict) -> list[str]:
    errors = []
    raw_defaults = cfg.get("jira")
    if raw_defaults is not None and not isinstance(raw_defaults, dict):
        errors.append("top-level `jira` config must be a mapping")
    defaults = raw_defaults if isinstance(raw_defaults, dict) else None
    for project in cfg.get("projects", []):
        if not isinstance(project, dict):
            continue
        jira = merged_jira_config(project, defaults)
        if jira.get("enabled") and not jira.get("base_url"):
            errors.append(
                f"project {project.get('path', '?')!r}: "
                "Jira is enabled but `base_url` is not configured"
            )
        elif jira.get("enabled") and not valid_jira_base_url(jira.get("base_url")):
            errors.append(
                f"project {project.get('path', '?')!r}: "
                "Jira `base_url` must be a safe HTTP(S) URL"
            )
        custom_fields = jira.get("custom_fields")
        valid_custom_fields = valid_jira_custom_fields_shape(custom_fields)
        if custom_fields is not None and not valid_custom_fields:
            message = "Jira `custom_fields` must be a label-to-field-id mapping"
            if message not in errors:
                errors.append(message)
        development_field = jira.get("development_field")
        field_ids = list(custom_fields.values()) if valid_custom_fields else []
        if development_field is not None:
            field_ids.append(development_field)
        if any(not valid_jira_field_id(field_id) for field_id in field_ids):
            message = "Jira custom field IDs must match `customfield_<number>`"
            if message not in errors:
                errors.append(message)
    return errors


def validate_preflight_config(cfg) -> list[str]:
    """Validate parsed config data without performing I/O."""
    if (
        not isinstance(cfg, dict)
        or not isinstance(cfg.get("projects"), list)
        or not cfg["projects"]
    ):
        return ["config must define at least one project under `projects`"]

    projects = cfg["projects"]
    errors = validate_jira_projects(cfg)
    valid_projects = []
    for index, proj in enumerate(projects):
        if not isinstance(proj, dict):
            errors.append(f"config projects[{index}] must be a mapping")
            continue
        project_is_valid = True
        for field in ("host", "path"):
            if not isinstance(proj.get(field), str) or not proj[field]:
                errors.append(
                    f"config projects[{index}] must define non-empty `{field}`"
                )
                project_is_valid = False
        triggers = proj.get("triggers", [])
        if not isinstance(triggers, list):
            errors.append(
                f"project {proj.get('path', '?')!r}: `triggers` must be a list"
            )
            project_is_valid = False
        else:
            unknown = [trigger for trigger in triggers if trigger not in VALID_TRIGGERS]
            if unknown:
                errors.append(
                    f"project {proj.get('path', '?')!r}: unknown triggers {unknown}; "
                    f"valid triggers are {list(VALID_TRIGGERS)}"
                )
        if project_is_valid:
            valid_projects.append(proj)
    for validator in (
        validate_unique_project_keys,
        validate_github_projects,
        validate_vault_projects,
    ):
        try:
            validator(valid_projects)
        except ConfigurationError as e:
            errors.append(str(e))
    return errors


def validate_preflight_state(
    state, *, state_path: Path, state_bak_path: Path
) -> list[str]:
    """Validate parsed state data without performing I/O."""
    if not isinstance(state, dict):
        return [f"state {state_path} must contain a JSON object"]
    if not isinstance(state.get("projects"), dict):
        return [f"state {state_path} must contain a `projects` object"]
    try:
        validate_project_poll_cursors(
            state,
            state_path=state_path,
            state_bak_path=state_bak_path,
        )
    except StatePersistenceError as e:
        return [str(e)]
    return []


def validate_project_key_alignment(cfg, state) -> list[str]:
    """Compare configured project identities with persisted state buckets."""
    if (
        not isinstance(cfg, dict)
        or not isinstance(cfg.get("projects"), list)
        or not cfg["projects"]
    ):
        return []
    if not isinstance(state, dict) or not isinstance(state.get("projects"), dict):
        return []
    try:
        config_keys = {project_config_key(proj) for proj in cfg["projects"]}
    except (KeyError, TypeError):
        return []
    state_keys = set(state["projects"])
    if config_keys == state_keys:
        return []

    differences = []
    missing = sorted(config_keys - state_keys)
    stale = sorted(state_keys - config_keys)
    if missing:
        differences.append(f"missing from state: {', '.join(missing)}")
    if stale:
        differences.append(f"not in config: {', '.join(stale)}")
    return ["config/state project keys differ; " + "; ".join(differences)]


def validate_launchd_plist(
    raw: bytes,
    plist,
    *,
    repository_root: Path,
    state_dir: Path,
    log_dir: Path,
) -> list[str]:
    """Validate parsed launchd data against explicit expected paths."""
    errors = []
    placeholders = sorted(
        set(re.findall(r"__[A-Z][A-Z0-9_]*__", raw.decode(errors="ignore")))
    )
    if placeholders:
        errors.append(
            f"launchd plist contains template placeholder(s) {placeholders}; rerun ./install.sh"
        )
    if not isinstance(plist, dict):
        return errors + ["launchd plist root must be a dictionary; rerun ./install.sh"]

    expected_executable = repository_root / "eastwatch"
    arguments = plist.get("ProgramArguments")
    if not isinstance(arguments, list) or str(expected_executable) not in arguments:
        errors.append(
            f"launchd ProgramArguments must point to {expected_executable}; rerun ./install.sh from that repository"
        )
    if "WorkingDirectory" in plist and plist["WorkingDirectory"] != str(
        repository_root
    ):
        errors.append(
            f"launchd WorkingDirectory must be {repository_root}; rerun ./install.sh"
        )

    environment = plist.get("EnvironmentVariables")
    environment = environment if isinstance(environment, dict) else {}
    home = environment.get("HOME")
    effective_state = environment.get("EASTWATCH_STATE_DIR") or environment.get(
        "BOARD_WATCHER_STATE_DIR"
    )
    if effective_state is None and isinstance(home, str):
        effective_state = str(Path(home) / ".local/state/eastwatch")
    if effective_state != str(state_dir):
        errors.append(f"launchd state path must be {state_dir}; rerun ./install.sh")
    effective_log = environment.get("EASTWATCH_LOG_DIR") or environment.get(
        "BOARD_WATCHER_LOG_DIR"
    )
    if effective_log is None and effective_state is not None:
        effective_log = str(Path(effective_state) / "logs")
    if effective_log != str(log_dir):
        errors.append(f"launchd log path must be {log_dir}; rerun ./install.sh")

    expected_logs = {
        "StandardOutPath": log_dir / "launchd.out.log",
        "StandardErrorPath": log_dir / "launchd.err.log",
    }
    for key, expected in expected_logs.items():
        if plist.get(key) != str(expected):
            errors.append(f"launchd {key} must be {expected}; rerun ./install.sh")
    return errors


def preflight_errors(
    *,
    config_path: Path,
    state_path: Path,
    plist_path: Path,
    repository_root: Path,
    state_dir: Path,
    log_dir: Path,
) -> list[str]:
    """Read local files and return migration/config errors without changing state."""
    errors = []
    cfg = None
    state = None
    if config_path.exists():
        try:
            cfg = yaml.safe_load(config_path.read_text())
        except (OSError, yaml.YAMLError) as e:
            errors.append(f"could not parse config {config_path}: {e}")
        else:
            errors.extend(validate_preflight_config(cfg))
    else:
        errors.append(
            f"config does not exist at {config_path}; copy config.yaml.example there and review it"
        )

    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            errors.append(f"could not parse state {state_path}: {e}")
        else:
            errors.extend(
                validate_preflight_state(
                    state,
                    state_path=state_path,
                    state_bak_path=state_path.with_name(f"{state_path.name}.bak"),
                )
            )
    else:
        errors.append(
            f"state does not exist at {state_path}; run the watcher once or restore its state"
        )
    errors.extend(validate_project_key_alignment(cfg, state))

    if plist_path.exists():
        try:
            raw_plist = plist_path.read_bytes()
            plist = plistlib.loads(raw_plist)
        except (OSError, plistlib.InvalidFileException, ValueError) as e:
            errors.append(
                f"could not parse launchd plist {plist_path}: {e}; rerun ./install.sh"
            )
        else:
            errors.extend(
                validate_launchd_plist(
                    raw_plist,
                    plist,
                    repository_root=repository_root,
                    state_dir=state_dir,
                    log_dir=log_dir,
                )
            )
    else:
        errors.append(
            f"launchd plist does not exist at {plist_path}; rerun ./install.sh"
        )
    return errors


def preflight_main() -> int:
    plist_path = Path.home() / "Library/LaunchAgents/com.stanwang.eastwatch.plist"
    errors = preflight_errors(
        config_path=CONFIG_PATH,
        state_path=STATE_PATH,
        plist_path=plist_path,
        repository_root=REPOSITORY_ROOT,
        state_dir=STATE_DIR,
        log_dir=LOG_DIR,
    )
    if errors:
        for error in errors:
            print(f"preflight: ERROR: {error}", file=sys.stderr)
        return 1
    print("preflight: OK: config, state, project keys, and launchd paths are valid")
    return 0


def warn_unknown_triggers(cfg: dict) -> None:
    """A configured trigger the code doesn't know is a silently dead fleet — flag it."""
    known = set(VALID_TRIGGERS)
    for proj in cfg.get("projects", []):
        unknown = [t for t in proj.get("triggers", []) if t not in known]
        if unknown:
            log.warning(
                "project %s: configured trigger(s) %s are not recognized and will NEVER fire — "
                "label triggers must be one of %s (plus 'mention'/'emoji')",
                proj.get("path", "?"),
                unknown,
                list(TRIGGER_LABELS),
            )


def main() -> int:
    for d in (STATE_DIR, LOG_DIR, CONVOS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    setup_logging()
    lock = open(CYCLE_LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("another cycle is already running — exiting")
        return 0
    if not CONFIG_PATH.exists():
        log.error("no config at %s — copy config.yaml.example there", CONFIG_PATH)
        return 1
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    warn_unknown_triggers(cfg)
    try:
        state = load_state()
    except StatePersistenceError as e:
        log.error("invalid state: %s", e)
        return 1
    if maybe_sweep_artifacts(state):
        save_state(state)
    log.info("cycle start (%d project(s))", len(cfg["projects"]))
    try:
        contexts = reconcile_projects(cfg, state)
    except ConfigurationError as e:
        log.error("invalid config: %s", e)
        return 1
    dispatch_pending(cfg, state, contexts)
    save_state(state)
    log.info("cycle complete")
    return 0


def sweep_main() -> int:
    for directory in (STATE_DIR, LOG_DIR, CONVOS_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    setup_logging()
    lock = open(CYCLE_LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.error("another cycle is already running — sweep skipped")
        return 1
    try:
        state = load_state()
    except StatePersistenceError as e:
        log.error("invalid state: %s", e)
        return 1
    counts = sweep_artifacts(state)
    state.setdefault("maintenance", {})["last_sweep_at"] = time.time()
    save_state(state)
    print(
        "sweep complete: "
        f"{counts['tails']} tail(s), "
        f"{counts['raw_captures']} raw capture(s), "
        f"{counts['orphans']} orphan run(s)"
    )
    return 0


def cli(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--preflight"]:
        return preflight_main()
    if len(args) == 2 and args[0] == "--worker":
        return worker_main(args[1])
    if args == ["sweep"]:
        return sweep_main()
    return main()


if __name__ == "__main__":
    raise SystemExit(cli())
