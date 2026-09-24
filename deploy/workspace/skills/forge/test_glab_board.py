from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parent / "scripts" / "glab-board"


class GlabBoardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        self.repo = self.root / "repo"
        self.log = self.root / "calls.jsonl"
        self.bin.mkdir()
        self.repo.mkdir()
        agent_link = self.home / "dotfiles" / "scripts" / "bin" / "agent-link"
        agent_link.parent.mkdir(parents=True)
        agent_link.write_text("#!/bin/sh\nprintf '%s\\n' \"agent-link $*\" >> \"$FAKE_TEXT_LOG\"\n")
        agent_link.chmod(0o755)
        self._write_fake_git()
        self._write_fake_glab()
        self._write_fake_gh()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_executable(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(textwrap.dedent(body))
        path.chmod(0o755)

    def _write_fake_git(self) -> None:
        self._write_executable(
            "git",
            r'''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(["git", *args]) + "\n")
if args[:2] == ["remote", "get-url"]:
    print(os.environ.get("FAKE_REMOTE", "git@gitlab.example.com:group/project.git"))
elif args[:2] == ["branch", "--show-current"]:
    print(os.environ.get("FAKE_BRANCH", "issue-103-player-card"))
elif args and args[0] == "symbolic-ref":
    print("refs/remotes/origin/main")
elif args[:2] == ["status", "--porcelain"]:
    print(os.environ.get("FAKE_STATUS", ""), end="")
elif args[:2] == ["rev-list", "--count"]:
    print(os.environ.get("FAKE_AHEAD", "1"))
elif args[:3] == ["worktree", "list", "--porcelain"]:
    existing = os.environ.get("FAKE_EXISTING_WORKTREE")
    if existing:
        print(f"worktree {existing}\nHEAD deadbeef\nbranch refs/heads/{os.environ['FAKE_BRANCH']}")
elif args[:2] == ["worktree", "add"]:
    # Last two arguments are destination and start point.
    destination = pathlib.Path(args[-2])
    destination.mkdir(parents=True, exist_ok=True)
elif args[0] == "config" and "forge.githubProjectId" in args:
    key_index = args.index("forge.githubProjectId")
    if key_index < len(args) - 1:
        pass  # a value follows the key -> write form: log and succeed
    else:
        # read form (git config [--get-all] forge.githubProjectId); comma splits multiple values
        value = os.environ.get("FAKE_GITHUB_PROJECT_ID", "")
        if not value:
            raise SystemExit(1)  # unset key exits non-zero, like real git
        for one in value.split(","):
            print(one)
elif args and args[0] == "show-ref":
    raise SystemExit(1)
''',
        )

    def _write_fake_glab(self) -> None:
        self._write_executable(
            "glab",
            r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(["glab", *args]) + "\n")
if args[:2] == ["api", "user"]:
    print(json.dumps({"id": 7}))
elif args[:2] == ["api", "projects/group%2Fproject/boards"]:
    print(json.dumps([{"id": 12}]))
elif args[:2] == ["api", "projects/group%2Fproject/labels?per_page=100"]:
    names = [
        "triage::pending",
        "agent::ready",
        "agent::ready-research",
        "agent::working",
        "agent::researching",
        "agent::parked",
        "agent::mr-ready",
        "agent::failed",
        "agent::for-human",
    ]
    print(json.dumps([{"id": 101 + i, "name": name} for i, name in enumerate(names)]))
elif args[:2] == ["api", "projects/group%2Fproject/boards/12/lists"]:
    print(json.dumps([{"label": {"id": 104, "name": "agent::working"}}]))
elif args and args[0] == "api" and "/issues/" in args[1] and "-X" not in args:
    iid = int(args[1].rsplit("/", 1)[-1])
    response = json.dumps({"iid": iid, "title": "Add async\nplayer card", "state": os.environ.get("FAKE_GITLAB_ISSUE_STATE", "opened"), "web_url": f"https://gitlab.example.com/group/project/-/issues/{iid}"})
    if os.environ.get("FAKE_GITLAB_RAW_NEWLINE") == "1":
        response = response.replace("\\n", "\n")
    print(response)
elif args[:4] == ["api", "-X", "POST", "projects/group%2Fproject/issues"]:
    response = json.dumps({
        "iid": 41,
        "title": "first line\nsecond line",
        "web_url": "https://gitlab.example.com/group/project/-/issues/41",
    })
    if os.environ.get("FAKE_GITLAB_RAW_NEWLINE") == "1":
        response = response.replace("\\n", "\n")
    print(response)
elif args and args[0] == "api" and "merge_requests?" in args[1]:
    print("[]")
elif args and args[0] == "api" and "/merge_requests/74" in args[1]:
    response = json.dumps({
        "source_branch": os.environ.get("FAKE_BRANCH", "issue-103-player-card"),
        "target_branch": "main",
        "draft": os.environ.get("FAKE_DRAFT") == "1",
        "changes_count": "4",
        "description": "first line\nsecond line",
        "web_url": "https://gitlab.example.com/group/project/-/merge_requests/74",
    }, indent=2)
    if os.environ.get("FAKE_GITLAB_RAW_NEWLINE") == "1":
        response = response.replace("\\n", "\n")
    print(response)
elif args[:2] == ["mr", "create"]:
    print("https://gitlab.example.com/group/project/-/merge_requests/74")
''',
        )

    def _write_fake_gh(self) -> None:
        self._write_executable(
            "gh",
            r'''#!/usr/bin/env python3
import json, os, re, sys
args = sys.argv[1:]
payload = {}
if "--input" in args:
    payload = json.load(sys.stdin)
query = payload.get("query", "")
if not query:
    query = next((arg.removeprefix("query=") for arg in args if arg.startswith("query=")), "")
match = re.search(r"\b(?:query|mutation)\s+(\w+)", query)
operation = match.group(1) if match else ""
# Variables passed on argv via -f/-F key=value (item mutations use these, not --input).
fargs = {}
for i, a in enumerate(args):
    if a in ("-f", "-F") and i + 1 < len(args) and "=" in args[i + 1]:
        k, v = args[i + 1].split("=", 1)
        fargs[k] = v
call = ["gh", *args]
if operation:
    call.append(f"operation={operation}")
mutation = re.search(r"\b(createProjectV2|linkProjectV2ToRepository|updateProjectV2Field)\b", query)
if mutation:
    call.append(f"mutation={mutation.group(1)}")
if payload.get("variables"):
    call.append("variables=" + json.dumps(payload["variables"], sort_keys=True))
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(call) + "\n")

if args[:2] == ["issue", "view"] and "--json" in args:
    print(json.dumps({"number": int(args[2]), "title": "Add async player card", "url": f"https://github.com/group/project/issues/{args[2]}"}))
    raise SystemExit(0)
if args[:2] == ["label", "edit"]:
    raise SystemExit(1)
if args[:4] == ["api", "-X", "POST", "repos/group/project/issues"]:
    print(json.dumps({"number": 52, "html_url": "https://github.com/group/project/issues/52"}))
    raise SystemExit(0)
if args[:2] != ["api", "graphql"]:
    raise SystemExit(0)

OPTION_IDS = {
    "Triage": "OPT_triage", "Needs-info": "OPT_needsinfo", "Ready": "OPT_ready",
    "Ready-research": "OPT_readyresearch", "Working": "OPT_working",
    "Researching": "OPT_researching", "Parked": "OPT_parked", "Review": "OPT_review",
    "Failed": "OPT_failed", "For-human": "OPT_forhuman", "Closed": "OPT_closed",
}
OPTION_NAMES = {v: k for k, v in OPTION_IDS.items()}
COLORS = {
    "Triage": "YELLOW", "Needs-info": "YELLOW", "Ready": "GREEN", "Ready-research": "GREEN",
    "Working": "BLUE", "Researching": "BLUE", "Parked": "ORANGE", "Review": "PURPLE",
    "Failed": "RED", "For-human": "GRAY", "Closed": "PINK",
    "Todo": "GRAY", "In Progress": "BLUE", "Done": "GREEN",
}


def options(names):
    return [{"id": f"OPT_{n}", "name": n, "color": COLORS.get(n, "GRAY"), "description": ""} for n in names]


project_state = os.environ.get("FAKE_GITHUB_PROJECT", "fresh")
canonical = os.environ.get("FAKE_GITHUB_PROJECT_ID", "PVT_canonical")
state_path = os.environ.get("FAKE_GH_STATE", "")

if operation == "FindProject":
    after = fargs.get("after")
    if project_state == "paged":
        if not after:
            page = {"nodes": [{"id": "PVT_unrelated", "title": "some other board"}], "pageInfo": {"hasNextPage": True, "endCursor": "CURSOR1"}}
        else:
            page = {"nodes": [{"id": "PVT_paged", "title": "project board"}], "pageInfo": {"hasNextPage": False, "endCursor": "CURSOR2"}}
    elif project_state == "dup":
        nodes = [{"id": "PVT_one", "title": "project board"}, {"id": "PVT_two", "title": "project board"}]
        page = {"nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}
    else:
        nodes = [{"id": "PVT_existing", "title": "project board"}] if project_state in {"liveboard", "complete", "reuse"} else []
        page = {"nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}
    print(json.dumps({"data": {"viewer": {"id": "U_viewer"}, "repository": {"id": "R_project", "projectsV2": page}}}))
elif operation == "CreateProject":
    print(json.dumps({"data": {"createProjectV2": {"projectV2": {"id": "PVT_created"}}}}))
elif operation == "LinkProject":
    print(json.dumps({"data": {"linkProjectV2ToRepository": {"repository": {"id": "R_project"}}}}))
elif operation == "ProjectStatus":
    new_desired = ["Triage", "Needs-info", "Ready", "Ready-research", "Working", "Researching", "Parked", "Review", "Failed", "For-human"]
    old_lanes = ["Triage", "Ready", "Ready-research", "Working", "Researching", "Parked", "Review", "Failed", "For-human"]
    if project_state == "complete":
        names = new_desired
    elif project_state == "liveboard":
        names = old_lanes + ["Closed"]
    else:
        names = ["Todo", "In Progress", "Done"]
    print(json.dumps({"data": {"node": {"fields": {"nodes": [{"id": "PVTSSF_status", "name": "Status", "options": options(names)}]}}}}))
elif operation == "UpdateStatus":
    print(json.dumps({"data": {"updateProjectV2Field": {"projectV2Field": {"id": "PVTSSF_status"}}}}))
elif operation == "IssueItem":
    after = fargs.get("after")
    issue_state = os.environ.get("FAKE_ISSUE_STATE", "OPEN")
    exists = os.environ.get("FAKE_ITEM_EXISTS", "0")
    if exists == "page2":
        # Canonical item only on the second page of associations.
        if not after:
            page = {"nodes": [{"id": "PVTI_other", "project": {"id": "PVT_someoneelse"}}], "pageInfo": {"hasNextPage": True, "endCursor": "ICURSOR1"}}
        else:
            page = {"nodes": [{"id": "PVTI_item", "project": {"id": canonical}}], "pageInfo": {"hasNextPage": False, "endCursor": "ICURSOR2"}}
    elif exists == "1":
        page = {"nodes": [{"id": "PVTI_item", "project": {"id": canonical}}], "pageInfo": {"hasNextPage": False, "endCursor": None}}
    else:
        page = {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}
    print(json.dumps({"data": {"repository": {"issue": {"id": "I_issue", "state": issue_state, "projectItems": page}}}}))
elif operation == "ProjectFields":
    print(json.dumps({"data": {"node": {"fields": {"nodes": [{"id": "PVTSSF_status", "name": "Status", "options": [{"id": v, "name": k} for k, v in OPTION_IDS.items()]}]}}}}))
elif operation == "AddItem":
    print(json.dumps({"data": {"addProjectV2ItemById": {"item": {"id": "PVTI_added"}}}}))
elif operation == "SetStatus":
    name = OPTION_NAMES.get(fargs.get("optionId", ""), "")
    if state_path:
        json.dump({"set": name}, open(state_path, "w"))
    print(json.dumps({"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": fargs.get("itemId", "")}}}}))
elif operation == "ItemStatus":
    st = {}
    if state_path and os.path.exists(state_path):
        st = json.load(open(state_path))
    if "set" in st:
        # Read-back returns what we set, unless a mismatch is being simulated.
        name = "Failed" if os.environ.get("FAKE_READBACK_WRONG") == "1" else st["set"]
        print(json.dumps({"data": {"node": {"fieldValueByName": {"name": name}}}}))
    else:
        polls = st.get("polls", 0) + 1
        st["polls"] = polls
        if state_path:
            json.dump(st, open(state_path, "w"))
        nulls = int(os.environ.get("FAKE_FENCE_NULLS", "1"))
        if polls <= nulls:
            print(json.dumps({"data": {"node": {"fieldValueByName": None}}}))
        else:
            print(json.dumps({"data": {"node": {"fieldValueByName": {"name": "Triage"}}}}))
else:
    raise SystemExit(f"unexpected GraphQL operation: {operation}")
''',
        )

    def run_script(self, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.bin}:/usr/bin:/bin",
                "WORKTREE_ROOT": str(self.root / "worktrees"),
                "FAKE_LOG": str(self.log),
                "FAKE_TEXT_LOG": str(self.root / "calls.txt"),
                "FAKE_GH_STATE": str(self.root / "gh_state.json"),
                "FAKE_BRANCH": "issue-103-player-card",
                "FAKE_REMOTE": "git@gitlab.example.com:group/project.git",
            }
        )
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [str(SCRIPT), *args],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def gh_ops(self) -> list[str]:
        return [
            marker.removeprefix("operation=")
            for call in self.calls()
            for marker in call
            if marker.startswith("operation=")
        ]

    def github_env(self, **extra: str) -> dict[str, str]:
        env = {
            "FAKE_REMOTE": "git@github.com:group/project.git",
            "FAKE_GITHUB_PROJECT_ID": "PVT_canonical",
            "GLAB_BOARD_FENCE_INTERVAL": "0",
        }
        env.update(extra)
        return env

    def test_create_gitlab_sends_fields_and_accepts_raw_newline_json(self) -> None:
        body = self.root / "issue.md"
        body.write_text("Issue body\nsecond line\n")
        result = self.run_script(
            "create",
            "--title",
            "Fix parser",
            "--description-file",
            str(body),
            "--label",
            "bug",
            "--label",
            "backend",
            extra_env={"FAKE_GITLAB_RAW_NEWLINE": "1"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "created #41: https://gitlab.example.com/group/project/-/issues/41\n")
        self.assertIn(
            [
                "glab", "api", "-X", "POST", "projects/group%2Fproject/issues",
                "-f", "title=Fix parser",
                "-f", "description=Issue body\nsecond line",
                "-f", "labels=bug,backend",
            ],
            self.calls(),
        )
        self.assertFalse((self.root / "calls.txt").exists())

    def test_create_github_sends_body_and_repeated_labels(self) -> None:
        body = self.root / "issue.md"
        body.write_text("GitHub body\n")
        result = self.run_script(
            "create",
            "--title",
            "Fix parser",
            "--description-file",
            str(body),
            "--label",
            "bug",
            "--label",
            "backend",
            extra_env=self.github_env(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "created #52: https://github.com/group/project/issues/52\n")
        self.assertIn(
            [
                "gh", "api", "-X", "POST", "repos/group/project/issues",
                "-f", "title=Fix parser",
                "-f", "body=GitHub body",
                "-f", "labels[]=bug",
                "-f", "labels[]=backend",
            ],
            self.calls(),
        )
        self.assertFalse((self.root / "calls.txt").exists())

    def test_create_validates_all_arguments_before_forge_calls(self) -> None:
        missing = self.root / "missing.md"
        cases = [
            ((), "create requires --title"),
            (("--title",), "--title requires a value"),
            (("--title", "   "), "create title must not be blank"),
            (("--title", "Valid", "--description-file"), "--description-file requires a path"),
            (("--title", "Valid", "--description-file", str(missing)), "description file not found"),
            (("--title", "Valid", "--label"), "--label requires a value"),
            (("--title", "Valid", "--label", "  "), "create label must not be blank"),
            (("--title", "Valid", "--label", "bug,backend"), "create label must not contain a comma"),
            (("--title", "Valid", "--bogus"), "unknown create option"),
        ]
        for args, message in cases:
            with self.subTest(args=args):
                result = self.run_script("create", *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
        self.assertFalse(any(call[0] in {"glab", "gh"} for call in self.calls()))

    def test_show_alias_views_gitlab_issue(self) -> None:
        result = self.run_script("show", "7")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(["glab", "issue", "view", "7", "-R", "group/project", "--comments"], self.calls())

    def test_show_alias_views_github_issue(self) -> None:
        result = self.run_script("show", "7", extra_env=self.github_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(["gh", "issue", "view", "7", "-R", "group/project", "--comments"], self.calls())

    def test_view_and_show_require_an_iid_before_forge_calls(self) -> None:
        for command in ("view", "show"):
            with self.subTest(command=command):
                result = self.run_script(command)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("usage: glab-board view|show <iid>", result.stderr)
        self.assertFalse(any(call[0] in {"glab", "gh"} for call in self.calls()))

    def test_setup_migrates_creates_and_adds_gitlab_board_lists(self) -> None:
        result = self.run_script("setup")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        legacy_triage = "-".join(("needs", "triage"))
        self.assertIn(
            [
                "glab",
                "api",
                "-X",
                "PUT",
                "projects/group%2Fproject/labels",
                "-f",
                f"name={legacy_triage}",
                "-f",
                "new_name=triage::pending",
            ],
            calls,
        )
        create = next(
            call
            for call in calls
            if call[:5]
            == ["glab", "api", "-X", "POST", "projects/group%2Fproject/labels"]
            and "name=agent::mr-ready" in call
        )
        self.assertIn("color=#6f42c1", create)
        list_calls = [
            call
            for call in calls
            if call[:5]
            == [
                "glab",
                "api",
                "-X",
                "POST",
                "projects/group%2Fproject/boards/12/lists",
            ]
        ]
        self.assertEqual(
            [call[-1] for call in list_calls],
            [
                "label_id=101",
                "label_id=102",
                "label_id=103",
                "label_id=105",
                "label_id=106",
                "label_id=107",
                "label_id=108",
                "label_id=109",
            ],
        )
        self.assertIn("skipped: list agent::working (already exists)", result.stdout)

    def test_setup_github_creates_links_pins_id_and_appends_status(self) -> None:
        result = self.run_script("setup", "--board", extra_env=self.github_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        mutations = [
            marker.removeprefix("mutation=")
            for call in self.calls()
            for marker in call
            if marker.startswith("mutation=")
        ]
        self.assertEqual(
            mutations,
            ["createProjectV2", "linkProjectV2ToRepository", "updateProjectV2Field"],
        )
        # The freshly created project's node id is pinned to git config.
        self.assertIn(["git", "config", "--replace-all", "forge.githubProjectId", "PVT_created"], self.calls())
        update = next(call for call in self.calls() if "operation=UpdateStatus" in call)
        variables = json.loads(next(value.removeprefix("variables=") for value in update if value.startswith("variables=")))
        names = [option["name"] for option in variables["options"]]
        # Never drop an existing option: GitHub's placeholder defaults are preserved (re-sent with id).
        self.assertEqual(names[:3], ["Todo", "In Progress", "Done"])
        for option in variables["options"][:3]:
            self.assertTrue(option["id"])
        # All ten desired options are appended (without ids -> newly created), Needs-info included.
        self.assertEqual(
            names[3:],
            ["Triage", "Needs-info", "Ready", "Ready-research", "Working", "Researching", "Parked", "Review", "Failed", "For-human"],
        )
        for option in variables["options"][3:]:
            self.assertNotIn("id", option)
        self.assertIn("created: project project board", result.stdout)
        self.assertIn("linked: project project board to group/project", result.stdout)
        self.assertIn("config: forge.githubProjectId=PVT_created", result.stdout)

    def test_setup_github_default_is_queue_only(self) -> None:
        result = self.run_script(
            "setup",
            extra_env={"FAKE_REMOTE": "git@github.com:group/project.git"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("labels bootstrapped (queue-only)", result.stdout)
        self.assertFalse(any("mutation=" in marker for call in self.calls() for marker in call))

    def test_setup_github_reuses_existing_project(self) -> None:
        result = self.run_script("setup", "--board", extra_env=self.github_env(FAKE_GITHUB_PROJECT="complete"))
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertIn("skipped: project project board (already linked)", result.stdout)
        self.assertIn("skipped: status lanes (already present)", result.stdout)
        self.assertIn(["git", "config", "--replace-all", "forge.githubProjectId", "PVT_existing"], calls)
        self.assertFalse(any("operation=CreateProject" in call for call in calls))
        self.assertFalse(any("operation=LinkProject" in call for call in calls))

    def test_setup_github_appends_needs_info_preserving_existing(self) -> None:
        # Live board has the nine agent lanes plus a hand-added Closed; setup must append only
        # the missing Needs-info and re-send every existing option (Closed included) with its id.
        result = self.run_script("setup", "--board", extra_env=self.github_env(FAKE_GITHUB_PROJECT="liveboard"))
        self.assertEqual(result.returncode, 0, result.stderr)
        update = next(call for call in self.calls() if "operation=UpdateStatus" in call)
        variables = json.loads(next(v.removeprefix("variables=") for v in update if v.startswith("variables=")))
        options = variables["options"]
        existing = ["Triage", "Ready", "Ready-research", "Working", "Researching", "Parked", "Review", "Failed", "For-human", "Closed"]
        # Existing options re-sent unchanged, each carrying its id so card assignments survive.
        self.assertEqual([o["name"] for o in options[:-1]], existing)
        for option in options[:-1]:
            self.assertTrue(option["id"])
        self.assertIn("Closed", [o["name"] for o in options])
        # Needs-info is the only appended option, and it is new (no id).
        self.assertEqual(options[-1]["name"], "Needs-info")
        self.assertNotIn("id", options[-1])
        self.assertIn("appended: status lanes (Needs-info)", result.stdout)

    def test_setup_github_paginates_to_find_canonical_project(self) -> None:
        result = self.run_script("setup", "--board", extra_env=self.github_env(FAKE_GITHUB_PROJECT="paged"))
        self.assertEqual(result.returncode, 0, result.stderr)
        finds = [c for c in self.calls() if "operation=FindProject" in c]
        # Two pages fetched; the second carries the cursor from the first page.
        self.assertEqual(len(finds), 2)
        self.assertTrue(any("after=CURSOR1" in c for c in finds))
        # Project found on page 2 -> reused (no create) and its id pinned.
        self.assertFalse(any("operation=CreateProject" in c for c in self.calls()))
        self.assertIn(["git", "config", "--replace-all", "forge.githubProjectId", "PVT_paged"], self.calls())

    def test_github_verb_fails_closed_without_pinned_project_id(self) -> None:
        result = self.run_script(
            "grab",
            "7",
            extra_env={"FAKE_REMOTE": "git@github.com:group/project.git"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("run 'glab-board setup --board' first", result.stderr)
        # Fails before ANY side effect: no Status write and no assignment.
        self.assertNotIn("SetStatus", self.gh_ops())
        self.assertFalse(any("--add-assignee" in c for c in self.calls() if c[0] == "gh"))

    def test_github_verb_fails_closed_on_ambiguous_project_id(self) -> None:
        result = self.run_script("grab", "7", extra_env=self.github_env(FAKE_GITHUB_PROJECT_ID="PVT_a,PVT_b"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ambiguous", result.stderr)
        self.assertNotIn("SetStatus", self.gh_ops())

    def test_setup_github_refuses_ambiguous_duplicate_projects(self) -> None:
        result = self.run_script("setup", "--board", extra_env=self.github_env(FAKE_GITHUB_PROJECT="dup"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("multiple projects titled", result.stderr)
        # Never pins an arbitrary id when ambiguous: no config write carrying a PVT_ value.
        self.assertFalse(any(
            c[:2] == ["git", "config"] and any(part.startswith("PVT_") for part in c)
            for c in self.calls()
        ))

    def test_grab_github_writes_status_and_no_labels(self) -> None:
        result = self.run_script("grab", "7", extra_env=self.github_env(FAKE_ITEM_EXISTS="1"))
        self.assertEqual(result.returncode, 0, result.stderr)
        gh_calls = [c for c in self.calls() if c[0] == "gh"]
        set_status = next(c for c in gh_calls if "operation=SetStatus" in c)
        self.assertIn("optionId=OPT_working", set_status)
        # The human claim is assigned, but NO lifecycle labels are written on GitHub.
        self.assertTrue(any(c[:3] == ["gh", "issue", "edit"] and "--add-assignee" in c for c in gh_calls))
        self.assertFalse(any("--add-label" in c or "--remove-label" in c for c in gh_calls))
        self.assertFalse(any(c[:3] == ["gh", "label", "create"] for c in gh_calls))
        # Item already present -> no add + no fence poll needed.
        self.assertNotIn("AddItem", self.gh_ops())

    def test_grab_github_research_sets_researching(self) -> None:
        result = self.run_script("grab", "7", "research", extra_env=self.github_env(FAKE_ITEM_EXISTS="1"))
        self.assertEqual(result.returncode, 0, result.stderr)
        set_status = next(c for c in self.calls() if "operation=SetStatus" in c)
        self.assertIn("optionId=OPT_researching", set_status)

    def test_github_item_added_fences_workflow_before_write(self) -> None:
        # Item absent from the canonical project: add it, wait for the Item-added->Triage workflow,
        # THEN write the target Status (else the workflow would clobber our write).
        result = self.run_script("grab", "7", extra_env=self.github_env(FAKE_ITEM_EXISTS="0", FAKE_FENCE_NULLS="1"))
        self.assertEqual(result.returncode, 0, result.stderr)
        ops = self.gh_ops()
        self.assertIn("AddItem", ops)
        # add -> fence poll (ItemStatus) -> write, in that order.
        self.assertLess(ops.index("AddItem"), ops.index("ItemStatus"))
        self.assertLess(ops.index("ItemStatus"), ops.index("SetStatus"))
        # The item added by AddItem is the one whose Status is written.
        set_status = next(c for c in self.calls() if "operation=SetStatus" in c)
        self.assertIn("itemId=PVTI_added", set_status)

    def test_github_item_added_fence_timeout_refuses_to_write(self) -> None:
        # Workflow never lands a Status: fail closed rather than race an unfenced write.
        result = self.run_script(
            "grab",
            "7",
            extra_env=self.github_env(FAKE_ITEM_EXISTS="0", FAKE_FENCE_NULLS="99", GLAB_BOARD_FENCE_ATTEMPTS="3"),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("workflow set no Status", result.stderr)
        self.assertNotIn("SetStatus", self.gh_ops())

    def test_github_item_lookup_paginates_associations(self) -> None:
        # Canonical item is on the second page of the issue's project associations: found, not re-added.
        result = self.run_script("grab", "7", extra_env=self.github_env(FAKE_ITEM_EXISTS="page2"))
        self.assertEqual(result.returncode, 0, result.stderr)
        issue_item_calls = [c for c in self.calls() if "operation=IssueItem" in c]
        self.assertEqual(len(issue_item_calls), 2)
        self.assertTrue(any("after=ICURSOR1" in c for c in issue_item_calls))
        self.assertNotIn("AddItem", self.gh_ops())
        set_status = next(c for c in self.calls() if "operation=SetStatus" in c)
        self.assertIn("itemId=PVTI_item", set_status)

    def test_github_status_read_back_mismatch_fails(self) -> None:
        result = self.run_script("grab", "7", extra_env=self.github_env(FAKE_ITEM_EXISTS="1", FAKE_READBACK_WRONG="1"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("read-back mismatch", result.stderr)

    def test_ready_github_sets_status_no_labels(self) -> None:
        result = self.run_script("ready", "7", extra_env=self.github_env(FAKE_ITEM_EXISTS="1"))
        self.assertEqual(result.returncode, 0, result.stderr)
        set_status = next(c for c in self.calls() if "operation=SetStatus" in c)
        self.assertIn("optionId=OPT_ready", set_status)
        self.assertFalse(any("--add-label" in c or "--remove-label" in c for c in self.calls() if c[0] == "gh"))
        self.assertIn("ready #7 (Ready)", result.stdout)

    def test_ready_research_github_sets_ready_research(self) -> None:
        result = self.run_script("ready", "7", "research", extra_env=self.github_env(FAKE_ITEM_EXISTS="1"))
        self.assertEqual(result.returncode, 0, result.stderr)
        set_status = next(c for c in self.calls() if "operation=SetStatus" in c)
        self.assertIn("optionId=OPT_readyresearch", set_status)

    def test_ready_refuses_closed_issue_github(self) -> None:
        result = self.run_script("ready", "7", extra_env=self.github_env(FAKE_ISSUE_STATE="CLOSED"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("closed", result.stderr)
        self.assertNotIn("SetStatus", self.gh_ops())

    def test_ready_gitlab_adds_ready_and_strips_triage(self) -> None:
        result = self.run_script("ready", "7")
        self.assertEqual(result.returncode, 0, result.stderr)
        put = next(
            call for call in self.calls()
            if call[:4] == ["glab", "api", "-X", "PUT"] and any(p == "add_labels=agent::ready" for p in call)
        )
        remove = next(p for p in put if p.startswith("remove_labels="))
        self.assertEqual(
            sorted(remove.removeprefix("remove_labels=").split(",")),
            sorted(["triage::pending", "triage::needs-info"]),
        )

    def test_ready_research_gitlab_adds_ready_research(self) -> None:
        result = self.run_script("ready", "7", "research")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(
            call[:4] == ["glab", "api", "-X", "PUT"] and "add_labels=agent::ready-research" in call
            for call in self.calls()
        ))

    def test_park_github_sets_parked_status_no_labels(self) -> None:
        result = self.run_script("park", "7", extra_env=self.github_env(FAKE_ITEM_EXISTS="1"))
        self.assertEqual(result.returncode, 0, result.stderr)
        set_status = next(c for c in self.calls() if "operation=SetStatus" in c)
        self.assertIn("optionId=OPT_parked", set_status)
        self.assertFalse(any("--add-label" in c or "--remove-label" in c for c in self.calls() if c[0] == "gh"))

    def test_close_github_closes_without_touching_labels_or_status(self) -> None:
        result = self.run_script("close", "7", extra_env=self.github_env(FAKE_ITEM_EXISTS="1"))
        self.assertEqual(result.returncode, 0, result.stderr)
        gh_calls = [c for c in self.calls() if c[0] == "gh"]
        self.assertTrue(any(c[:3] == ["gh", "issue", "close"] for c in gh_calls))
        self.assertFalse(any("--add-label" in c or "--remove-label" in c for c in gh_calls))
        # Closed is outside reconciliation: no Status write.
        self.assertNotIn("SetStatus", self.gh_ops())

    def test_close_clears_every_agent_label_gitlab(self) -> None:
        result = self.run_script("close", "7")
        self.assertEqual(result.returncode, 0, result.stderr)
        put = next(
            call for call in self.calls()
            if call[:2] == ["glab", "api"] and any("state_event=close" in part for part in call)
        )
        removal = next(part for part in put if part.startswith("remove_labels="))
        self.assertEqual(
            sorted(removal.removeprefix("remove_labels=").split(",")),
            sorted([
                "agent::ready",
                "agent::ready-research",
                "agent::working",
                "agent::researching",
                "agent::parked",
                "agent::mr-ready",
                "agent::failed",
                "agent::for-human",
            ]),
        )

    def test_mr_pins_current_source_and_default_target_then_verifies(self) -> None:
        result = self.run_script("mr", "--title", "Player card", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        create = next(call for call in self.calls() if call[:3] == ["glab", "mr", "create"])
        self.assertIn("--source-branch", create)
        self.assertEqual(create[create.index("--source-branch") + 1], "issue-103-player-card")
        self.assertIn("--target-branch", create)
        self.assertEqual(create[create.index("--target-branch") + 1], "main")
        self.assertIn("verified MR:", result.stdout)
        self.assertIn("agent-link add mr", (self.root / "calls.txt").read_text())

    def test_mr_accepts_gitlab_json_with_a_raw_newline_in_a_string(self) -> None:
        result = self.run_script(
            "mr",
            "--title",
            "Player card",
            "--yes",
            extra_env={"FAKE_GITLAB_RAW_NEWLINE": "1"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verified MR: https://gitlab.example.com/group/project/-/merge_requests/74", result.stdout)

    def test_mr_rejects_source_that_differs_from_current_branch(self) -> None:
        result = self.run_script("mr", "--source-branch", "wrong-branch", "--title", "Bad")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match current branch", result.stderr)
        self.assertFalse(any(call[:3] == ["glab", "mr", "create"] for call in self.calls()))

    def test_start_creates_worktree_claims_issue_and_returns_json(self) -> None:
        result = self.run_script("start", "103", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["issue"], 103)
        self.assertEqual(payload["branch"], "issue-103-add-async-player-card")
        self.assertTrue(Path(payload["worktree"]).is_dir())
        self.assertIn("grabbed #103", result.stderr)
        self.assertIn("agent-link issue", (self.root / "calls.txt").read_text())

    def test_start_accepts_gitlab_issue_json_with_a_raw_newline(self) -> None:
        result = self.run_script(
            "start",
            "103",
            "--json",
            extra_env={"FAKE_GITLAB_RAW_NEWLINE": "1"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["branch"], "issue-103-add-async-player-card")

    def test_start_warns_but_excludes_dirty_shared_checkout(self) -> None:
        result = self.run_script("start", "103", "--json", extra_env={"FAKE_STATUS": " M AGENTS.md\n"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("shared checkout is dirty", result.stderr)
        self.assertTrue(Path(json.loads(result.stdout)["worktree"]).is_dir())

    def test_finish_runs_repo_verifier_and_creates_ready_mr(self) -> None:
        verifier = self.repo / "scripts" / "verify-agent-change"
        verifier.parent.mkdir()
        verifier.write_text("#!/bin/sh\necho verified > \"$FAKE_VERIFY_LOG\"\n")
        verifier.chmod(0o755)
        result = self.run_script(
            "finish",
            "103",
            extra_env={"FAKE_VERIFY_LOG": str(self.root / "verify.log")},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "verify.log").read_text().strip(), "verified")
        create = next(call for call in self.calls() if call[:3] == ["glab", "mr", "create"])
        self.assertNotIn("--draft", create)
        self.assertIn("finished #103", result.stdout)


if __name__ == "__main__":
    unittest.main()
