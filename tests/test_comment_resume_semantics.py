import atexit
import shutil
import tempfile
import types
import unittest
from pathlib import Path

from tests.support import load_watcher

TMP_ROOT = Path(tempfile.mkdtemp(prefix="eastwatch-resume-semantics-test-"))
watcher = load_watcher(TMP_ROOT)

atexit.register(lambda: shutil.rmtree(TMP_ROOT, ignore_errors=True))


class FakeGitLab:
    def __init__(self, routes):
        self.routes = routes

    def get(self, path, **params):
        value = self.routes[path]
        return value() if callable(value) else value


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


def conv():
    return {
        "provider": "pi",
        "model": "gpt",
        "effort": "low",
        "pending": [],
        "status": "done",
        "last_note_id": None,
        "parked_note_id": None,
    }


def assemble_once(gl, ps, gesture, triggers=("mention",)):
    watcher.assemble(gl, PROJ, ps, [gesture], [], set(), "stanwang", DEFAULTS, list(triggers))


def http_error(status_code):
    error = watcher.requests.HTTPError(f"HTTP {status_code}")
    error.response = types.SimpleNamespace(status_code=status_code)
    return error


class CommentResumeSemanticsTest(unittest.TestCase):
    def test_closed_issue_plain_comment_is_ignored(self):
        existing = conv()
        ps = {"conversations": {"20": existing}, "mr_index": {}, "pending_mr_comment_gestures": []}
        gesture = {"kind": "issue", "iid": "20", "body": "bookkeeping", "issue_state": "closed", "note_id": 1}

        assemble_once(FakeGitLab({}), ps, gesture)

        self.assertEqual(existing["pending"], [])
        self.assertNotIn("next_reply_target", existing)

    def test_open_issue_top_level_plain_comment_is_ignored(self):
        existing = conv()
        ps = {"conversations": {"20": existing}, "mr_index": {}, "pending_mr_comment_gestures": []}
        gesture = {"kind": "issue", "iid": "20", "body": "top level", "issue_state": "opened", "note_id": 2}

        assemble_once(FakeGitLab({}), ps, gesture)

        self.assertEqual(existing["pending"], [])
        self.assertNotIn("next_reply_target", existing)

    def test_issue_plain_reply_in_bot_note_discussion_resumes(self):
        existing = conv()
        ps = {"conversations": {"20": existing}, "mr_index": {}, "pending_mr_comment_gestures": []}
        gl = FakeGitLab(
            {
                "projects/1/issues/20/discussions/bot-thread": {
                    "id": "bot-thread",
                    "notes": [{"id": 10, "author": {"id": 999}}, {"id": 11}],
                }
            }
        )
        gesture = {
            "kind": "issue",
            "iid": "20",
            "body": "reply in bot thread",
            "discussion_id": "bot-thread",
            "issue_state": "opened",
            "note_id": 11,
        }

        assemble_once(gl, ps, gesture)

        self.assertEqual(existing["pending"], ["reply in bot thread"])
        self.assertEqual(
            existing["next_reply_target"],
            {"kind": "issue", "issue_iid": "20", "note_id": 11, "discussion_id": "bot-thread"},
        )

    def test_issue_plain_reply_discussion_lookup_failure_is_deferred(self):
        existing = conv()
        ps = {"conversations": {"20": existing}, "mr_index": {}, "pending_mr_comment_gestures": []}

        def fail_lookup():
            raise watcher.requests.RequestException("discussion unavailable")

        gl = FakeGitLab({"projects/1/issues/20/discussions/bot-thread": fail_lookup})
        gesture = {
            "kind": "issue",
            "iid": "20",
            "body": "retry me",
            "discussion_id": "bot-thread",
            "issue_state": "opened",
            "note_id": 12,
            "event_id": 13,
        }

        assemble_once(gl, ps, gesture)

        self.assertEqual(existing["pending"], [])
        self.assertNotIn("next_reply_target", existing)
        self.assertEqual(ps["pending_mr_comment_gestures"], [gesture])

    def test_issue_agent_mention_top_level_resumes_open_and_closed(self):
        for state in ("opened", "closed"):
            with self.subTest(state=state):
                existing = conv()
                ps = {"conversations": {"20": existing}, "mr_index": {}, "pending_mr_comment_gestures": []}
                gesture = {"kind": "issue", "iid": "20", "body": "@agent please continue", "issue_state": state, "note_id": 3}

                assemble_once(FakeGitLab({}), ps, gesture)

                self.assertEqual(existing["pending"], ["please continue"])
                self.assertEqual(existing["next_reply_target"], {"kind": "issue", "issue_iid": "20", "note_id": 3})

    def test_closed_mr_plain_comment_is_ignored(self):
        for state in ("merged", "closed"):
            with self.subTest(state=state):
                existing = conv()
                ps = {
                    "conversations": {"20": existing},
                    "mr_index": {"7": {"conversation_key": "20"}},
                    "pending_mr_comment_gestures": [],
                }
                gesture = {
                    "kind": "mr",
                    "mr_iid": "7",
                    "body": "Owner commented on merge request !7:\n\nbookkeeping",
                    "comment_body": "bookkeeping",
                    "mr_state": state,
                    "note_id": 4,
                }

                assemble_once(FakeGitLab({}), ps, gesture)

                self.assertEqual(existing["pending"], [])
                self.assertNotIn("next_reply_target", existing)

    def test_open_mr_top_level_plain_comment_is_ignored(self):
        existing = conv()
        ps = {
            "conversations": {"20": existing},
            "mr_index": {"7": {"conversation_key": "20"}},
            "pending_mr_comment_gestures": [],
        }
        gesture = {
            "kind": "mr",
            "mr_iid": "7",
            "body": "Owner commented on merge request !7:\n\ntop level",
            "comment_body": "top level",
            "mr_state": "opened",
            "note_id": 5,
        }

        assemble_once(FakeGitLab({}), ps, gesture)

        self.assertEqual(existing["pending"], [])
        self.assertNotIn("next_reply_target", existing)

    def test_mr_plain_reply_in_bot_note_discussion_resumes(self):
        existing = conv()
        ps = {
            "conversations": {"20": existing},
            "mr_index": {"7": {"conversation_key": "20"}},
            "pending_mr_comment_gestures": [],
        }
        gl = FakeGitLab(
            {
                "projects/1/merge_requests/7/discussions/bot-thread": {
                    "id": "bot-thread",
                    "notes": [{"id": 70, "author": {"id": 999}}, {"id": 71}],
                }
            }
        )
        gesture = {
            "kind": "mr",
            "mr_iid": "7",
            "body": "Owner commented on merge request !7:\n\nreply in bot thread",
            "comment_body": "reply in bot thread",
            "discussion_id": "bot-thread",
            "mr_state": "opened",
            "note_id": 71,
        }

        assemble_once(gl, ps, gesture)

        self.assertEqual(existing["pending"], ["Owner commented on merge request !7:\n\nreply in bot thread"])
        self.assertEqual(
            existing["next_reply_target"],
            {"kind": "mr", "mr_iid": "7", "note_id": 71, "discussion_id": "bot-thread"},
        )

    def test_mr_plain_reply_discussion_lookup_failure_is_deferred(self):
        existing = conv()
        ps = {
            "conversations": {"20": existing},
            "mr_index": {"7": {"conversation_key": "20"}},
            "pending_mr_comment_gestures": [],
        }

        def fail_lookup():
            raise watcher.requests.RequestException("discussion unavailable")

        gl = FakeGitLab({"projects/1/merge_requests/7/discussions/bot-thread": fail_lookup})
        gesture = {
            "kind": "mr",
            "mr_iid": "7",
            "body": "Owner commented on merge request !7:\n\nretry me",
            "comment_body": "retry me",
            "discussion_id": "bot-thread",
            "mr_state": "opened",
            "note_id": 72,
            "event_id": 73,
        }

        assemble_once(gl, ps, gesture)

        self.assertEqual(existing["pending"], [])
        self.assertNotIn("next_reply_target", existing)
        self.assertEqual(ps["pending_mr_comment_gestures"], [gesture])

    def test_mr_plain_reply_discussion_404_is_dropped(self):
        existing = conv()
        ps = {
            "conversations": {"20": existing},
            "mr_index": {"7": {"conversation_key": "20"}},
            "pending_mr_comment_gestures": [],
        }

        def missing_discussion():
            raise http_error(404)

        gl = FakeGitLab({"projects/1/merge_requests/7/discussions/stale-thread": missing_discussion})
        gesture = {
            "kind": "mr",
            "mr_iid": "7",
            "body": "Owner commented on merge request !7:\n\nstale thread",
            "comment_body": "stale thread",
            "discussion_id": "stale-thread",
            "mr_state": "opened",
            "note_id": 74,
            "event_id": 75,
        }

        assemble_once(gl, ps, gesture)

        self.assertEqual(existing["pending"], [])
        self.assertNotIn("next_reply_target", existing)
        self.assertEqual(ps["pending_mr_comment_gestures"], [])

    def test_mr_agent_mention_top_level_resumes_open_and_closed(self):
        for state in ("opened", "closed"):
            with self.subTest(state=state):
                existing = conv()
                ps = {
                    "conversations": {"20": existing},
                    "mr_index": {"7": {"conversation_key": "20"}},
                    "pending_mr_comment_gestures": [],
                }
                gl = FakeGitLab({"projects/1/merge_requests/7": {"iid": 7, "state": state}})
                gesture = {
                    "kind": "mr",
                    "mr_iid": "7",
                    "body": "Owner commented on merge request !7:\n\n@agent continue",
                    "comment_body": "@agent continue",
                    "mr_state": state,
                    "note_id": 6,
                }

                assemble_once(gl, ps, gesture)

                self.assertEqual(len(existing["pending"]), 1)
                self.assertIn("continue", existing["pending"][0])
                self.assertNotIn("@agent", existing["pending"][0])
                self.assertEqual(existing["next_reply_target"], {"kind": "mr", "mr_iid": "7", "note_id": 6})

    def test_award_resume_path_is_unchanged(self):
        existing = conv()
        existing["last_note_id"] = 55
        ps = {"conversations": {"20": existing}, "mr_index": {}, "pending_mr_comment_gestures": []}

        watcher.assemble(
            FakeGitLab({}),
            PROJ,
            ps,
            [],
            [],
            {("note:55", "white_check_mark", "stanwang")},
            "stanwang",
            DEFAULTS,
            ["mention"],
        )

        self.assertEqual(existing["pending"], [watcher.APPROVAL_MESSAGE])
        self.assertEqual(existing["next_reply_target"], {"kind": "issue", "issue_iid": "20"})


if __name__ == "__main__":
    unittest.main()
