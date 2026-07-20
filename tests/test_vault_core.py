"""VaultBoard TaskNotes board client — surgical reads/writes over real temp notes.

The `forge: vault` provider's authority is each task note's YAML frontmatter
`status:` field. These tests exercise the client directly on real files in a
temp vault: parsing TaskNotes frontmatter, byte-for-byte surgical status writes,
idempotent result sections, atomic replace, and best-effort single-file commits.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from eastwatch.vault import VaultBoard, split_frontmatter, wikilink_targets

# A realistic TaskNotes note: list-form tags, a `reminders:` block with an offset
# duration, quoted wikilinks, `cssclasses`, and a bare `model:` — exactly the
# shapes a naive yaml round-trip would reorder or corrupt.
GOLDEN_NOTE = """\
---
title: Research something local
status: open
tags:
  - task
model: claude:opus
cssclasses:
  - tasknote
due: 2026-08-01
reminders:
  - type: relative
    relatedProp: due
    offset: -P1D
blockedBy:
  - "[[inbox/tasks/blocker|blocker]]"
---

Body text with a wikilink to [[somewhere]].

Second paragraph.
"""


class VaultBoardTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.vault = Path(self.temp.name)
        self.tasks_dir = self.vault / "inbox" / "tasks"
        self.tasks_dir.mkdir(parents=True)
        self.board = VaultBoard(str(self.vault))

    def tearDown(self):
        self.temp.cleanup()

    def write_note(self, name: str, text: str) -> Path:
        path = self.tasks_dir / name
        path.write_text(text, encoding="utf-8")
        return path


class FetchItemsTest(VaultBoardTestBase):
    def test_parses_tasknote_frontmatter(self):
        self.write_note("task.md", GOLDEN_NOTE)
        items = self.board.fetch_items()

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["item_id"], VaultBoard.item_id_for("inbox/tasks/task.md"))
        self.assertEqual(item["status"], "open")
        self.assertEqual(item["note_path"], "inbox/tasks/task.md")
        self.assertEqual(item["title"], "Research something local")
        self.assertEqual(item["model"], "claude:opus")
        # Quoted wikilink target is stripped of the `|alias` and the `.md` stays as-is.
        self.assertEqual(item["blocked_by"], ["inbox/tasks/blocker"])
        self.assertTrue(item["body"].startswith("Body text"))
        self.assertNotIn("status:", item["body"])  # frontmatter stripped from body

    def test_excludes_note_without_task_tag(self):
        self.write_note(
            "note.md",
            "---\ntitle: Just a note\nstatus: open\ntags:\n  - note\n---\n\nbody\n",
        )
        self.assertEqual(self.board.fetch_items(), [])

    def test_excludes_note_without_status(self):
        self.write_note("no-status.md", "---\ntitle: T\ntags: task\n---\n\nbody\n")
        self.assertEqual(self.board.fetch_items(), [])

    def test_skips_malformed_yaml_without_raising(self):
        # A broken frontmatter note must be skipped, not crash the cycle, and it
        # must not hide a valid neighbour.
        self.write_note("broken.md", "---\ntitle: [unterminated\nstatus: agent\ntags: task\n---\n\nx\n")
        self.write_note("good.md", "---\nstatus: agent\ntags: task\n---\n\nok\n")

        items = self.board.fetch_items()

        self.assertEqual([i["note_path"] for i in items], ["inbox/tasks/good.md"])


class IdentityTest(VaultBoardTestBase):
    def test_item_id_stable_across_body_edit(self):
        path = self.write_note("task.md", "---\nstatus: open\ntags: task\n---\n\nfirst body\n")
        before = self.board.fetch_items()[0]["item_id"]

        path.write_text("---\nstatus: open\ntags: task\n---\n\nedited much longer body\n", encoding="utf-8")
        after = self.board.fetch_items()[0]["item_id"]

        self.assertEqual(before, after)

    def test_item_id_changes_on_rename(self):
        self.assertNotEqual(
            VaultBoard.item_id_for("inbox/tasks/task.md"),
            VaultBoard.item_id_for("inbox/tasks/task-renamed.md"),
        )


class SurgicalWriteTest(VaultBoardTestBase):
    def test_set_status_touches_only_the_status_line(self):
        path = self.write_note("task.md", GOLDEN_NOTE)
        self.board.set_status(str(path), "in-progress")

        before = GOLDEN_NOTE.splitlines(keepends=True)
        after = path.read_text(encoding="utf-8").splitlines(keepends=True)

        self.assertEqual(len(before), len(after))
        diffs = [(b, a) for b, a in zip(before, after) if b != a]
        self.assertEqual(len(diffs), 1)  # exactly one line changed
        self.assertEqual(diffs[0][0], "status: open\n")
        self.assertEqual(diffs[0][1], "status: in-progress\n")

    def test_set_status_fenced_refuses_when_not_active(self):
        path = self.write_note("task.md", "---\nstatus: review\ntags: task\n---\n\nbody\n")
        original = path.read_text(encoding="utf-8")

        wrote = self.board.set_status_fenced(str(path), "done")

        self.assertFalse(wrote)
        self.assertEqual(path.read_text(encoding="utf-8"), original)  # no write at all

    def test_set_status_fenced_writes_when_active(self):
        path = self.write_note("task.md", "---\nstatus: in-progress\ntags: task\n---\n\nbody\n")

        wrote = self.board.set_status_fenced(str(path), "review")

        self.assertTrue(wrote)
        self.assertEqual(self.board.read_status(str(path)), "review")

    def test_write_frontmatter_field_inserts_then_replaces(self):
        path = self.write_note("task.md", "---\nstatus: agent\ntags: task\n---\n\nbody\n")

        self.board.write_frontmatter_field(str(path), "session-id", "abc-123")
        fm, _, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        self.assertEqual(fm.get("session-id"), "abc-123")

        self.board.write_frontmatter_field(str(path), "session-id", "def-456")
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count("session-id:"), 1)  # replaced, not duplicated
        fm, _, _ = split_frontmatter(text)
        self.assertEqual(fm.get("session-id"), "def-456")


class ResultSectionTest(VaultBoardTestBase):
    def test_append_is_idempotent(self):
        path = self.write_note("task.md", "---\nstatus: review\ntags: task\n---\n\nbody\n")

        self.board.append_result_section(str(path), "First run output.")
        self.board.append_result_section(str(path), "Second run output.")

        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count("## Result"), 1)  # replaced, not stacked
        self.assertIn("Second run output.", text)
        self.assertNotIn("First run output.", text)


class AtomicWriteTest(VaultBoardTestBase):
    def test_no_tmp_file_left_behind(self):
        path = self.write_note("task.md", GOLDEN_NOTE)
        self.board.set_status(str(path), "in-progress")
        self.board.append_result_section(str(path), "done")

        leftovers = list(self.tasks_dir.glob(".*.tmp")) + list(self.tasks_dir.glob("*.tmp"))
        self.assertEqual(leftovers, [])
        # And the note is whole, never a partial write.
        self.assertEqual(self.board.read_status(str(path)), "in-progress")


class UnicodeTest(unittest.TestCase):
    def test_cjk_vault_path_and_spaced_cjk_filename(self):
        with TemporaryDirectory() as raw:
            vault = Path(raw) / "花园vault"
            tasks = vault / "inbox" / "tasks"
            tasks.mkdir(parents=True)
            (tasks / "我的 任务.md").write_text(
                "---\nstatus: agent\ntags: task\n---\n\n身体\n", encoding="utf-8"
            )
            board = VaultBoard(str(vault))

            items = board.fetch_items()

            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["note_path"], "inbox/tasks/我的 任务.md")
            self.assertRegex(items[0]["item_id"], r"^[0-9a-f]{12}$")  # ASCII hex id


class GitCommitTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.vault = Path(self.temp.name)
        self.tasks_dir = self.vault / "inbox" / "tasks"
        self.tasks_dir.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def _git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.vault), *args],
            capture_output=True, text=True, check=True,
        ).stdout

    def test_commits_exactly_the_one_file(self):
        self._git("init", "-q")
        self._git("config", "user.email", "t@t.test")
        self._git("config", "user.name", "Test")
        note = self.tasks_dir / "task.md"
        note.write_text("---\nstatus: review\ntags: task\n---\n\nbody\n", encoding="utf-8")
        # A second, unrelated dirty file must be left out of the commit.
        (self.vault / "other.md").write_text("untracked\n", encoding="utf-8")

        board = VaultBoard(str(self.vault))
        board.git_commit(str(note), "agent(task): review")

        log = self._git("log", "--oneline")
        self.assertEqual(len(log.strip().splitlines()), 1)
        changed = self._git("show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(changed, ["inbox/tasks/task.md"])
        # The unrelated file stayed untracked.
        self.assertIn("other.md", self._git("status", "--porcelain"))

    def test_non_git_dir_is_a_noop_that_does_not_raise(self):
        note = self.tasks_dir / "task.md"
        note.write_text("---\nstatus: review\ntags: task\n---\n\nbody\n", encoding="utf-8")
        board = VaultBoard(str(self.vault))

        board.git_commit(str(note), "no repo here")  # must not raise

    def test_commit_results_false_never_calls_git(self):
        note = self.tasks_dir / "task.md"
        note.write_text("---\nstatus: review\ntags: task\n---\n\nbody\n", encoding="utf-8")
        board = VaultBoard(str(self.vault), commit_results=False)

        with mock.patch("eastwatch.vault.subprocess.run") as run:
            board.git_commit(str(note), "should be skipped")

        run.assert_not_called()


class HelperTest(unittest.TestCase):
    def test_wikilink_targets_strips_alias_and_passes_bare_strings(self):
        self.assertEqual(wikilink_targets("[[inbox/tasks/foo|foo]]"), ["inbox/tasks/foo"])
        self.assertEqual(wikilink_targets(["[[a|x]]", "bare"]), ["a", "bare"])
        self.assertEqual(wikilink_targets(None), [])

    def test_split_frontmatter_on_note_without_frontmatter(self):
        fm, inner, span = split_frontmatter("no frontmatter here\n")
        self.assertEqual(fm, {})
        self.assertIsNone(span)


if __name__ == "__main__":
    unittest.main()
