import atexit
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.support import load_watcher

TMP_ROOT = Path(tempfile.mkdtemp(prefix="eastwatch-mr-test-"))
watcher = load_watcher(TMP_ROOT)

atexit.register(lambda: shutil.rmtree(TMP_ROOT, ignore_errors=True))


class FakeGitLab:
    def __init__(self, routes, post_errors=None):
        self.routes = routes
        self.post_errors = post_errors or {}
        self.gets = []
        self.posts = []
        self.puts = []

    def get(self, path, **params):
        self.gets.append((path, params))
        value = self.routes[path]
        return value(path, **params) if callable(value) else value

    def post(self, path, **data):
        self.posts.append((path, data))
        error = self.post_errors.get(path)
        if error:
            raise error
        return {"id": 1000 + len(self.posts)}

    def put(self, path, **data):
        self.puts.append((path, data))
        return {}


PROJ = {
    "id": 1,
    "host": "gitlab.example.com",
    "path": "group/project",
    "local_checkout": None,
    "bot_user_id": 999,
    "bot_username": "eastwatch_bot",
}
DEFAULTS = {
    "agent::ready": "pi:gpt:high",
    "agent::ready-research": "pi:gpt:medium",
    "qa": "pi:gpt:low",
}


class MRCommentFallbackTest(unittest.TestCase):
    def capture_reply_mr(self, description, issue_iid="43"):
        mr = {
            "iid": 46,
            "description": description,
            "source_branch": "feature/work",
        }
        gl = FakeGitLab({"projects/1/merge_requests/46": mr})
        conv = {"issue_iid": issue_iid, "mr_iids": []}
        ps = {"conversations": {issue_iid: conv}, "mr_index": {}}

        watcher.capture_mrs_from_reply(gl, PROJ, ps, issue_iid, conv, "Opened !46")

        return gl, conv, ps

    def test_durable_mapping_with_existing_conversation_is_trusted(self):
        gl = FakeGitLab({})
        ps = {
            "conversations": {"43": {"issue_iid": "43", "mr_iids": ["46"]}},
            "mr_index": {
                "46": {
                    "issue_iid": "43",
                    "conversation_key": "43",
                    "mapped_from": "final_reply",
                }
            },
        }

        mapping, mr = watcher.resolve_mr_mapping(gl, PROJ, ps, "46")

        self.assertEqual(
            mapping,
            {
                "issue_iid": "43",
                "conversation_key": "43",
                "mapped_from": "final_reply",
                "conversation_exists": True,
            },
        )
        self.assertIsNone(mr)
        self.assertEqual(gl.gets, [])

    def test_index_without_conversation_still_requires_marker(self):
        mr = {"iid": 46, "description": "MR body", "source_branch": "issue-43-work"}
        gl = FakeGitLab({"projects/1/merge_requests/46": mr})
        ps = {
            "conversations": {},
            "mr_index": {"46": {"issue_iid": "43", "conversation_key": "43"}},
        }

        mapping, fetched_mr = watcher.resolve_mr_mapping(gl, PROJ, ps, "46")

        self.assertIsNone(mapping)
        self.assertIs(fetched_mr, mr)

    def test_issue_branch_without_marker_stays_standalone(self):
        mr = {
            "iid": 46,
            "title": "Scheduler mailpit",
            "description": "MR body",
            "source_branch": "sw/issue-43-scheduler-mailpit",
            "target_branch": "main",
            "web_url": "https://gitlab.example.com/group/project/-/merge_requests/46",
        }
        issue = {
            "iid": 43,
            "title": "Unrelated issue",
            "description": "Issue body",
            "web_url": "https://gitlab.example.com/group/project/-/issues/43",
        }
        gl = FakeGitLab(
            {
                "projects/1/merge_requests/46": mr,
                "projects/1/issues/43": issue,
                "projects/1/issues/43/notes": [],
                "projects/1/issues/43/links": [],
            }
        )
        ps = {"conversations": {}, "mr_index": {}, "pending_mr_comment_gestures": []}
        gesture = {
            "kind": "mr",
            "mr_iid": "46",
            "body": "unused",
            "comment_body": "@agent why is this failing?",
            "note_id": 99,
            "event_id": 100,
        }

        watcher.assemble(
            gl, PROJ, ps, [gesture], [], set(), "owner", DEFAULTS, ["mention"]
        )

        key, conv = next(iter(ps["conversations"].items()))
        self.assertTrue(key.startswith("mr:46:note:99"))
        self.assertEqual(conv["anchor"], "mr")
        self.assertEqual(ps["mr_index"], {})

    def test_final_reply_mr_with_matching_marker_is_recorded(self):
        description = (
            "<!-- eastwatch: source_project=group/project "
            "source_issue_iid=43 conversation_key=43 -->"
        )

        gl, conv, ps = self.capture_reply_mr(description)

        self.assertEqual(gl.gets, [("projects/1/merge_requests/46", {})])
        self.assertEqual(conv["mr_iids"], ["46"])
        self.assertEqual(ps["mr_index"]["46"]["conversation_key"], "43")
        self.assertEqual(ps["mr_index"]["46"]["mapped_from"], "final_reply")

    def test_final_reply_mr_with_legacy_marker_is_recorded(self):
        description = (
            "<!-- board-watcher: source_project=group/project "
            "source_issue_iid=43 conversation_key=43 -->"
        )

        gl, conv, ps = self.capture_reply_mr(description)

        self.assertEqual(gl.gets, [("projects/1/merge_requests/46", {})])
        self.assertEqual(conv["mr_iids"], ["46"])
        self.assertEqual(ps["mr_index"]["46"]["conversation_key"], "43")
        self.assertEqual(ps["mr_index"]["46"]["mapped_from"], "final_reply")

    def test_final_reply_mr_without_marker_is_not_recorded(self):
        gl, conv, ps = self.capture_reply_mr("MR body")

        self.assertEqual(gl.gets, [("projects/1/merge_requests/46", {})])
        self.assertEqual(conv["mr_iids"], [])
        self.assertEqual(ps["mr_index"], {})

    def test_final_reply_mr_with_wrong_project_marker_is_not_recorded(self):
        description = (
            "<!-- eastwatch: source_project=other/project "
            "source_issue_iid=43 conversation_key=43 -->"
        )

        gl, conv, ps = self.capture_reply_mr(description)

        self.assertEqual(gl.gets, [("projects/1/merge_requests/46", {})])
        self.assertEqual(conv["mr_iids"], [])
        self.assertEqual(ps["mr_index"], {})

    def test_final_reply_mr_with_wrong_issue_marker_is_not_recorded(self):
        description = (
            "<!-- eastwatch: source_project=group/project "
            "source_issue_iid=44 conversation_key=43 -->"
        )

        gl, conv, ps = self.capture_reply_mr(description)

        self.assertEqual(gl.gets, [("projects/1/merge_requests/46", {})])
        self.assertEqual(conv["mr_iids"], [])
        self.assertEqual(ps["mr_index"], {})

    def test_closing_reference_without_marker_does_not_map(self):
        mr = {
            "iid": 46,
            "description": "Closes #43",
            "source_branch": "feature/no-issue",
        }
        gl = FakeGitLab({"projects/1/merge_requests/46": mr})
        ps = {
            "conversations": {"43": {"issue_iid": "43", "mr_iids": []}},
            "mr_index": {},
        }

        mapping, fetched_mr = watcher.resolve_mr_mapping(gl, PROJ, ps, "46")

        self.assertIsNone(mapping)
        self.assertIs(fetched_mr, mr)
        self.assertEqual(ps["mr_index"], {})

    def test_unmapped_agent_mr_comment_creates_mr_qa(self):
        mr = {
            "iid": 46,
            "title": "Scheduler mailpit",
            "description": "MR body",
            "source_branch": "feature/no-issue",
            "target_branch": "main",
            "web_url": "https://gitlab.example.com/group/project/-/merge_requests/46",
        }
        gl = FakeGitLab({"projects/1/merge_requests/46": mr})
        ps = {"conversations": {}, "mr_index": {}, "pending_mr_comment_gestures": []}
        gesture = {
            "kind": "mr",
            "mr_iid": "46",
            "body": "unused",
            "comment_body": "@agent why is this failing?",
            "position": {"new_path": "app.py", "new_line": 12},
            "note_id": 99,
            "event_id": 100,
        }

        watcher.assemble(
            gl, PROJ, ps, [gesture], [], set(), "owner", DEFAULTS, ["mention"]
        )

        self.assertEqual(len(ps["conversations"]), 1)
        key, conv = next(iter(ps["conversations"].items()))
        self.assertTrue(key.startswith("mr:46:note:99"))
        self.assertEqual(conv["anchor"], "mr")
        self.assertEqual(
            conv["next_reply_target"], {"kind": "mr", "mr_iid": "46", "note_id": 99}
        )
        prompt = conv["pending"][0]
        self.assertIn("MR title: Scheduler mailpit", prompt)
        self.assertIn("Source branch: feature/no-issue", prompt)
        self.assertIn("`app.py:12`", prompt)
        self.assertIn("why is this failing?", prompt)

    def test_marker_maps_missing_conversation_to_issue_qa(self):
        mr = {
            "iid": 46,
            "title": "Scheduler mailpit",
            "description": (
                "MR body\n\n"
                "<!-- eastwatch: source_project=group/project "
                "source_issue_iid=43 conversation_key=43 -->"
            ),
            "source_branch": "bugfix/PROJ-123/example-change",
            "target_branch": "main",
            "web_url": "https://gitlab.example.com/group/project/-/merge_requests/46",
        }
        issue = {
            "iid": 43,
            "title": "Scheduler mailpit issue",
            "description": "Issue body",
            "web_url": "https://gitlab.example.com/group/project/-/issues/43",
        }
        gl = FakeGitLab(
            {
                "projects/1/merge_requests/46": mr,
                "projects/1/issues/43": issue,
                "projects/1/issues/43/notes": [],
                "projects/1/issues/43/links": [],
            }
        )
        ps = {"conversations": {}, "mr_index": {}, "pending_mr_comment_gestures": []}
        gesture = {
            "kind": "mr",
            "mr_iid": "46",
            "body": "unused",
            "comment_body": "@agent please review this?",
            "note_id": 101,
            "event_id": 102,
        }

        watcher.assemble(
            gl, PROJ, ps, [gesture], [], set(), "owner", DEFAULTS, ["mention"]
        )

        conv = ps["conversations"]["43"]
        self.assertEqual(conv["anchor"], "issue")
        self.assertEqual(
            conv["next_reply_target"], {"kind": "mr", "mr_iid": "46", "note_id": 101}
        )
        self.assertIn("MR title: Scheduler mailpit", conv["pending"][0])
        self.assertEqual(ps["mr_index"]["46"]["conversation_key"], "43")

    def test_issue_comment_after_mr_comment_replies_to_issue(self):
        issue = {
            "iid": 43,
            "title": "Scheduler mailpit issue",
            "description": "Issue body",
            "web_url": "https://gitlab.example.com/group/project/-/issues/43",
        }
        gl = FakeGitLab(
            {
                "projects/1/issues/43/notes": [],
                "projects/1/issues/43/links": [],
                "projects/1/issues/43/discussions/issue-discussion": {
                    "id": "issue-discussion",
                    "notes": [{"id": 900, "author": {"id": 999}}, {"id": 203}],
                },
                "projects/1/merge_requests/46": {"iid": 46, "state": "opened"},
                "projects/1/merge_requests/46/discussions/mr-discussion": {
                    "id": "mr-discussion",
                    "notes": [{"id": 901, "author": {"id": 999}}, {"id": 201}],
                },
            }
        )
        conv = watcher.make_conversation(
            gl, PROJ, issue, "qa", ["Issue body"], DEFAULTS
        )
        ps = {
            "conversations": {"43": conv},
            "mr_index": {"46": {"conversation_key": "43"}},
            "pending_mr_comment_gestures": [],
        }
        mr_gesture = {
            "kind": "mr",
            "mr_iid": "46",
            "body": "Owner commented on merge request !46:\n\nMR follow-up",
            "comment_body": "MR follow-up",
            "discussion_id": "mr-discussion",
            "note_id": 201,
            "event_id": 202,
        }
        issue_gesture = {
            "kind": "issue",
            "iid": "43",
            "body": "Issue follow-up",
            "discussion_id": "issue-discussion",
            "issue_state": "opened",
            "note_id": 203,
            "event_id": 204,
        }

        watcher.assemble(
            gl, PROJ, ps, [mr_gesture], [], set(), "owner", DEFAULTS, ["mention"]
        )
        self.assertEqual(
            conv["next_reply_target"],
            {
                "kind": "mr",
                "mr_iid": "46",
                "note_id": 201,
                "discussion_id": "mr-discussion",
            },
        )
        conv["reply_target"] = conv.pop("next_reply_target")

        watcher.assemble(
            gl, PROJ, ps, [issue_gesture], [], set(), "owner", DEFAULTS, ["mention"]
        )
        self.assertEqual(
            conv["next_reply_target"],
            {
                "kind": "issue",
                "issue_iid": "43",
                "note_id": 203,
                "discussion_id": "issue-discussion",
            },
        )
        conv["current_run"] = {"reply_target": conv.pop("next_reply_target")}
        watcher.collect_success(
            gl,
            PROJ,
            ps,
            "43",
            conv,
            {
                "reply": "Issue answer\nSTATUS: done",
                "session_file": "/tmp/session.jsonl",
            },
            {"projects": {"gitlab.example.com/group/project": ps}},
        )

        self.assertEqual(
            gl.posts[-1][0], "projects/1/issues/43/discussions/issue-discussion/notes"
        )
        self.assertEqual(
            conv["reply_target"],
            {
                "kind": "issue",
                "issue_iid": "43",
                "note_id": 203,
                "discussion_id": "issue-discussion",
            },
        )

    def test_mr_anchored_parked_answer_is_marked_done(self):
        mr = {
            "iid": 46,
            "title": "Scheduler mailpit",
            "description": "MR body",
            "source_branch": "feature/no-issue",
            "target_branch": "main",
            "web_url": "https://gitlab.example.com/group/project/-/merge_requests/46",
        }
        gl = FakeGitLab({"projects/1/merge_requests/46/discussions": []})
        conv = watcher.make_mr_conversation(
            PROJ, mr, "mr:46:note:99", "qa", ["@agent question"], DEFAULTS
        )
        conv["reply_target"] = {"kind": "mr", "mr_iid": "46", "note_id": 99}
        conv["current_run"] = {"reply_target": conv["reply_target"]}
        ps = {
            "conversations": {"mr:46:note:99": conv},
            "mr_index": {},
            "pending_mr_comment_gestures": [],
        }

        watcher.collect_success(
            gl,
            PROJ,
            ps,
            "mr:46:note:99",
            conv,
            {
                "reply": "Can you clarify the expected output?\nSTATUS: parked",
                "session_file": "/tmp/session.jsonl",
            },
            {"projects": {"gitlab.example.com/group/project": ps}},
        )

        self.assertEqual(gl.posts[-1][0], "projects/1/merge_requests/46/notes")
        self.assertEqual(conv["status"], "done")
        self.assertIsNone(conv["parked_note_id"])

    def test_poll_comments_resolves_thread_discussion_ids(self):
        proj = {**PROJ, "bot_user_id": 999, "bot_username": "eastwatch_bot"}
        gl = FakeGitLab(
            {
                "projects/1/events": [
                    {
                        "id": 301,
                        "author": {"id": 1, "username": "maintainer"},
                        "note": {
                            "id": 501,
                            "noteable_type": "Issue",
                            "noteable_iid": 43,
                            "body": "Issue thread follow-up",
                        },
                    },
                    {
                        "id": 302,
                        "author": {"id": 1, "username": "maintainer"},
                        "note": {
                            "id": 601,
                            "noteable_type": "MergeRequest",
                            "noteable_iid": 46,
                            "body": "MR thread follow-up",
                        },
                    },
                    {
                        "id": 303,
                        "author": {"id": 1, "username": "maintainer"},
                        "note": {
                            "id": 502,
                            "noteable_type": "Issue",
                            "noteable_iid": 43,
                            "discussion_id": "issue-top-level",
                            "body": "Top-level follow-up",
                        },
                    },
                ],
                "projects/1/issues/43/discussions": [
                    {
                        "id": "issue-discussion",
                        "individual_note": False,
                        "notes": [{"id": 501}],
                    },
                ],
                "projects/1/issues/43/discussions/issue-top-level": {
                    "id": "issue-top-level",
                    "individual_note": True,
                    "notes": [{"id": 502}],
                },
                "projects/1/merge_requests/46/discussions": [
                    {
                        "id": "mr-discussion",
                        "individual_note": False,
                        "notes": [{"id": 601}],
                    }
                ],
            }
        )
        ps = {"bootstrapped": True, "last_event_id": 0}

        gestures = watcher.poll_comments(gl, proj, ps, "maintainer")

        self.assertEqual(gestures[0]["discussion_id"], "issue-discussion")
        self.assertEqual(gestures[1]["discussion_id"], "mr-discussion")
        self.assertIsNone(gestures[2]["discussion_id"])

    def test_poll_comments_captures_unthreaded_when_discussion_lookup_is_transient(
        self,
    ):
        def fail_discussion_lookup(_path, **_params):
            raise watcher.requests.RequestException("discussion timeout")

        gl = FakeGitLab(
            {
                "projects/1/events": [
                    {
                        "id": 301,
                        "author": {"id": 1, "username": "maintainer"},
                        "note": {
                            "id": 501,
                            "noteable_type": "Issue",
                            "noteable_iid": 43,
                            "discussion_id": "issue-discussion",
                            "body": "Issue thread follow-up",
                        },
                    }
                ],
                "projects/1/issues/43/discussions/issue-discussion": fail_discussion_lookup,
            }
        )
        ps = {"bootstrapped": True, "last_event_id": 300}

        gestures = watcher.poll_comments(gl, PROJ, ps, "maintainer")

        self.assertEqual(ps["last_event_id"], 301)
        self.assertEqual(len(gestures), 1)
        self.assertEqual(gestures[0]["discussion_id"], None)
        self.assertEqual(gestures[0]["note_id"], 501)

    def test_issue_thread_reply_posts_to_discussion(self):
        issue = {
            "iid": 43,
            "title": "Threaded issue",
            "description": "Issue body",
            "web_url": "https://gitlab.example.com/group/project/-/issues/43",
        }
        gl = FakeGitLab(
            {
                "projects/1/issues/43/notes": [],
                "projects/1/issues/43/links": [],
                "projects/1/issues/43/discussions/issue-discussion": {
                    "id": "issue-discussion",
                    "notes": [{"id": 900, "author": {"id": 999}}, {"id": 501}],
                },
            }
        )
        conv = watcher.make_conversation(
            gl, PROJ, issue, "qa", ["Issue body"], DEFAULTS
        )
        ps = {
            "conversations": {"43": conv},
            "mr_index": {},
            "pending_mr_comment_gestures": [],
        }
        gesture = {
            "kind": "issue",
            "iid": "43",
            "body": "Thread follow-up",
            "discussion_id": "issue-discussion",
            "issue_state": "opened",
            "note_id": 501,
            "event_id": 301,
        }

        watcher.assemble(
            gl, PROJ, ps, [gesture], [], set(), "maintainer", DEFAULTS, ["mention"]
        )
        self.assertEqual(
            conv["next_reply_target"],
            {
                "kind": "issue",
                "issue_iid": "43",
                "note_id": 501,
                "discussion_id": "issue-discussion",
            },
        )
        conv["current_run"] = {"reply_target": conv.pop("next_reply_target")}
        watcher.collect_success(
            gl,
            PROJ,
            ps,
            "43",
            conv,
            {
                "reply": "Issue thread answer\nSTATUS: done",
                "session_file": "/tmp/session.jsonl",
            },
            {"projects": {"gitlab.example.com/group/project": ps}},
        )

        self.assertEqual(
            gl.posts[-1][0], "projects/1/issues/43/discussions/issue-discussion/notes"
        )

    def test_post_conversation_note_falls_back_to_top_level_when_discussion_lookup_is_transient(
        self,
    ):
        def fail_discussion_lookup(_path, **_params):
            raise watcher.requests.RequestException("discussion list timeout")

        gl = FakeGitLab({"projects/1/issues/43/discussions": fail_discussion_lookup})
        conv = {
            "reply_target": {"kind": "issue", "issue_iid": "43", "note_id": 501},
        }

        note = watcher.post_conversation_note(gl, PROJ, conv, "fallback body")

        self.assertEqual(note["id"], 1001)
        self.assertEqual(
            gl.posts, [("projects/1/issues/43/notes", {"body": "fallback body"})]
        )

    def test_issue_discussion_post_failure_falls_back_to_top_level_note(self):
        issue = {
            "iid": 43,
            "title": "Threaded issue",
            "description": "Issue body",
            "web_url": "https://gitlab.example.com/group/project/-/issues/43",
        }
        discussion_path = "projects/1/issues/43/discussions/issue-discussion/notes"
        gl = FakeGitLab(
            {"projects/1/issues/43/notes": [], "projects/1/issues/43/links": []},
            post_errors={
                discussion_path: watcher.requests.RequestException("thread post failed")
            },
        )
        conv = watcher.make_conversation(
            gl, PROJ, issue, "qa", ["Issue body"], DEFAULTS
        )
        conv["current_run"] = {
            "reply_target": {
                "kind": "issue",
                "issue_iid": "43",
                "note_id": 501,
                "discussion_id": "issue-discussion",
            }
        }
        ps = {
            "conversations": {"43": conv},
            "mr_index": {},
            "pending_mr_comment_gestures": [],
        }

        watcher.collect_success(
            gl,
            PROJ,
            ps,
            "43",
            conv,
            {
                "reply": "Issue thread answer\nSTATUS: done",
                "session_file": "/tmp/session.jsonl",
            },
            {"projects": {"gitlab.example.com/group/project": ps}},
        )

        self.assertEqual(
            [path for path, _data in gl.posts],
            [discussion_path, "projects/1/issues/43/notes"],
        )
        self.assertEqual(conv["last_note_id"], 1002)

    def test_find_note_discussion_id_verifies_payload_discussion_id_by_direct_get(self):
        gl = FakeGitLab(
            {
                "projects/1/issues/43/discussions/issue-discussion": {
                    "id": "issue-discussion",
                    "individual_note": False,
                    "notes": [{"id": 501}],
                }
            }
        )

        self.assertEqual(
            watcher.find_note_discussion_id(
                gl, PROJ, "issue", "43", 501, "issue-discussion"
            ),
            "issue-discussion",
        )
        self.assertEqual(
            gl.gets,
            [("projects/1/issues/43/discussions/issue-discussion", {})],
        )

    def test_find_note_discussion_id_finds_note_on_second_discussion_page(self):
        page_one = [
            {"id": f"decoy-{i}", "individual_note": False, "notes": [{"id": 1000 + i}]}
            for i in range(watcher.DISCUSSION_LIST_PER_PAGE)
        ]
        page_two = [
            {
                "id": "target-discussion",
                "individual_note": False,
                "notes": [{"id": 501}],
            }
        ]

        def discussions(_path, **params):
            return page_one if params.get("page") == 1 else page_two

        gl = FakeGitLab({"projects/1/issues/43/discussions": discussions})

        self.assertEqual(
            watcher.find_note_discussion_id(gl, PROJ, "issue", "43", 501),
            "target-discussion",
        )
        self.assertEqual(
            gl.gets,
            [
                ("projects/1/issues/43/discussions", {"per_page": 100, "page": 1}),
                ("projects/1/issues/43/discussions", {"per_page": 100, "page": 2}),
            ],
        )

    def test_mr_thread_reply_posts_to_discussion(self):
        mr = {
            "iid": 46,
            "title": "Threaded MR",
            "description": "MR body",
            "source_branch": "feature/thread",
            "target_branch": "main",
            "web_url": "https://gitlab.example.com/group/project/-/merge_requests/46",
        }
        gl = FakeGitLab({})
        conv = watcher.make_mr_conversation(
            PROJ, mr, "mr:46:note:601", "qa", ["@agent question"], DEFAULTS
        )
        conv["reply_target"] = {
            "kind": "mr",
            "mr_iid": "46",
            "note_id": 601,
            "discussion_id": "mr-discussion",
        }
        conv["current_run"] = {"reply_target": conv["reply_target"]}
        ps = {
            "conversations": {"mr:46:note:601": conv},
            "mr_index": {},
            "pending_mr_comment_gestures": [],
        }

        watcher.collect_success(
            gl,
            PROJ,
            ps,
            "mr:46:note:601",
            conv,
            {
                "reply": "MR thread answer\nSTATUS: done",
                "session_file": "/tmp/session.jsonl",
            },
            {"projects": {"gitlab.example.com/group/project": ps}},
        )

        self.assertEqual(
            gl.posts[-1][0],
            "projects/1/merge_requests/46/discussions/mr-discussion/notes",
        )

    def test_mr_discussion_post_failure_falls_back_to_top_level_note(self):
        mr = {
            "iid": 46,
            "title": "Threaded MR",
            "description": "MR body",
            "source_branch": "feature/thread",
            "target_branch": "main",
            "web_url": "https://gitlab.example.com/group/project/-/merge_requests/46",
        }
        discussion_path = "projects/1/merge_requests/46/discussions/mr-discussion/notes"
        gl = FakeGitLab(
            {},
            post_errors={
                discussion_path: watcher.requests.RequestException("thread post failed")
            },
        )
        conv = watcher.make_mr_conversation(
            PROJ, mr, "mr:46:note:601", "qa", ["@agent question"], DEFAULTS
        )
        conv["current_run"] = {
            "reply_target": {
                "kind": "mr",
                "mr_iid": "46",
                "note_id": 601,
                "discussion_id": "mr-discussion",
            }
        }
        ps = {
            "conversations": {"mr:46:note:601": conv},
            "mr_index": {},
            "pending_mr_comment_gestures": [],
        }

        watcher.collect_success(
            gl,
            PROJ,
            ps,
            "mr:46:note:601",
            conv,
            {
                "reply": "MR thread answer\nSTATUS: done",
                "session_file": "/tmp/session.jsonl",
            },
            {"projects": {"gitlab.example.com/group/project": ps}},
        )

        self.assertEqual(
            [path for path, _data in gl.posts],
            [discussion_path, "projects/1/merge_requests/46/notes"],
        )
        self.assertEqual(conv["last_note_id"], 1002)


def _standalone_mr_conv(gl, answer_note_id=500):
    """A standalone MR Q&A conversation that has already posted answer note 500."""
    mr = {
        "iid": 46,
        "title": "Standalone MR",
        "description": "MR body",
        "source_branch": "feature/no-issue",
        "target_branch": "main",
        "web_url": "https://gitlab.example.com/group/project/-/merge_requests/46",
    }
    conv = watcher.make_mr_conversation(
        PROJ, mr, "mr:46:note:99", "qa", ["@agent original"], DEFAULTS
    )
    conv["status"] = "done"
    conv["last_note_id"] = answer_note_id
    ps = {
        "conversations": {"mr:46:note:99": conv},
        "mr_index": {},
        "pending_mr_comment_gestures": [],
    }
    return conv, ps


class StandaloneMRThreadResumeTest(unittest.TestCase):
    MR = {"iid": 46, "state": "opened"}
    THREAD = {
        "id": "mr-thread",
        "individual_note": False,
        "notes": [{"id": 500, "author": {"id": 999}}, {"id": 700}],
    }

    def test_plain_thread_reply_resumes_standalone_conversation(self):
        gl = FakeGitLab(
            {
                "projects/1/merge_requests/46/discussions/mr-thread": self.THREAD,
                "projects/1/merge_requests/46": self.MR,
            }
        )
        conv, ps = _standalone_mr_conv(gl)
        gesture = {
            "kind": "mr",
            "mr_iid": "46",
            "body": "Owner commented on merge request !46:\n\ndoes this handle retries?",
            "comment_body": "does this handle retries?",
            "discussion_id": "mr-thread",
            "mr_state": "opened",
            "note_id": 700,
            "event_id": 701,
        }

        watcher.assemble(
            gl, PROJ, ps, [gesture], [], set(), "owner", DEFAULTS, ["mention"]
        )

        # No duplicate conversation, and the reply landed on the existing one.
        self.assertEqual(list(ps["conversations"]), ["mr:46:note:99"])
        self.assertEqual(len(conv["pending"]), 1)
        self.assertIn("does this handle retries?", conv["pending"][0])
        self.assertEqual(
            conv["next_reply_target"],
            {
                "kind": "mr",
                "mr_iid": "46",
                "note_id": 700,
                "discussion_id": "mr-thread",
            },
        )

    def test_agent_reply_in_thread_does_not_spawn_duplicate(self):
        gl = FakeGitLab(
            {
                "projects/1/merge_requests/46/discussions/mr-thread": self.THREAD,
                "projects/1/merge_requests/46": self.MR,
            }
        )
        conv, ps = _standalone_mr_conv(gl)
        gesture = {
            "kind": "mr",
            "mr_iid": "46",
            "body": "unused",
            "comment_body": "@agent what about retries?",
            "discussion_id": "mr-thread",
            "mr_state": "opened",
            "note_id": 700,
            "event_id": 701,
        }

        watcher.assemble(
            gl, PROJ, ps, [gesture], [], set(), "owner", DEFAULTS, ["mention"]
        )

        self.assertEqual(list(ps["conversations"]), ["mr:46:note:99"])
        self.assertEqual(len(conv["pending"]), 1)
        self.assertIn("what about retries?", conv["pending"][0])
        self.assertNotIn("@agent", conv["pending"][0])

    def test_thread_reply_matches_via_originating_note_in_key(self):
        # Bot's answer never recorded (last_note_id None), but the thread carries
        # the originating @agent note (99) encoded in the conversation key.
        thread = {
            "id": "mr-thread",
            "individual_note": False,
            "notes": [{"id": 99}, {"id": 88, "author": {"id": 999}}, {"id": 700}],
        }
        gl = FakeGitLab(
            {
                "projects/1/merge_requests/46/discussions/mr-thread": thread,
                "projects/1/merge_requests/46": self.MR,
            }
        )
        conv, ps = _standalone_mr_conv(gl, answer_note_id=None)
        gesture = {
            "kind": "mr",
            "mr_iid": "46",
            "body": "Owner commented on merge request !46:\n\nfollow-up",
            "comment_body": "follow-up",
            "discussion_id": "mr-thread",
            "mr_state": "opened",
            "note_id": 700,
            "event_id": 701,
        }

        watcher.assemble(
            gl, PROJ, ps, [gesture], [], set(), "owner", DEFAULTS, ["mention"]
        )

        self.assertEqual(list(ps["conversations"]), ["mr:46:note:99"])
        self.assertEqual(len(conv["pending"]), 1)

    def test_thread_reply_without_bot_note_is_not_resumed(self):
        # Owner replied under their own note (no bot note in the thread) -> the
        # thread is not the agent's answer thread, so we do not resume.
        thread = {
            "id": "mr-thread",
            "individual_note": False,
            "notes": [{"id": 99}, {"id": 700}],
        }
        gl = FakeGitLab(
            {
                "projects/1/merge_requests/46/discussions/mr-thread": thread,
                "projects/1/merge_requests/46": self.MR,
            }
        )
        conv, ps = _standalone_mr_conv(gl)
        gesture = {
            "kind": "mr",
            "mr_iid": "46",
            "body": "Owner commented on merge request !46:\n\nplain",
            "comment_body": "plain",
            "discussion_id": "mr-thread",
            "mr_state": "opened",
            "note_id": 700,
            "event_id": 701,
        }

        watcher.assemble(
            gl, PROJ, ps, [gesture], [], set(), "owner", DEFAULTS, ["mention"]
        )

        self.assertEqual(conv["pending"], [])
        self.assertNotIn("next_reply_target", conv)

    def test_top_level_plain_comment_still_ignored(self):
        # No discussion_id -> not a thread reply -> unchanged legacy behavior.
        gl = FakeGitLab({"projects/1/merge_requests/46": self.MR})
        conv, ps = _standalone_mr_conv(gl)
        gesture = {
            "kind": "mr",
            "mr_iid": "46",
            "body": "Owner commented on merge request !46:\n\ntop level",
            "comment_body": "top level",
            "mr_state": "opened",
            "note_id": 700,
            "event_id": 701,
        }

        watcher.assemble(
            gl, PROJ, ps, [gesture], [], set(), "owner", DEFAULTS, ["mention"]
        )

        self.assertEqual(list(ps["conversations"]), ["mr:46:note:99"])
        self.assertEqual(conv["pending"], [])
        self.assertNotIn("next_reply_target", conv)

    def test_thread_reply_on_closed_mr_is_dropped(self):
        gl = FakeGitLab(
            {"projects/1/merge_requests/46/discussions/mr-thread": self.THREAD}
        )
        conv, ps = _standalone_mr_conv(gl)
        gesture = {
            "kind": "mr",
            "mr_iid": "46",
            "body": "Owner commented on merge request !46:\n\nlate reply",
            "comment_body": "late reply",
            "discussion_id": "mr-thread",
            "mr_state": "merged",
            "note_id": 700,
            "event_id": 701,
        }

        watcher.assemble(
            gl, PROJ, ps, [gesture], [], set(), "owner", DEFAULTS, ["mention"]
        )

        self.assertEqual(conv["pending"], [])
        self.assertNotIn("next_reply_target", conv)

    def test_thread_lookup_transient_defers_gesture(self):
        def fail_lookup(_path, **_params):
            raise watcher.requests.RequestException("discussion unavailable")

        gl = FakeGitLab(
            {"projects/1/merge_requests/46/discussions/mr-thread": fail_lookup}
        )
        conv, ps = _standalone_mr_conv(gl)
        gesture = {
            "kind": "mr",
            "mr_iid": "46",
            "body": "Owner commented on merge request !46:\n\nretry me",
            "comment_body": "retry me",
            "discussion_id": "mr-thread",
            "mr_state": "opened",
            "note_id": 700,
            "event_id": 701,
        }

        watcher.assemble(
            gl, PROJ, ps, [gesture], [], set(), "owner", DEFAULTS, ["mention"]
        )

        self.assertEqual(conv["pending"], [])
        self.assertNotIn("next_reply_target", conv)
        self.assertEqual(ps["pending_mr_comment_gestures"], [gesture])


if __name__ == "__main__":
    unittest.main()
