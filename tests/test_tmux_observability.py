"""Tests for tmux-hosted workers, streaming, and the fleet-status emitter."""

import atexit
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from eastwatch import sessions
from eastwatch.collector import ClaudeStreamCollector, PiStreamCollector, RunJournal
from eastwatch.fleet import status as fleet_status
from tests.support import load_watcher

TMP_ROOT = Path(tempfile.mkdtemp(prefix="eastwatch-tmux-test-"))
watcher = load_watcher(TMP_ROOT)

atexit.register(lambda: shutil.rmtree(TMP_ROOT, ignore_errors=True))


class SessionNameTest(unittest.TestCase):
    def test_derived_from_session_dir_basename(self):
        conv = {"session_dir": "/x/convos/example-org-example-repo-73"}
        self.assertEqual(
            watcher.tmux_session_name(conv), "task-example-org-example-repo-73"
        )

    def test_unsafe_chars_folded(self):
        conv = {"session_dir": "/x/convos/grp-repo-mr-5-note:99.beta"}
        name = watcher.tmux_session_name(conv)
        self.assertEqual(name, "task-grp-repo-mr-5-note-99-beta")
        # tmux targets treat ':' and '.' specially — must not survive.
        self.assertNotIn(":", name)
        self.assertNotIn(".", name)

    def test_none_without_session_dir(self):
        self.assertIsNone(watcher.tmux_session_name({}))

    def test_exact_match_names_do_not_collide(self):
        a = watcher.tmux_session_name({"session_dir": "/c/repo-6"})
        b = watcher.tmux_session_name({"session_dir": "/c/repo-62"})
        self.assertNotEqual(a, b)


class HasSessionGuardTest(unittest.TestCase):
    def test_false_when_tmux_missing(self):
        orig = watcher.tmux_bin
        watcher.tmux_bin = lambda: None
        try:
            self.assertFalse(watcher.tmux_has_session("task-anything"))
        finally:
            watcher.tmux_bin = orig

    def test_false_on_empty_name(self):
        self.assertFalse(watcher.tmux_has_session(None))
        self.assertFalse(watcher.tmux_has_session(""))

    def test_retained_dead_pane_is_not_a_live_worker(self):
        completed = subprocess.CompletedProcess(
            ["tmux"],
            0,
            stdout="1\n",
            stderr="",
        )
        with (
            mock.patch.object(watcher, "tmux_bin", return_value="/usr/bin/tmux"),
            mock.patch.object(watcher.subprocess, "run", return_value=completed),
        ):
            self.assertFalse(watcher.tmux_has_session("task-x"))

        completed.stdout = "1\n0\n"
        with (
            mock.patch.object(watcher, "tmux_bin", return_value="/usr/bin/tmux"),
            mock.patch.object(watcher.subprocess, "run", return_value=completed),
        ):
            self.assertTrue(watcher.tmux_has_session("task-x"))


class TmuxLaunchTest(unittest.TestCase):
    def test_worker_pane_enables_remain_on_exit_before_exec(self):
        completed = subprocess.CompletedProcess(["tmux"], 0, stdout="", stderr="")
        with (
            mock.patch.object(watcher, "tmux_bin", return_value="/usr/bin/tmux"),
            mock.patch.object(watcher, "tmux_kill_session"),
            mock.patch.object(watcher.subprocess, "run", return_value=completed) as run,
        ):
            watcher.tmux_launch_worker(
                "task-x",
                "/tmp",
                ["python", "watcher.py", "--worker", "/tmp/request.json"],
                {},
            )

        command = run.call_args.args[0]
        self.assertEqual(
            command[:6], ["/usr/bin/tmux", "new-session", "-d", "-s", "task-x", "-c"]
        )
        self.assertIn('set-option -pt "$TMUX_PANE" remain-on-exit on', command[-1])
        self.assertIn('set-option -pt "$TMUX_PANE" @agent_worktree /tmp', command[-1])
        self.assertIn("exec python watcher.py --worker /tmp/request.json", command[-1])

    def test_explicit_pi_config_paths_are_passed_to_the_tmux_session(self):
        completed = subprocess.CompletedProcess(["tmux"], 0, stdout="", stderr="")
        env = {
            "PATH": "/operator/bin",
            "GITLAB_HOST": "git.example.com",
            "XDG_CONFIG_HOME": "/tmp/operator config;$(touch unsafe)",
            "PI_CODING_AGENT_DIR": "/tmp/pi agent && echo unsafe",
        }
        with (
            mock.patch.object(watcher, "tmux_bin", return_value="/usr/bin/tmux"),
            mock.patch.object(watcher, "tmux_kill_session"),
            mock.patch.object(watcher.subprocess, "run", return_value=completed) as run,
        ):
            watcher.tmux_launch_worker(
                "task-x",
                "/tmp/worktree",
                ["python", "watcher.py", "--worker", "/tmp/request.json"],
                env,
            )

        command = run.call_args.args[0]
        self.assertEqual(
            command[:-1],
            [
                "/usr/bin/tmux",
                "new-session",
                "-d",
                "-s",
                "task-x",
                "-c",
                "/tmp/worktree",
                "-e",
                "XDG_CONFIG_HOME=/tmp/operator config;$(touch unsafe)",
                "-e",
                "PI_CODING_AGENT_DIR=/tmp/pi agent && echo unsafe",
            ],
        )
        self.assertIs(run.call_args.kwargs["env"], env)
        self.assertNotIn(f"PATH={env['PATH']}", command)
        self.assertNotIn(f"GITLAB_HOST={env['GITLAB_HOST']}", command)

    def test_login_shell_restores_explicit_pi_config_paths_after_zshenv(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            zdotdir = root / "zdotdir"
            cwd = root / "worker cwd"
            home.mkdir()
            zdotdir.mkdir()
            cwd.mkdir()
            (zdotdir / ".zshenv").write_text(
                'export XDG_CONFIG_HOME="$HOME/.config-from-zshenv"\n'
                'export PI_CODING_AGENT_DIR="$HOME/.config-from-zshenv/pi/agent"\n'
            )

            fake_tmux = root / "fake tmux"
            fake_tmux.write_text("#!/bin/sh\nexit 0\n")
            fake_tmux.chmod(0o755)

            output = root / "worker output.json"
            worker = root / "capture env.py"
            worker.write_text(
                "import json, os, pathlib, sys\n"
                "pathlib.Path(sys.argv[1]).write_text(json.dumps({\n"
                '    "XDG_CONFIG_HOME": os.environ.get("XDG_CONFIG_HOME"),\n'
                '    "PI_CODING_AGENT_DIR": os.environ.get("PI_CODING_AGENT_DIR"),\n'
                "}))\n"
            )

            marker = root / "unsafe-marker"
            explicit_xdg = f"{root}/operator config;$(touch {marker})"
            explicit_pi = f"{root}/pi agent && touch {marker}"
            env = {
                "HOME": str(home),
                "PATH": os.environ["PATH"],
                "ZDOTDIR": str(zdotdir),
                "TMUX_PANE": "%1",
                "XDG_CONFIG_HOME": explicit_xdg,
                "PI_CODING_AGENT_DIR": explicit_pi,
            }
            completed = subprocess.CompletedProcess(
                [str(fake_tmux)], 0, stdout="", stderr=""
            )
            with (
                mock.patch.object(watcher, "tmux_bin", return_value=str(fake_tmux)),
                mock.patch.object(watcher, "tmux_kill_session"),
                mock.patch.object(
                    watcher.subprocess, "run", return_value=completed
                ) as run,
            ):
                watcher.tmux_launch_worker(
                    "task-x",
                    str(cwd),
                    [sys.executable, str(worker), str(output)],
                    env,
                )

            login_command = run.call_args.args[0][-1]
            result = subprocess.run(
                ["/bin/sh", "-c", login_command],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            observed = json.loads(output.read_text())
            self.assertEqual(observed["XDG_CONFIG_HOME"], explicit_xdg)
            self.assertEqual(observed["PI_CODING_AGENT_DIR"], explicit_pi)
            self.assertFalse(marker.exists())


class ArchiveRunTest(unittest.TestCase):
    def test_archives_copy_before_current_run_is_cleared(self):
        conv = {"current_run": {"run_id": "r1", "stream_path": "/tmp/stream.log"}}
        watcher.archive_current_run(conv)
        conv["current_run"]["run_id"] = "mutated"
        self.assertEqual(conv["last_run"]["run_id"], "r1")

    def test_success_archives_without_killing_retained_tmux(self):
        conv = {
            "status": "working",
            "anchor": "issue",
            "current_run": {
                "run_id": "r1",
                "tmux_session": "task-x",
                "reply_target": None,
            },
        }
        with (
            mock.patch.object(watcher, "tmux_kill_session") as kill,
            mock.patch.object(watcher, "label_issue_iid", return_value=None),
            mock.patch.object(watcher, "save_state"),
            mock.patch.object(
                watcher,
                "post_conversation_note",
                return_value={"id": 42},
            ),
            mock.patch.object(watcher, "resume_footer", return_value=""),
        ):
            watcher.collect_success(
                object(),
                {},
                {},
                "62",
                conv,
                {"reply": "done\nSTATUS: done", "completed_at": 456.0},
                {"projects": {}},
            )

        kill.assert_not_called()
        self.assertEqual(conv["status"], "done")
        self.assertIsNone(conv["current_run"])
        self.assertEqual(conv["last_run"]["tmux_session"], "task-x")
        self.assertEqual(conv["last_run"]["completed_at"], 456.0)


class ReplyEvidenceTest(unittest.TestCase):
    def test_extracts_mr_url_and_common_commit_labels(self):
        for label in ("Commit", "commit hash", "commit SHA", "SHA"):
            with self.subTest(label=label):
                evidence = watcher.reply_evidence(
                    f"MR: https://gitlab.example/group/repo/-/merge_requests/42\n"
                    f"{label}: `deadbeef`"
                )
                self.assertEqual(
                    evidence["mr_url"],
                    "https://gitlab.example/group/repo/-/merge_requests/42",
                )
                self.assertEqual(evidence["commit_sha"], "deadbeef")


class ClaudeStreamCollectorTest(unittest.TestCase):
    def collect(self, stdout):
        collector = ClaudeStreamCollector(RunJournal(None, "test"))
        collector.feed(stdout.encode())
        collector.finish_stdout()
        return collector

    def test_jsonl_last_result_event(self):
        lines = [
            {"type": "system", "subtype": "init", "session_id": "s1"},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "thinking"}]},
            },
            {
                "type": "result",
                "subtype": "success",
                "result": "hi",
                "session_id": "s1",
            },
        ]
        result = self.collect(
            "\n".join(json.dumps(line) for line in lines) + "\n"
        ).result()
        self.assertEqual(result["result"], "hi")
        self.assertEqual(result["session_id"], "s1")

    def test_blank_lines_ignored(self):
        collector = self.collect(
            '\n{"type":"result","result":"x","session_id":"s"}\n\n'
        )
        self.assertEqual(collector.result()["result"], "x")

    def test_no_result_raises_indexerror(self):
        with self.assertRaises(IndexError):
            self.collect('{"type":"assistant"}\n').result()

    def test_session_id_captured_from_opening_init_event(self):
        collector = self.collect(
            '{"type":"system","subtype":"init","session_id":"s1"}\n'
        )
        self.assertEqual(collector.session_id, "s1")

    def test_session_id_read_from_any_event_carrying_one(self):
        # The stream schema is internal, so discovery must not depend on
        # matching a particular event type.
        collector = self.collect('{"type":"unheard-of","session_id":"s9"}\n')
        self.assertEqual(collector.session_id, "s9")

    def test_session_id_tracks_a_mid_stream_change(self):
        collector = self.collect(
            '{"type":"system","subtype":"init","session_id":"s1"}\n'
            '{"type":"system","subtype":"init","session_id":"s2"}\n'
        )
        self.assertEqual(collector.session_id, "s2")

    def test_events_without_a_session_id_do_not_clear_it(self):
        collector = self.collect(
            '{"type":"system","subtype":"init","session_id":"s1"}\n'
            '{"type":"assistant","message":{"content":[]}}\n'
        )
        self.assertEqual(collector.session_id, "s1")

    def test_discover_session_journals_each_file_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal_path = Path(tmp) / "run.jsonl"
            collector = ClaudeStreamCollector(RunJournal(journal_path, "test"))
            collector.discover_session("/tmp/projects/slug/s1.jsonl")
            collector.discover_session("/tmp/projects/slug/s1.jsonl")
            collector.discover_session(None)
            facts = [
                json.loads(line)
                for line in journal_path.read_text().splitlines()
                if line.strip()
            ]
        self.assertEqual([fact["type"] for fact in facts], ["session_discovered"])
        self.assertEqual(facts[0]["session_file"], "/tmp/projects/slug/s1.jsonl")


class ClaudeSessionResolutionTest(unittest.TestCase):
    """find_claude_session_file globs the id rather than rebuilding the slug."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name)

    def write_transcript(self, slug, session_id, body='{"anything":true}\n'):
        path = self.config / "projects" / slug / f"{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path

    def resolve(self, session_id):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config)}):
            return sessions.find_claude_session_file(session_id)

    def test_resolves_under_any_project_slug(self):
        path = self.write_transcript("-Users-someone-Developer-repo", "s1")
        self.assertEqual(self.resolve("s1"), str(path))

    def test_contents_are_never_parsed(self):
        # Claude documents the transcript schema as internal, so resolution
        # must survive a body we do not recognise at all.
        path = self.write_transcript("-slug", "s2", body="not json at all\n")
        self.assertEqual(self.resolve("s2"), str(path))

    def test_missing_transcript_resolves_to_none(self):
        self.write_transcript("-slug", "s1")
        self.assertIsNone(self.resolve("s-absent"))

    def test_blank_session_id_resolves_to_none(self):
        self.assertIsNone(self.resolve(None))
        self.assertIsNone(self.resolve(""))

    def test_absent_config_dir_resolves_to_none(self):
        with mock.patch.dict(
            os.environ, {"CLAUDE_CONFIG_DIR": str(self.config / "nope")}
        ):
            self.assertIsNone(sessions.find_claude_session_file("s1"))


class DrainProcessTest(unittest.TestCase):
    def _req(self):
        run_dir = Path(tempfile.mkdtemp(dir=TMP_ROOT))
        return {
            "cwd": str(run_dir),
            "stdout_path": str(run_dir / "stdout.log"),
            "stderr_path": str(run_dir / "stderr.log"),
            "stream_path": str(run_dir / "stream.log"),
        }

    def test_returns_bounded_collector_and_writes_stderr_tail(self):
        req = self._req()
        proc = subprocess.Popen(
            ["/bin/sh", "-c", "printf 'a\\nb\\n'; printf 'oops\\n' 1>&2"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
        )
        code, collector = watcher.drain_process(proc, req, 10)
        self.assertEqual(code, 0)
        self.assertIn("b", collector.stdout_text)
        self.assertIn("oops", collector.stderr_text)
        self.assertIn("oops", Path(req["stderr_path"]).read_text())
        self.assertFalse(Path(req["stream_path"]).exists())
        self.assertFalse(Path(req["stdout_path"]).exists())

    def test_timeout_kills_group_and_raises(self):
        req = self._req()
        proc = subprocess.Popen(
            ["/bin/sh", "-c", "sleep 30"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
        )
        start = time.time()
        with self.assertRaises(watcher.WorkerCommandError) as ctx:
            watcher.drain_process(proc, req, 1)
        self.assertEqual(ctx.exception.kind, "timeout")
        self.assertLess(time.time() - start, 15)  # killed, not waited out
        # give the SIGKILL a moment to land
        for _ in range(20):
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        self.assertIsNotNone(proc.poll())

    def test_timeout_journals_session_discovery_before_terminal_fact(self):
        req = self._req()
        session_id = "timeout-session"
        session_file = Path(req["cwd"]) / f"conversation_{session_id}.jsonl"
        req["session_dir"] = req["cwd"]
        req["journal_path"] = str(Path(req["cwd"]) / "run.jsonl")
        collector = PiStreamCollector(
            RunJournal(req["journal_path"], "timeout-run"),
            grace_seconds=20,
        )
        collector.session_id = session_id
        script = (
            "from pathlib import Path; import time; "
            f"time.sleep(0.1); Path({str(session_file)!r}).write_text('{{}}\\n'); "
            "time.sleep(30)"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
        )

        with self.assertRaises(watcher.WorkerCommandError):
            watcher.drain_process(
                proc,
                req,
                1,
                render=watcher.render_pi_line,
                pi_settled_exit_grace_seconds=20,
                collector=collector,
            )

        facts = [
            json.loads(line)
            for line in Path(req["journal_path"]).read_text().splitlines()
        ]
        self.assertEqual(
            [fact["type"] for fact in facts], ["session_discovered", "timeout"]
        )

    def _pi_process(self, events: list[dict], *, tail: str = "time.sleep(30)"):
        writes = "\n".join(
            f"print({json.dumps(event)!r}, flush=True)" for event in events
        )
        return subprocess.Popen(
            [sys.executable, "-c", f"import time\n{writes}\n{tail}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
        )

    def test_pi_settled_error_terminates_leaked_process_for_recovery(self):
        req = self._req()
        req["pi_settled_exit_grace_seconds"] = 0.1
        proc = self._pi_process(
            [
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "error",
                        "errorMessage": "api error",
                    },
                },
                {"type": "agent_end", "willRetry": False},
                {"type": "agent_settled"},
            ]
        )

        start = time.time()
        code, _, err = watcher.finish_provider_process(proc, req, 10)

        self.assertNotEqual(code, 0)
        self.assertLess(time.time() - start, 3)
        self.assertIn("[pi-settled-safeguard]", err)
        self.assertIn("outcome=failure", Path(req["stderr_path"]).read_text())

    def test_pi_settlement_captured_during_launch_window_arms_safeguard(self):
        req = self._req()
        req["pi_settled_exit_grace_seconds"] = 0.1
        proc = self._pi_process([])
        prefix = b"\n".join(
            [
                json.dumps({"type": "agent_end", "willRetry": False}).encode(),
                json.dumps({"type": "agent_settled"}).encode(),
                b"",
            ]
        )

        code, _, err = watcher.finish_provider_process(
            proc, req, 10, stdout_prefix=prefix
        )

        self.assertNotEqual(code, 0)
        self.assertIn("[pi-settled-safeguard]", err)

    def test_pi_settled_success_terminates_leak_but_preserves_success(self):
        req = self._req()
        req["pi_settled_exit_grace_seconds"] = 0.1
        proc = self._pi_process(
            [
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "stop",
                        "content": [{"type": "text", "text": "finished\nSTATUS: done"}],
                    },
                },
                {"type": "agent_end", "willRetry": False},
                {"type": "agent_settled"},
            ]
        )

        code, out, err = watcher.finish_provider_process(proc, req, 10)

        self.assertEqual(code, 0)
        self.assertIn("STATUS: done", out)
        self.assertIn("outcome=success", err)

    def test_pi_settled_error_that_exits_cleanly_still_requests_recovery(self):
        req = self._req()
        req["pi_settled_exit_grace_seconds"] = 1
        proc = self._pi_process(
            [
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "error",
                        "errorMessage": "api error",
                    },
                },
                {"type": "agent_end", "willRetry": False},
                {"type": "agent_settled"},
            ],
            tail="pass",
        )

        code, _, err = watcher.finish_provider_process(proc, req, 10)

        self.assertNotEqual(code, 0)
        self.assertNotIn("[pi-settled-safeguard]", err)

    def test_pi_settled_with_retry_does_not_arm_safeguard(self):
        req = self._req()
        req["pi_settled_exit_grace_seconds"] = 0.05
        proc = self._pi_process(
            [
                {"type": "agent_end", "willRetry": True},
                {"type": "agent_settled"},
            ],
            tail="time.sleep(0.2)",
        )

        start = time.time()
        code, _, err = watcher.finish_provider_process(proc, req, 10)

        self.assertEqual(code, 0)
        self.assertGreaterEqual(time.time() - start, 0.15)
        self.assertNotIn("[pi-settled-safeguard]", err)

    def test_new_agent_activity_cancels_pending_safeguard(self):
        req = self._req()
        req["pi_settled_exit_grace_seconds"] = 0.15
        events = [
            {"type": "agent_end", "willRetry": False},
            {"type": "agent_settled"},
        ]
        writes = "\n".join(
            f"print({json.dumps(event)!r}, flush=True)" for event in events
        )
        script = (
            f"import time\n{writes}\n"
            "time.sleep(0.05)\n"
            f"print({json.dumps({'type': 'agent_start'})!r}, flush=True)\n"
            "time.sleep(0.2)"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
        )

        code, _, err = watcher.finish_provider_process(proc, req, 10)

        self.assertEqual(code, 0)
        self.assertNotIn("[pi-settled-safeguard]", err)

    def test_activity_before_settled_clears_stale_success_outcome(self):
        req = self._req()
        req["pi_settled_exit_grace_seconds"] = 0.1
        proc = self._pi_process(
            [
                {
                    "type": "message_end",
                    "message": {"role": "assistant", "stopReason": "stop"},
                },
                {"type": "agent_end", "willRetry": False},
                {"type": "agent_start"},
                {
                    "type": "message_end",
                    "message": {"role": "assistant", "stopReason": "length"},
                },
                {"type": "agent_end", "willRetry": False},
                {"type": "agent_settled"},
            ]
        )

        code, _, err = watcher.finish_provider_process(proc, req, 10)

        self.assertNotEqual(code, 0)
        self.assertIn("outcome=unknown", err)

    def test_kill_process_group_escalates_when_only_descendant_survives_term(self):
        child_code = (
            "import os, signal, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "fd = int(os.environ['READY_FD']); os.write(fd, b'1'); os.close(fd); time.sleep(30)"
        )
        parent_code = (
            "import os, subprocess, sys, time\n"
            "read_fd, write_fd = os.pipe()\n"
            "env = dict(os.environ, READY_FD=str(write_fd))\n"
            f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}], pass_fds=(write_fd,), env=env)\n"
            "os.close(write_fd); os.read(read_fd, 1); os.close(read_fd)\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(30)"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", parent_code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
        )
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.stderr.close)
        self.assertTrue(proc.stdout.readline().strip())

        with mock.patch.object(watcher, "TERM_GRACE_SECONDS", 0.05):
            watcher.kill_process_group(proc)
        proc.wait(timeout=2)

        readable, _, _ = select.select([proc.stdout], [], [], 2)
        self.assertTrue(readable, "descendant kept the process-group stdout pipe open")
        self.assertEqual(proc.stdout.read(), b"")


class FleetStatusDerivedTest(unittest.TestCase):
    def setUp(self):
        self.fs = fleet_status

    def test_working_when_session_alive(self):
        conv = {"status": "working", "current_run": {"tmux_session": "task-x"}}
        self.assertEqual(
            self.fs.derived_state(conv, conv["current_run"], True), "working"
        )

    def test_crashed_when_session_gone(self):
        conv = {"status": "working", "current_run": {"tmux_session": "task-x"}}
        self.assertEqual(
            self.fs.derived_state(conv, conv["current_run"], False), "crashed"
        )

    def test_finishing_when_success_artifact_exists_after_session_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "result.json"
            result_path.write_text('{"reply":"done"}')
            run = {
                "tmux_session": "task-x",
                "result_path": str(result_path),
            }
            conv = {"status": "working", "current_run": run}
            self.assertEqual(self.fs.derived_state(conv, run, False), "finishing")

    def test_queued_before_launch(self):
        run = {"launch_state": "launching"}
        self.assertEqual(
            self.fs.derived_state({"status": "working"}, run, False), "queued"
        )

    def test_parked_input_vs_review(self):
        self.assertEqual(
            self.fs.derived_state({"status": "parked", "anchor": "issue"}, None, False),
            "parked-input",
        )
        self.assertEqual(
            self.fs.derived_state({"status": "parked", "anchor": "mr"}, None, False),
            "parked-review",
        )

    def test_summarize_assistant_and_tool(self):
        text_ev = json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "hello world"}]},
            }
        )
        self.assertTrue(self.fs.summarize_line(text_ev).startswith("assistant: hello"))
        tool_ev = json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "Bash"}]},
            }
        )
        self.assertEqual(self.fs.summarize_line(tool_ev), "tool: Bash")
        # Both providers emit pure JSONL, so a non-JSON line is a partial mid-write
        # and must be skipped (not surfaced as raw bytes / base64).
        self.assertIsNone(self.fs.summarize_line("plain pi text"))

    def test_summarize_pi_tool_from_execution_start(self):
        # pi's resolved tool call is the top-level tool_execution_start event; the
        # message_update toolcall_* deltas are noise and must summarize to None.
        ev = json.dumps(
            {
                "type": "tool_execution_start",
                "toolName": "bash",
                "args": {"command": "git status"},
            }
        )
        self.assertEqual(self.fs.summarize_line(ev), "tool: bash git status")
        delta = json.dumps(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "toolcall_delta", "delta": '{"'},
            }
        )
        self.assertIsNone(self.fs.summarize_line(delta))

    def test_model_label_self_identifying(self):
        self.assertEqual(
            self.fs.model_label({"provider": "claude", "model": "opus"}), "claude:opus"
        )
        self.assertEqual(
            self.fs.model_label(
                {"provider": "pi", "model": "gpt-5.5", "effort": "high"}
            ),
            "pi:gpt-5.5:high",
        )

    def test_run_completed_at_reads_existing_result_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "result.json"
            result_path.write_text(json.dumps({"completed_at": 456.0}))
            self.assertEqual(
                self.fs.run_completed_at({"result_path": str(result_path)}),
                456.0,
            )

    def test_build_rows_emits_resume_metadata(self):
        state = {
            "projects": {
                "gitlab.example/group/repo": {
                    "conversations": {
                        "62": {
                            "status": "parked",
                            "anchor": "issue",
                            "provider": "claude",
                            "model": "opus",
                            "effort": "high",
                            "session_id": "sid-62",
                            "cwd": "/tmp/repo",
                            "session_dir": "/tmp/convos/repo-62",
                            "last_run": {
                                "stream_path": "/tmp/last-stream.log",
                                "started_at": 123.0,
                            },
                        }
                    }
                }
            }
        }
        original_load_state = self.fs.load_state
        original_live_sessions = self.fs.live_tmux_sessions
        original_running_sessions = self.fs.running_tmux_sessions
        self.fs.load_state = lambda: state
        self.fs.live_tmux_sessions = lambda: set()
        self.fs.running_tmux_sessions = lambda: set()
        try:
            built = self.fs.build_rows()
        finally:
            self.fs.load_state = original_load_state
            self.fs.live_tmux_sessions = original_live_sessions
            self.fs.running_tmux_sessions = original_running_sessions

        self.assertEqual(len(built), 1)
        emitted = built[0]
        self.assertEqual(emitted["identity"], "gitlab.example/group/repo:62")
        self.assertEqual(emitted["provider"], "claude")
        self.assertEqual(emitted["model_id"], "opus")
        self.assertEqual(emitted["effort"], "high")
        self.assertEqual(emitted["cwd"], "/tmp/repo")
        self.assertEqual(emitted["log"], "/tmp/last-stream.log")
        self.assertEqual(emitted["trace_source"], "legacy-stream")
        self.assertEqual(emitted["started_at"], 123.0)

    def build_one(self, conv):
        state = {
            "projects": {"gitlab.example/group/repo": {"conversations": {"62": conv}}}
        }
        with (
            mock.patch.object(self.fs, "load_state", lambda: state),
            mock.patch.object(self.fs, "live_tmux_sessions", set),
            mock.patch.object(self.fs, "running_tmux_sessions", set),
        ):
            built = self.fs.build_rows()
        self.assertEqual(len(built), 1)
        return built[0]

    def claude_conv(self, **overrides):
        conv = {
            "status": "done",
            "provider": "claude",
            "model": "opus",
            "session_id": "sid-62",
            "cwd": "/tmp/repo",
            "session_dir": "/tmp/convos/repo-62",
            "last_run": {"run_id": "run-62"},
        }
        conv.update(overrides)
        return conv

    def test_claude_row_previews_its_native_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "sid-62.jsonl"
            transcript.write_text('{"anything":true}\n')
            emitted = self.build_one(
                self.claude_conv(session_file=str(transcript)),
            )
        self.assertEqual(emitted["log"], str(transcript))
        self.assertEqual(emitted["trace_source"], "session")

    def test_finished_claude_row_backfills_from_its_session_id(self):
        # Runs that predate discovery have neither a stored session_file nor a
        # session_discovered fact — only the id claude reported.
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp)
            transcript = config / "projects" / "-tmp-repo" / "sid-62.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text('{"anything":true}\n')
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}):
                emitted = self.build_one(self.claude_conv())
        self.assertEqual(emitted["log"], str(transcript))
        self.assertEqual(emitted["trace_source"], "session")

    def test_pruned_transcript_reports_expired_not_blank(self):
        emitted = self.build_one(
            self.claude_conv(session_file="/tmp/absent/sid-62.jsonl"),
        )
        self.assertEqual(emitted["log"], "")
        self.assertEqual(emitted["trace_source"], "expired")

    def test_claude_row_with_no_transcript_anywhere_reports_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": tmp}):
                emitted = self.build_one(self.claude_conv())
        self.assertEqual(emitted["log"], "")
        self.assertEqual(emitted["trace_source"], "missing")

    def test_pi_rows_keep_selecting_their_session_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "session_sid-62.jsonl"
            session.write_text("{}\n")
            emitted = self.build_one(
                {
                    "status": "done",
                    "provider": "pi",
                    "model": "gpt-5.5",
                    "session_id": "sid-62",
                    "session_file": str(session),
                    "cwd": "/tmp/repo",
                    "session_dir": tmp,
                    "last_run": {"run_id": "run-62"},
                }
            )
        self.assertEqual(emitted["log"], str(session))
        self.assertEqual(emitted["trace_source"], "session")

    def test_build_rows_emits_pi_session_file_as_resume_handle(self):
        state = {
            "projects": {
                "gitlab.example/group/repo": {
                    "conversations": {
                        "62": {
                            "status": "done",
                            "provider": "pi",
                            "model": "gpt-5.5",
                            "session_id": "sid-62",
                            "session_file": "/tmp/convos/repo-62/session_sid-62.jsonl",
                            "cwd": "/tmp/repo",
                            "session_dir": "/tmp/convos/repo-62",
                            "current_run": None,
                            "last_run": {"run_id": "run-62"},
                        }
                    }
                }
            }
        }
        with (
            mock.patch.object(self.fs, "load_state", return_value=state),
            mock.patch.object(self.fs, "live_tmux_sessions", return_value=set()),
            mock.patch.object(self.fs, "running_tmux_sessions", return_value=set()),
        ):
            built = self.fs.build_rows()

        self.assertEqual(
            built[0]["session"],
            "/tmp/convos/repo-62/session_sid-62.jsonl",
        )

    def test_build_rows_reads_status_and_preview_from_journal_and_pi_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal_path = Path(tmp) / "run.jsonl"
            session_path = Path(tmp) / "session.jsonl"
            session_path.write_text('{"type":"session"}\n')
            journal_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "v": 1,
                                "ts": 1,
                                "run_id": "run-62",
                                "type": "session_discovered",
                                "session_file": str(session_path),
                            }
                        ),
                        json.dumps(
                            {
                                "v": 1,
                                "ts": 2,
                                "run_id": "run-62",
                                "type": "tool_first_started",
                                "tool": "bash",
                            }
                        ),
                    ]
                )
                + "\n"
            )
            state = {
                "projects": {
                    "gitlab.example/group/repo": {
                        "conversations": {
                            "62": {
                                "status": "working",
                                "provider": "pi",
                                "model": "gpt-5.5",
                                "cwd": tmp,
                                "session_dir": tmp,
                                "current_run": {
                                    "run_id": "run-62",
                                    "journal_path": str(journal_path),
                                    "tmux_session": "task-repo-62",
                                },
                            }
                        }
                    }
                }
            }
            with (
                mock.patch.object(self.fs, "load_state", return_value=state),
                mock.patch.object(
                    self.fs, "live_tmux_sessions", return_value={"task-repo-62"}
                ),
                mock.patch.object(
                    self.fs, "running_tmux_sessions", return_value={"task-repo-62"}
                ),
            ):
                built = self.fs.build_rows()

        self.assertEqual(built[0]["last_line"], "tool: bash")
        self.assertEqual(built[0]["log"], str(session_path))
        self.assertEqual(built[0]["session"], str(session_path))
        self.assertEqual(built[0]["journal"], str(journal_path))

    def test_build_rows_keeps_finished_run_attachable_until_dismissed(self):
        state = {
            "projects": {
                "gitlab.example/group/repo": {
                    "conversations": {
                        "62": {
                            "status": "done",
                            "provider": "claude",
                            "model": "opus",
                            "cwd": "/tmp/repo",
                            "session_dir": "/tmp/convos/repo-62",
                            "current_run": None,
                            "last_run": {
                                "run_id": "run-62",
                                "tmux_session": "task-repo-62",
                                "stream_path": "/tmp/stream.log",
                                "started_at": 123.0,
                                "completed_at": 456.0,
                            },
                        }
                    }
                }
            }
        }
        with (
            mock.patch.object(self.fs, "load_state", return_value=state),
            mock.patch.object(
                self.fs,
                "live_tmux_sessions",
                return_value={"task-repo-62"},
            ),
            mock.patch.object(self.fs, "running_tmux_sessions", return_value=set()),
        ):
            built = self.fs.build_rows()

        self.assertEqual(len(built), 1)
        self.assertEqual(built[0]["derived"], "finished")
        self.assertTrue(built[0]["tmux_alive"])
        self.assertEqual(built[0]["run_id"], "run-62")
        self.assertEqual(built[0]["finished_at"], 456.0)


class RenderStreamLineTest(unittest.TestCase):
    def test_assistant_text(self):
        ev = json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "hello there"}]},
            }
        )
        self.assertEqual(watcher.render_stream_line(ev), "hello there\n")

    def test_thinking_and_tool(self):
        think = json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "thinking", "thinking": "hmm"}]},
            }
        )
        self.assertEqual(watcher.render_stream_line(think), "· thinking…\n")
        tool = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "git status"},
                        }
                    ]
                },
            }
        )
        self.assertEqual(watcher.render_stream_line(tool), "→ Bash git status\n")

    def test_result_and_dropped_events(self):
        self.assertEqual(
            watcher.render_stream_line(
                json.dumps({"type": "result", "subtype": "success"})
            ),
            "── success\n",
        )
        # system / rate_limit / unparseable are dropped from the pane
        self.assertIsNone(
            watcher.render_stream_line(
                json.dumps({"type": "system", "subtype": "init"})
            )
        )
        self.assertIsNone(watcher.render_stream_line("not json at all"))
        self.assertIsNone(watcher.render_stream_line(""))

    def test_tool_detail_truncated(self):
        long = "x" * (watcher.PANE_DETAIL_MAX + 200)
        ev = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "input": {"command": long}}
                    ]
                },
            }
        )
        out = watcher.render_stream_line(ev)
        self.assertTrue(out.startswith("→ Bash "))
        self.assertLessEqual(
            len(out.rstrip()), len("→ Bash ") + watcher.PANE_DETAIL_MAX
        )


class RenderPiLineTest(unittest.TestCase):
    def _upd(self, ame):
        return json.dumps({"type": "message_update", "assistantMessageEvent": ame})

    def test_text_delta_inline_no_newline(self):
        self.assertEqual(
            watcher.render_pi_line(self._upd({"type": "text_delta", "delta": "HEL"})),
            "HEL",
        )
        self.assertEqual(
            watcher.render_pi_line(self._upd({"type": "text_delta", "delta": "LO"})),
            "LO",
        )

    def test_thinking_and_end(self):
        self.assertEqual(
            watcher.render_pi_line(self._upd({"type": "thinking_start"})),
            "· thinking…\n",
        )
        self.assertEqual(
            watcher.render_pi_line(self._upd({"type": "text_end", "content": "HELLO"})),
            "\n",
        )
        self.assertEqual(
            watcher.render_pi_line(json.dumps({"type": "agent_end"})), "── done\n"
        )

    def test_tool_rendered_from_tool_execution_start(self):
        # The resolved tool call is the top-level tool_execution_start event, which
        # carries toolName + full args — render name + a salient arg.
        ev = json.dumps(
            {
                "type": "tool_execution_start",
                "toolName": "bash",
                "args": {"command": "git status"},
            }
        )
        self.assertEqual(watcher.render_pi_line(ev), "→ bash git status\n")
        readev = json.dumps(
            {
                "type": "tool_execution_start",
                "toolName": "read",
                "args": {"file": "AGENTS.md"},
            }
        )
        self.assertEqual(watcher.render_pi_line(readev), "→ read AGENTS.md\n")
        todoev = json.dumps(
            {
                "type": "tool_execution_start",
                "toolName": "todo",
                "args": {"action": "complete"},
            }
        )
        self.assertEqual(watcher.render_pi_line(todoev), "→ todo complete\n")

    def test_tool_detail_truncated(self):
        ev = json.dumps(
            {
                "type": "tool_execution_start",
                "toolName": "bash",
                "args": {"command": "x" * (watcher.PANE_DETAIL_MAX + 200)},
            }
        )
        out = watcher.render_pi_line(ev)
        self.assertTrue(out.startswith("→ bash "))
        self.assertLessEqual(
            len(out.rstrip()), len("→ bash ") + watcher.PANE_DETAIL_MAX
        )

    def test_toolcall_deltas_are_noise(self):
        # message_update toolcall_* events stream arg JSON char-by-char — must NOT
        # render (this was the blank-then-flooded '→ tool' pane bug).
        for at in ("toolcall_start", "toolcall_delta", "toolcall_end"):
            self.assertIsNone(
                watcher.render_pi_line(self._upd({"type": at, "delta": '{"'})), at
            )

    def test_noise_events_dropped(self):
        self.assertIsNone(
            watcher.render_pi_line(json.dumps({"type": "session", "id": "x"}))
        )
        self.assertIsNone(watcher.render_pi_line(json.dumps({"type": "turn_start"})))
        self.assertIsNone(watcher.render_pi_line("not json"))

    def test_deltas_stream_inline_through_panewriter(self):
        import io

        sink = io.StringIO()
        pw = watcher.PaneWriter(sink, watcher.render_pi_line)
        pw.feed(self._upd({"type": "text_delta", "delta": "HEL"}) + "\n")
        pw.feed(self._upd({"type": "text_delta", "delta": "LO"}) + "\n")
        pw.feed(self._upd({"type": "text_end"}) + "\n")
        self.assertEqual(sink.getvalue(), "HELLO\n")  # deltas concatenated inline


class PiStreamCollectorReplyTest(unittest.TestCase):
    def collect(self, lines):
        collector = PiStreamCollector(RunJournal(None, "test"), grace_seconds=20)
        collector.feed(("\n".join(json.dumps(line) for line in lines) + "\n").encode())
        return collector

    def test_reply_from_last_assistant_message_end(self):
        collector = self.collect(
            [
                {"type": "session", "id": "s"},
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": "HEL"},
                },
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": "LO"},
                },
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": ""},
                            {"type": "text", "text": "HELLO"},
                        ],
                    },
                },
                {"type": "agent_end"},
            ]
        )
        self.assertEqual(collector.reply, "HELLO")

    def test_falls_back_to_deltas_if_no_message_end(self):
        collector = self.collect(
            [
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": "par"},
                },
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": "tial"},
                },
            ]
        )
        self.assertEqual(collector.reply, "partial")

    def test_ignores_user_message_end(self):
        collector = self.collect(
            [
                {
                    "type": "message_end",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "the question"}],
                    },
                },
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "the answer"}],
                    },
                },
            ]
        )
        self.assertEqual(collector.reply, "the answer")


class PaneWriterTest(unittest.TestCase):
    def test_renders_only_complete_lines_raw_hidden(self):
        import io

        sink = io.StringIO()
        pw = watcher.PaneWriter(sink, watcher.render_stream_line)
        # a JSONL event split across two feeds — nothing until the newline arrives
        first = (
            '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}'
        )
        pw.feed(first[:20])
        self.assertEqual(sink.getvalue(), "")  # partial line not rendered
        pw.feed(first[20:] + "\n")
        self.assertEqual(sink.getvalue(), "hi\n")  # rendered, not raw JSON
        self.assertNotIn("{", sink.getvalue())

    def test_oversized_partial_line_is_dropped_without_unbounded_buffering(self):
        import io

        sink = io.StringIO()
        writer = watcher.PaneWriter(
            sink, watcher.render_stream_line, max_buffer_chars=100
        )
        writer.feed("x" * 101)
        self.assertEqual(writer.buf, "")
        self.assertTrue(writer.discarding_line)
        writer.feed('\n{"type":"result","subtype":"ok"}\n')
        self.assertEqual(sink.getvalue(), "── ok\n")
        self.assertFalse(writer.discarding_line)

    def test_passthrough_without_render(self):
        import io

        sink = io.StringIO()
        pw = watcher.PaneWriter(sink, None)
        pw.feed("plain pi text\n")
        self.assertEqual(sink.getvalue(), "plain pi text\n")

    def test_none_sink_is_noop(self):
        pw = watcher.PaneWriter(None, watcher.render_stream_line)
        pw.feed('{"type":"result","subtype":"success"}\n')  # must not raise
        pw.close()


if __name__ == "__main__":
    unittest.main()
