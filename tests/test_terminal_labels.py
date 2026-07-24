import atexit
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.support import load_watcher

TMP_ROOT = Path(tempfile.mkdtemp(prefix="eastwatch-terminal-labels-test-"))
watcher = load_watcher(TMP_ROOT)

atexit.register(lambda: shutil.rmtree(TMP_ROOT, ignore_errors=True))

PROJ = {"id": 1, "host": "gitlab.example.com", "path": "group/project"}


class FakeForge:
    """Apply the same label request with or without GitLab scoped-label swaps."""

    def __init__(self, scoped: bool):
        self.scoped = scoped
        self.labels = set(watcher.SHADOW_LABELS) | {"category::feature"}
        self.puts = []
        self.next_note_id = 100

    def put(self, path, **data):
        self.puts.append((path, data))
        self.labels.difference_update(filter(None, data.get("remove_labels", "").split(",")))
        for label in filter(None, data.get("add_labels", "").split(",")):
            if self.scoped and label.startswith("agent::"):
                self.labels = {existing for existing in self.labels if not existing.startswith("agent::")}
            self.labels.add(label)
        return {}

    def post(self, _path, **_data):
        self.next_note_id += 1
        return {"id": self.next_note_id}


class TerminalLabelTest(unittest.TestCase):
    def make_conversation(self, *, kind="agent::ready"):
        return {
            "provider": "pi",
            "model": "gpt",
            "effort": "low",
            "session_file": str(TMP_ROOT / "session.jsonl"),
            "kind": kind,
            "issue_iid": "41",
            "pending": [],
            "status": "working",
            "current_run": {"run_id": "run-1"},
            "reply_target": {"kind": "issue", "issue_iid": "41"},
        }

    def assert_agent_label(self, forge, expected):
        self.assertEqual(forge.labels & set(watcher.SHADOW_LABELS), {expected})
        self.assertIn("category::feature", forge.labels)

    def collect_done(self, forge, *, with_mr):
        conv = self.make_conversation()
        mr_index = {"27": {"conversation_key": "41", "issue_iid": "41"}} if with_mr else {}
        ps = {"conversations": {"41": conv}, "mr_index": mr_index}
        state = {"projects": {"gitlab.example.com/group/project": ps}}
        with mock.patch.object(watcher, "save_state"):
            watcher.collect_success(
                forge,
                PROJ,
                ps,
                "41",
                conv,
                {"reply": "Run complete.\n\nSTATUS: done", "completed_at": 1},
                state,
            )
        return conv

    def test_done_with_mr_moves_to_mr_ready(self):
        for scoped in (True, False):
            with self.subTest(scoped_labels=scoped):
                forge = FakeForge(scoped)
                conv = self.collect_done(forge, with_mr=True)
                self.assertEqual(conv["status"], "done")
                self.assert_agent_label(forge, watcher.MR_READY_LABEL)

    def test_done_without_mr_moves_to_for_human(self):
        for scoped in (True, False):
            with self.subTest(scoped_labels=scoped):
                forge = FakeForge(scoped)
                conv = self.collect_done(forge, with_mr=False)
                self.assertEqual(conv["status"], "done")
                self.assert_agent_label(forge, watcher.FOR_HUMAN_LABEL)

    def test_wrapper_failure_moves_to_failed(self):
        for scoped in (True, False):
            with self.subTest(scoped_labels=scoped):
                forge = FakeForge(scoped)
                conv = self.make_conversation()
                watcher.mark_failed(forge, PROJ, "41", conv, "timeout")
                self.assertEqual(conv["status"], "failed")
                self.assert_agent_label(forge, watcher.FAILED_LABEL)

    def test_corrupt_result_artifact_moves_to_failed(self):
        forge = FakeForge(scoped=False)
        conv = self.make_conversation()
        run_dir = TMP_ROOT / "corrupt-result"
        run_dir.mkdir(exist_ok=True)
        result_path = run_dir / "result.json"
        result_path.write_text("{")
        run = conv["current_run"] | {
            "result_path": str(result_path),
            "error_path": str(run_dir / "error.json"),
        }
        conv["current_run"] = run
        ps = {"conversations": {"41": conv}, "mr_index": {}}

        self.assertTrue(
            watcher.collect_terminal_artifact(
                forge,
                PROJ,
                ps,
                {"projects": {}},
                "41",
                conv,
                run,
            )
        )
        self.assert_agent_label(forge, watcher.FAILED_LABEL)

    def test_research_dispatch_moves_to_researching_and_clears_triage(self):
        for scoped in (True, False):
            with self.subTest(scoped_labels=scoped):
                forge = FakeForge(scoped)
                session_dir = TMP_ROOT / f"research-{scoped}"
                conv = self.make_conversation(kind="agent::ready-research") | {
                    "session_dir": str(session_dir),
                    "cwd": str(TMP_ROOT),
                    "host": "gitlab.example.com",
                    "project_path": "group/project",
                    "session_id": None,
                    "session_file": str(session_dir / "existing.jsonl"),
                    "pending": ["research this"],
                    "status": "new",
                    "current_run": None,
                }
                ps = {"conversations": {"41": conv}}
                state = {"projects": {"gitlab.example.com/group/project": ps}}
                with (
                    mock.patch.object(watcher, "save_state"),
                    mock.patch.object(watcher, "tmux_bin", return_value="/opt/homebrew/bin/tmux"),
                    mock.patch.object(
                        watcher,
                        "tmux_launch_worker",
                        return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
                    ) as launch,
                ):
                    self.assertTrue(watcher.start_one(forge, PROJ, ps, "41", state))

                worker_argv = launch.call_args.args[2]
                self.assertEqual(worker_argv[1], str(watcher.REPOSITORY_ROOT / "eastwatch"))
                self.assertEqual(worker_argv[-2], "--worker")
                self.assert_agent_label(forge, watcher.RESEARCHING_LABEL)


if __name__ == "__main__":
    unittest.main()
