"""Vault status poller — adopt-only bootstrap, status-only generation, blocked
skip, crash-exactly-once outbox, rename-resume, and forge-aware write-back.

Mirrors ``tests/test_github_status.py`` but drives the *real* ``VaultBoard`` over
notes on disk (via ``build_forge_client`` + ``fetch_vault_inputs``) — closer to
production than a fake, and cheap because the board is local.
"""

from __future__ import annotations

import atexit
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.support import load_watcher

TMP_ROOT = Path(tempfile.mkdtemp(prefix="eastwatch-vault-poller-test-"))
watcher = load_watcher(TMP_ROOT)
atexit.register(lambda: shutil.rmtree(TMP_ROOT, ignore_errors=True))

STATE_KEY = "local/vault-board"


def note(status, *, tags="task", extra="", body="body"):
    tag_block = f"tags: {tags}\n" if isinstance(tags, str) else "tags:\n  - task\n"
    return f"---\nstatus: {status}\n{tag_block}{extra}---\n\n{body}\n"


class VaultPollerTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name)
        self.tasks_dir = self.vault / "inbox" / "tasks"
        self.tasks_dir.mkdir(parents=True)
        self.proj = {
            "forge": "vault",
            "host": "local",
            "path": "vault/board",
            "vault_path": str(self.vault),
            "commit_results": False,
        }
        self.client = watcher.build_forge_client(self.proj, {})
        self.state = {"projects": {}}
        self.ps = watcher.project_state(self.state, STATE_KEY)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, text):
        (self.tasks_dir / name).write_text(text, encoding="utf-8")
        return self.tasks_dir / name

    def poll(self):
        return watcher.fetch_vault_inputs(self.client, self.proj, self.ps)

    def bootstrap(self):
        """Run the adopt-only first tick, then flip to steady state."""
        self.poll()
        self.ps["bootstrapped"] = True


class BootstrapTest(VaultPollerTestBase):
    def test_adopt_only_bootstrap_dispatches_nothing(self):
        self.write("a.md", note("open"))
        self.write("b.md", note("agent"))  # pre-existing `agent` must NOT fire
        self.assertFalse(self.ps["bootstrapped"])

        out = self.poll()

        self.assertEqual(out["label_fires"], [])
        self.assertEqual(self.ps["vault_outbox"], {})
        self.assertEqual(watcher.vault_dispatch_fires(self.ps), [])
        # Both notes adopted into observations.
        self.assertEqual(len(self.ps["vault_observations"]), 2)


class DispatchTransitionTest(VaultPollerTestBase):
    def test_open_to_agent_fires_once_and_does_not_refire(self):
        path = self.write("a.md", note("open"))
        self.bootstrap()

        self.client.set_status(str(path), "agent")
        self.poll()
        fires = watcher.vault_dispatch_fires(self.ps)
        self.assertEqual(len(fires), 1)
        self.assertEqual(fires[0]["label"], watcher.TRIGGER_LABELS[0])
        self.assertEqual(fires[0]["issue"]["note_path"], "inbox/tasks/a.md")

        # A second identical poll (still `agent`, same status-only generation) does
        # not queue a second command.
        self.poll()
        self.assertEqual(len(self.ps["vault_outbox"]), 1)

    def test_generation_excludes_mtime(self):
        # Touch without a content change -> no new generation, no fire.
        path = self.write("a.md", note("agent"))
        self.bootstrap()
        path.write_text(
            path.read_text(encoding="utf-8"), encoding="utf-8"
        )  # rewrite, same bytes

        self.poll()

        self.assertEqual(self.ps["vault_outbox"], {})


class BlockedTest(VaultPollerTestBase):
    def test_blocked_note_skips_then_fires_once_unblocked(self):
        blocker = self.write("blocker.md", note("open"))
        main = self.write(
            "main.md",
            note("open", extra='blockedBy:\n  - "[[inbox/tasks/blocker|blocker]]"\n'),
        )
        self.bootstrap()

        # Dragged to agent while the blocker is still open -> no dispatch.
        self.client.set_status(str(main), "agent")
        self.poll()
        self.assertEqual(self.ps["vault_outbox"], {})

        # Move main back, resolve the blocker, then drag again -> now it fires.
        self.client.set_status(str(main), "open")
        self.poll()
        self.client.set_status(str(blocker), "done")
        self.poll()
        self.client.set_status(str(main), "agent")
        self.poll()

        fires = watcher.vault_dispatch_fires(self.ps)
        self.assertEqual(len(fires), 1)
        self.assertEqual(fires[0]["issue"]["note_path"], "inbox/tasks/main.md")

    def test_deferred_note_autofires_when_blocker_completes(self):
        # The blocked note has no transition of its own when the blocker finishes,
        # so the poller must re-check the deferred note and fire it — no re-drag.
        blocker = self.write("blocker.md", note("open"))
        main = self.write(
            "main.md",
            note("open", extra='blockedBy:\n  - "[[inbox/tasks/blocker|blocker]]"\n'),
        )
        self.bootstrap()

        self.client.set_status(str(main), "agent")  # dragged while blocked -> deferred
        self.poll()
        self.assertEqual(self.ps["vault_outbox"], {})

        self.client.set_status(
            str(blocker), "done"
        )  # blocker done; main stays at `agent`
        self.poll()
        fires = watcher.vault_dispatch_fires(self.ps)
        self.assertEqual(len(fires), 1)
        self.assertEqual(fires[0]["issue"]["note_path"], "inbox/tasks/main.md")

        # Idle tick afterwards does not re-fire.
        self.poll()
        self.assertEqual(len(self.ps["vault_outbox"]), 1)


class CrashExactlyOnceTest(VaultPollerTestBase):
    def test_outbox_replays_until_marked_then_never_again(self):
        path = self.write("a.md", note("open"))
        self.bootstrap()
        self.client.set_status(str(path), "agent")
        self.poll()

        # After fetch, before mark: the fire is durably recorded and replayable.
        fires_a = watcher.vault_dispatch_fires(self.ps)
        self.assertEqual(len(fires_a), 1)
        gen = fires_a[0]["_generation"]
        # Re-fetch before marking still shows exactly the same one fire.
        self.poll()
        fires_b = watcher.vault_dispatch_fires(self.ps)
        self.assertEqual([f["_generation"] for f in fires_b], [gen])

        # Mark consumed: the fire is gone and re-fetching does not re-add it.
        watcher.vault_mark_dispatched(self.ps, fires_b)
        self.assertTrue(self.ps["vault_outbox"][gen]["dispatched"])
        self.assertEqual(watcher.vault_dispatch_fires(self.ps), [])
        self.poll()
        self.assertEqual(watcher.vault_dispatch_fires(self.ps), [])


class ConversationTest(VaultPollerTestBase):
    def _issue(self):
        items = self.client.fetch_items()
        self.assertEqual(len(items), 1)
        return watcher.vault_issue_from_item(items[0])

    def test_make_conversation_sets_vault_fields_and_default_model(self):
        self.write("a.md", note("agent"))
        conv = watcher.make_conversation(
            None,
            self.proj,
            self._issue(),
            "agent::ready",
            [],
            {"agent::ready": "claude:opus"},
        )

        self.assertEqual(conv["forge"], "vault")
        self.assertEqual(conv["cwd"], str(self.vault))
        self.assertEqual(conv["provider"], "claude")
        self.assertEqual(conv["model"], "opus")  # from defaults[kind]
        self.assertEqual(conv["abs_path"], str((self.tasks_dir / "a.md").resolve()))
        self.assertIsNone(conv["session_id"])

    def test_note_model_hint_wins_over_default(self):
        self.write("a.md", note("agent", extra="model: pi:gpt-5\n"))
        conv = watcher.make_conversation(
            None,
            self.proj,
            self._issue(),
            "agent::ready",
            [],
            {"agent::ready": "claude:opus"},
        )
        self.assertEqual(conv["provider"], "pi")
        self.assertEqual(conv["model"], "gpt-5")

    def test_rename_resume_seeds_session_id_from_note(self):
        # A note carrying a persisted session-id (its item_id is whatever the
        # current path hashes to) resumes the same claude session.
        self.write("renamed.md", note("agent", extra="session-id: sess-abc-123\n"))
        conv = watcher.make_conversation(
            None,
            self.proj,
            self._issue(),
            "agent::ready",
            [],
            {"agent::ready": "claude:sonnet"},
        )
        self.assertEqual(conv["session_id"], "sess-abc-123")


class WriteBackTest(VaultPollerTestBase):
    def _conv_for(self, path):
        items = self.client.fetch_items()
        issue = next(
            watcher.vault_issue_from_item(i)
            for i in items
            if i["abs_path"] == str(path.resolve())
        )
        return watcher.make_conversation(
            None,
            self.proj,
            issue,
            "agent::ready",
            [],
            {"agent::ready": "claude:sonnet"},
        )

    def test_parked_result_writes_needs_input_and_result_section(self):
        path = self.write("a.md", note("in-progress", extra="session-id: sess-1\n"))
        conv = self._conv_for(path)

        watcher.post_conversation_note(
            None, self.proj, conv, "STATUS: parked\n\nNeed the owner."
        )
        watcher.set_issue_agent_label(
            None, self.proj, conv["issue_iid"], watcher.PARKED_LABEL, conv
        )

        text = path.read_text(encoding="utf-8")
        self.assertEqual(self.client.read_status(str(path)), "needs-input")
        self.assertIn("## Result", text)
        self.assertIn("Need the owner.", text)
        self.assertIn("session-id: sess-1", text)  # stamped for rename-resume

    def test_for_human_result_writes_review(self):
        path = self.write("a.md", note("in-progress"))
        conv = self._conv_for(path)

        watcher.post_conversation_note(None, self.proj, conv, "All done.")
        watcher.set_issue_agent_label(
            None, self.proj, conv["issue_iid"], watcher.FOR_HUMAN_LABEL, conv
        )

        self.assertEqual(self.client.read_status(str(path)), "review")
        self.assertIn("## Result", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
