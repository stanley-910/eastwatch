import atexit
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.support import load_watcher

TMP_ROOT = Path(tempfile.mkdtemp(prefix="eastwatch-pi-test-"))
watcher = load_watcher(TMP_ROOT)

atexit.register(lambda: shutil.rmtree(TMP_ROOT, ignore_errors=True))


class PiParallelismTest(unittest.TestCase):
    def setUp(self):
        watcher.STATE_DIR.mkdir(parents=True, exist_ok=True)

    def make_issue_conv(self, kind: str = "agent::ready") -> dict:
        return {
            "anchor": "issue",
            "kind": kind,
            "issue_url": "https://gitlab.example.com/group/repo/-/issues/103",
            "issue_title": "Player card",
            "issue_iid": "103",
            "project_path": "group/repo",
            "host": "gitlab.example.com",
            "checkout": None,
        }

    def test_pi_prompt_arg_neutralizes_leading_at_file_token(self):
        self.assertEqual(watcher.pi_prompt_arg("@agent retry"), " @agent retry")
        self.assertEqual(
            watcher.pi_prompt_arg("quoted @agent retry"), "quoted @agent retry"
        )

    def test_prepare_issue_workspace_uses_forge_result_as_worker_cwd(self):
        checkout = TMP_ROOT / "forge-checkout"
        worktree = TMP_ROOT / "forge-worktree"
        command = TMP_ROOT / "glab-board"
        checkout.mkdir(exist_ok=True)
        worktree.mkdir(exist_ok=True)
        command.write_text("#!/bin/sh\n")
        conv = {
            "anchor": "issue",
            "kind": "agent::ready",
            "checkout": str(checkout),
            "cwd": str(checkout),
            "host": "gitlab.example.com",
            "issue_iid": "103",
        }
        completed = mock.Mock(
            returncode=0,
            stdout=(
                '{"issue":103,"worktree":"'
                + str(worktree)
                + '","branch":"issue-103-player-card","base":"origin/main"}\n'
            ),
            stderr="grabbed #103",
        )
        with (
            mock.patch.object(watcher.subprocess, "run", return_value=completed) as run,
            mock.patch.object(watcher, "worker_env", return_value={}),
            mock.patch.dict(os.environ, {"EASTWATCH_GLAB_BOARD": str(command)}),
        ):
            self.assertTrue(watcher.prepare_issue_workspace(conv))

        self.assertEqual(conv["cwd"], str(worktree))
        self.assertEqual(conv["worktree_branch"], "issue-103-player-card")
        self.assertTrue(conv["workspace_prepared"])
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][1:], ["start", "103", "work", "--json"])
        self.assertEqual(run.call_args.kwargs["cwd"], str(checkout))

    def test_prepared_workspace_prompt_uses_single_finish_command(self):
        conv = {
            "checkout": "/repo",
            "cwd": "/worktree",
            "workspace_prepared": True,
            "worktree_branch": "issue-103-player-card",
            "issue_iid": "103",
            "kind": "agent::ready",
        }
        prompt = watcher.workspace_prompt(conv)
        self.assertIn("Forge onboarding is complete", prompt)
        self.assertIn("finish 103", prompt)
        self.assertIn("--description-file <path>", prompt)
        self.assertIn("derive the body from commits", prompt)
        self.assertNotIn("create a git worktree", prompt)

    def test_prepared_research_workspace_does_not_require_finish_mr(self):
        conv = {
            "checkout": "/repo",
            "cwd": "/worktree",
            "workspace_prepared": True,
            "worktree_branch": "issue-104-research",
            "issue_iid": "104",
            "kind": "agent::ready-research",
        }
        prompt = watcher.workspace_prompt(conv)
        self.assertIn("research deliverable", prompt)
        self.assertNotIn("finish 104", prompt)

    def test_launch_prompt_reserves_manual_marker_instruction_for_forge_bypass(self):
        prompt = watcher.build_launch_prompt(self.make_issue_conv(), [])
        self.assertIn("bypass `glab-board finish`", prompt)
        self.assertIn("source_issue_iid=103", prompt)

    def test_work_launch_prompt_includes_common_and_work_charters(self):
        prompt = watcher.build_launch_prompt(self.make_issue_conv(), [])

        self.assertIn(watcher.CHARTER_COMMON, prompt)
        self.assertIn(watcher.CHARTER_WORK, prompt)
        self.assertNotIn(watcher.CHARTER_RESEARCH, prompt)
        self.assertLess(
            prompt.index("source_issue_iid=103"), prompt.index(watcher.CHARTER_COMMON)
        )

    def test_research_launch_prompt_includes_common_and_research_charters(self):
        prompt = watcher.build_launch_prompt(
            self.make_issue_conv("agent::ready-research"), []
        )

        self.assertIn(watcher.CHARTER_COMMON, prompt)
        self.assertIn(watcher.CHARTER_RESEARCH, prompt)
        self.assertNotIn(watcher.CHARTER_WORK, prompt)

    def test_mr_launch_prompt_excludes_autonomy_charters(self):
        conv = {
            "anchor": "mr",
            "kind": "mr-question",
            "mr_url": "https://gitlab.example.com/group/repo/-/merge_requests/9",
            "mr_iid": "9",
            "mr_title": "Player card",
            "checkout": None,
        }
        prompt = watcher.build_launch_prompt(
            conv, ["Does this handle missing avatars?"]
        )

        self.assertNotIn(watcher.CHARTER_COMMON, prompt)
        self.assertNotIn(watcher.CHARTER_WORK, prompt)
        self.assertNotIn(watcher.CHARTER_RESEARCH, prompt)

    def test_qa_launch_prompt_excludes_autonomy_charters(self):
        prompt = watcher.build_launch_prompt(
            self.make_issue_conv("qa"), ["What changed?"]
        )

        self.assertNotIn(watcher.CHARTER_COMMON, prompt)
        self.assertNotIn(watcher.CHARTER_WORK, prompt)
        self.assertNotIn(watcher.CHARTER_RESEARCH, prompt)

    def make_req(
        self, run_dir: Path, *, is_new: bool = True, sid: str | None = None
    ) -> dict:
        run_dir.mkdir(parents=True, exist_ok=True)
        req = {
            "cwd": str(run_dir),
            "is_new": is_new,
            "session_dir": str(run_dir),
            "session_file": None,
            "child_pid_path": str(run_dir / "child.pid"),
            "stdout_path": str(run_dir / "stdout.log"),
            "stderr_path": str(run_dir / "stderr.log"),
        }
        if sid:
            req["planned_session_id"] = sid
        return req

    def test_run_timeout_allows_three_hour_agent_runs(self):
        req = watcher.make_run_request(
            {"provider": "pi", "model": "gpt", "cwd": str(TMP_ROOT)},
            [],
            False,
            "continue",
            TMP_ROOT / "three-hour-timeout",
            "run-1",
        )

        self.assertEqual(watcher.RUN_TIMEOUT_SECONDS, 10_800)
        self.assertEqual(req["timeout_seconds"], 10_800)

    def test_dispatch_allows_multiple_pi_runs_up_to_global_cap(self):
        ps = {
            "conversations": {
                "1": {"provider": "pi", "pending": ["one"], "status": "new"},
                "2": {"provider": "pi", "pending": ["two"], "status": "new"},
            }
        }
        state = {"projects": {"gitlab.example.com/group/project": ps}}
        contexts = [{"proj": {"path": "group/project"}, "ps": ps, "gl": object()}]
        started_keys = []
        original_start_one = watcher.start_one

        def fake_start_one(_gl, _proj, project_state, conv_key, _state):
            started_keys.append(conv_key)
            project_state["conversations"][conv_key]["current_run"] = {"provider": "pi"}
            return True

        watcher.start_one = fake_start_one
        try:
            watcher.dispatch_pending({"concurrency_cap": 2}, state, contexts)
        finally:
            watcher.start_one = original_start_one

        self.assertEqual(started_keys, ["1", "2"])

    def test_start_clears_prior_state_labels_when_marking_issue_working(self):
        session_dir = TMP_ROOT / "label-lifecycle"
        conv = {
            "provider": "pi",
            "model": "gpt",
            "effort": "low",
            "session_id": None,
            "session_file": str(session_dir / "existing.jsonl"),
            "session_dir": str(session_dir),
            "cwd": str(TMP_ROOT),
            "host": "gitlab.example.com",
            "project_path": "group/project",
            "reply_target": None,
            "status": "new",
            "kind": "agent::ready",
            "issue_iid": "42",
            "pending": ["retry"],
        }
        ps = {"conversations": {"42": conv}}
        state = {"projects": {"gitlab.example.com/group/project": ps}}
        gl = object()
        project = {"id": 1, "path": "group/project"}
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with (
            mock.patch.object(watcher, "set_issue_labels") as set_issue_labels,
            mock.patch.object(watcher, "save_state"),
            mock.patch.object(
                watcher, "tmux_bin", return_value="/opt/homebrew/bin/tmux"
            ),
            mock.patch.object(watcher, "tmux_launch_worker", return_value=completed),
        ):
            self.assertTrue(watcher.start_one(gl, project, ps, "42", state))

        set_issue_labels.assert_called_once_with(
            gl,
            project,
            "42",
            add=[watcher.WORKING_LABEL],
            remove=[
                label
                for label in watcher.SHADOW_LABELS
                if label != watcher.WORKING_LABEL
            ],
        )

    def test_project_polling_fetches_projects_in_parallel_and_commits_serially(self):
        cfg = {
            "keychain": {"service": "svc", "account": "acct"},
            "owner": "stanwang",
            "defaults": {},
            "projects": [
                {
                    "host": "gitlab.example.com",
                    "path": "group/one",
                    "id": 1,
                    "triggers": [],
                },
                {
                    "host": "gitlab.example.com",
                    "path": "group/two",
                    "id": 2,
                    "triggers": [],
                },
            ],
        }
        state = {"projects": {}}
        started = []
        parallel_observed = []
        commit_order = []
        commit_threads = []
        calling_thread = threading.get_ident()
        all_started = threading.Event()
        lock = threading.Lock()

        originals = {
            "GitLab": watcher.GitLab,
            "keychain_token": watcher.keychain_token,
            "collect_and_heal_runs": watcher.collect_and_heal_runs,
            "poll_comments": watcher.poll_comments,
            "assemble": watcher.assemble,
            "save_state": watcher.save_state,
        }

        class FakeGitLab:
            def __init__(self, host, token):
                self.host = host
                self.token = token

        def fake_poll_comments(_gl, proj, ps, _owner):
            with lock:
                started.append(proj["path"])
                if len(started) == 2:
                    all_started.set()
            if all_started.wait(2):
                with lock:
                    parallel_observed.append(proj["path"])
            ps["last_event_id"] = proj["id"] * 10
            return []

        def fake_assemble(_gl, proj, ps, *_args):
            commit_order.append(proj["path"])
            commit_threads.append(threading.get_ident())
            self.assertEqual(ps["last_event_id"], proj["id"] * 10)

        watcher.GitLab = FakeGitLab
        watcher.keychain_token = lambda _service, _account: "token"
        watcher.collect_and_heal_runs = lambda *_args: None
        watcher.poll_comments = fake_poll_comments
        watcher.assemble = fake_assemble
        watcher.save_state = lambda _state: None
        try:
            contexts = watcher.reconcile_projects(cfg, state)
        finally:
            for name, value in originals.items():
                setattr(watcher, name, value)

        self.assertEqual(
            [ctx["key"] for ctx in contexts],
            [
                "gitlab.example.com/group/one",
                "gitlab.example.com/group/two",
            ],
        )
        self.assertCountEqual(started, ["group/one", "group/two"])
        self.assertCountEqual(parallel_observed, ["group/one", "group/two"])
        self.assertCountEqual(commit_order, ["group/one", "group/two"])
        self.assertEqual(set(commit_threads), {calling_thread})

    def test_fast_project_commits_while_slow_project_poll_is_running(self):
        cfg = {
            "keychain": {"service": "svc", "account": "acct"},
            "owner": "stanwang",
            "defaults": {},
            "projects": [
                {
                    "host": "gitlab.example.com",
                    "path": "group/slow",
                    "id": 1,
                    "triggers": [],
                },
                {
                    "host": "gitlab.example.com",
                    "path": "group/fast",
                    "id": 2,
                    "triggers": [],
                },
            ],
        }
        state = {"projects": {}}
        slow_started = threading.Event()
        release_slow = threading.Event()
        fast_committed = threading.Event()
        contexts = []
        errors = []
        originals = {
            "GitLab": watcher.GitLab,
            "keychain_token": watcher.keychain_token,
            "collect_and_heal_runs": watcher.collect_and_heal_runs,
            "poll_comments": watcher.poll_comments,
            "assemble": watcher.assemble,
            "save_state": watcher.save_state,
        }

        class FakeGitLab:
            def __init__(self, _host, _token):
                pass

        def fake_poll_comments(_gl, proj, _ps, _owner):
            if proj["path"] == "group/slow":
                slow_started.set()
                if not release_slow.wait(3):
                    raise RuntimeError("slow poll was not released")
            return []

        def fake_assemble(_gl, proj, *_args):
            if proj["path"] == "group/fast":
                fast_committed.set()

        def run_reconcile():
            try:
                contexts.extend(watcher.reconcile_projects(cfg, state))
            except Exception as exc:  # noqa: BLE001 — surfaced on the test thread
                errors.append(exc)

        watcher.GitLab = FakeGitLab
        watcher.keychain_token = lambda _service, _account: "token"
        watcher.collect_and_heal_runs = lambda *_args: None
        watcher.poll_comments = fake_poll_comments
        watcher.assemble = fake_assemble
        watcher.save_state = lambda _state: None
        thread = threading.Thread(target=run_reconcile)
        try:
            thread.start()
            self.assertTrue(slow_started.wait(1), "slow project poll did not start")
            self.assertTrue(
                fast_committed.wait(1),
                "fast project did not commit while the slow poll was still running",
            )
        finally:
            release_slow.set()
            thread.join(timeout=5)
            for name, value in originals.items():
                setattr(watcher, name, value)

        if errors:
            raise errors[0]
        self.assertFalse(thread.is_alive(), "reconcile thread did not finish")
        self.assertEqual(
            [ctx["key"] for ctx in contexts],
            ["gitlab.example.com/group/slow", "gitlab.example.com/group/fast"],
        )

    def test_project_poll_workers_must_be_a_positive_integer(self):
        for value in (0, -1, True, False, 1.5, "2"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    watcher.ConfigurationError,
                    "project_poll_workers must be a positive integer",
                ):
                    watcher.configured_project_poll_workers(
                        {"project_poll_workers": value}
                    )

        self.assertIsNone(watcher.configured_project_poll_workers({}))
        self.assertEqual(
            watcher.project_poll_worker_count({"project_poll_workers": 2}, 4), 2
        )
        self.assertEqual(
            watcher.project_poll_worker_count({"project_poll_workers": 8}, 4), 4
        )

    def test_failed_assembly_does_not_advance_live_poll_state(self):
        cfg = {
            "keychain": {"service": "svc", "account": "acct"},
            "owner": "stanwang",
            "defaults": {},
            "projects": [
                {
                    "host": "gitlab.example.com",
                    "path": "group/one",
                    "id": 1,
                    "triggers": [],
                },
            ],
        }
        state = {"projects": {}}
        saved_states = []
        originals = {
            "GitLab": watcher.GitLab,
            "keychain_token": watcher.keychain_token,
            "collect_and_heal_runs": watcher.collect_and_heal_runs,
            "poll_comments": watcher.poll_comments,
            "assemble": watcher.assemble,
            "save_state": watcher.save_state,
        }

        class FakeGitLab:
            def __init__(self, _host, _token):
                pass

        def fake_poll_comments(_gl, _proj, ps, _owner):
            ps["last_event_id"] = 42
            return [{"kind": "issue", "iid": "1", "body": "work"}]

        def fail_assemble(*_args):
            raise RuntimeError("assemble failed")

        watcher.GitLab = FakeGitLab
        watcher.keychain_token = lambda _service, _account: "token"
        watcher.collect_and_heal_runs = lambda *_args: None
        watcher.poll_comments = fake_poll_comments
        watcher.assemble = fail_assemble
        watcher.save_state = lambda value: saved_states.append(value)
        try:
            contexts = watcher.reconcile_projects(cfg, state)
        finally:
            for name, value in originals.items():
                setattr(watcher, name, value)

        ps = state["projects"]["gitlab.example.com/group/one"]
        self.assertEqual(contexts, [])
        self.assertEqual(ps["last_event_id"], 0)
        self.assertEqual(ps["conversations"], {})
        self.assertEqual(saved_states, [])

    def test_duplicate_project_keys_are_rejected_before_polling(self):
        cfg = {
            "projects": [
                {"host": "gitlab.example.com", "path": "group/one"},
                {"host": "gitlab.example.com", "path": "group/one"},
            ],
        }
        state = {"projects": {}}

        with self.assertRaisesRegex(
            watcher.ConfigurationError,
            r"duplicate project config for gitlab\.example\.com/group/one",
        ):
            watcher.reconcile_projects(cfg, state)

        self.assertEqual(state, {"projects": {}})

    def test_recovered_session_fallback_ignores_stale_file_during_launch_window(self):
        first_dir = TMP_ROOT / "recovered-fallback"
        second_dir = TMP_ROOT / "after-recovered-fallback"
        sid = "recovered-session"
        recovered_session = first_dir / f"conversation_{sid}.jsonl"
        first_started = first_dir / "started"
        first_req = self.make_req(first_dir, sid=sid)
        second_req = self.make_req(second_dir, sid="second-session")
        recovered_session.write_text("stale recovered session")
        first_errors = []

        first_cmd = [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import sys, time; "
                "Path(sys.argv[-1]).write_text('started'); "
                "time.sleep(0.7); "
                "print('direct auth passed', flush=True); "
                "time.sleep(0.5)"
            ),
            "--session",
            str(recovered_session),
            str(first_started),
        ]
        second_cmd = [sys.executable, "-c", "print('second done', flush=True)"]

        def run_first():
            try:
                watcher.run_pi_provider_command(first_req, first_cmd, 5, sid)
            except Exception as exc:  # noqa: BLE001 — re-raised in test thread
                first_errors.append(exc)

        thread = threading.Thread(target=run_first)
        thread.start()
        deadline = time.time() + 2
        while not first_started.exists() and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(
            first_started.exists(), "recovered-session child did not launch"
        )

        started = time.time()
        code, stdout, stderr = watcher.run_pi_provider_command(
            second_req, second_cmd, 5, "second-session"
        )
        elapsed = time.time() - started
        thread.join(timeout=3)

        if first_errors:
            raise first_errors[0]
        self.assertFalse(thread.is_alive(), "recovered-session command did not finish")
        self.assertEqual(code, 0)
        self.assertIn("second done", stdout)
        self.assertEqual(stderr, "")
        self.assertGreaterEqual(elapsed, 0.4)

    def test_pi_lock_releases_after_launch_window_not_process_exit(self):
        first_dir = TMP_ROOT / "first"
        second_dir = TMP_ROOT / "second"
        sid = "first-session"
        first_session = first_dir / f"conversation_{sid}.jsonl"
        first_started = first_dir / "started"
        first_req = self.make_req(first_dir, sid=sid)
        second_req = self.make_req(second_dir, sid="second-session")
        first_errors = []

        first_cmd = [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import sys, time; "
                "Path(sys.argv[1]).write_text('session'); "
                "Path(sys.argv[2]).write_text('started'); "
                "print('first launched', flush=True); "
                "time.sleep(1.5); "
                "print('first done', flush=True)"
            ),
            str(first_session),
            str(first_started),
        ]
        second_cmd = [sys.executable, "-c", "print('second done', flush=True)"]

        def run_first():
            try:
                watcher.run_pi_provider_command(first_req, first_cmd, 5, sid)
            except Exception as exc:  # noqa: BLE001 — re-raised in test thread
                first_errors.append(exc)

        thread = threading.Thread(target=run_first)
        thread.start()
        deadline = time.time() + 2
        while not first_started.exists() and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(first_started.exists(), "first child did not launch")

        started = time.time()
        code, stdout, stderr = watcher.run_pi_provider_command(
            second_req, second_cmd, 5, "second-session"
        )
        elapsed = time.time() - started
        thread.join(timeout=3)

        if first_errors:
            raise first_errors[0]
        self.assertFalse(thread.is_alive(), "first command did not finish")
        self.assertEqual(code, 0)
        self.assertIn("second done", stdout)
        self.assertEqual(stderr, "")
        self.assertLess(elapsed, 1.0)


if __name__ == "__main__":
    unittest.main()
