"""Headless interaction tests for the Textual fleet console."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    from textual.widgets import DataTable, Input, OptionList, Static
except ImportError as exc:  # Normal watcher tests do not install optional TUI deps.
    raise unittest.SkipTest("Textual is not installed") from exc

from eastwatch.fleet.core import RepositoryIdentity
from eastwatch.fleet.tui import (
    ALL_REPOSITORIES,
    BW,
    FINISHED_TRACE_GRACE_S,
    FINISHED_TRACE_LINES,
    FLEET_THEMES,
    STATE_STYLE,
    Chrome,
    FleetApp,
    FleetFilterInput,
    FleetTable,
    RepositorySwitcher,
    ThemeSwitcher,
    TraceView,
    main,
)


def render_text(widget: Static) -> str:
    if isinstance(widget, TraceView):
        return "\n".join(widget.source_lines)
    rendered = widget.render()
    return rendered.plain if hasattr(rendered, "plain") else str(rendered)


class FleetTUITest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.rows_path = self.root / "rows.json"
        self.state_path = self.root / "state.json"
        self.state_path.write_text("{}")
        self.claude_log = self.root / "claude.log"
        self.claude_log.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "Need retry ownership"}]
                    },
                }
            )
            + "\n"
        )
        self.pi_log = self.root / "pi.log"
        self.pi_log.write_text(
            json.dumps(
                {
                    "type": "tool_execution_start",
                    "toolName": "bash",
                    "args": {"command": "git status"},
                }
            )
            + "\n"
        )
        self.status = self.root / "fleet-status"
        self.status.write_text(f"#!/bin/sh\n/bin/cat {self.rows_path}\n")
        self.status.chmod(0o755)
        self.resume = self.root / "fleet-resume"
        self.resume.write_text("#!/bin/sh\nexit 0\n")
        self.resume.chmod(0o755)
        self.dismiss = self.root / "fleet-dismiss"
        self.dismiss.write_text("#!/bin/sh\nexit 0\n")
        self.dismiss.chmod(0o755)
        self.hosted_rows_path = self.root / "hosted-rows.json"
        self.hosted = self.root / "fleet-hosted"
        self.hosted.write_text(f"#!/bin/sh\n/bin/cat {self.hosted_rows_path}\n")
        self.hosted.chmod(0o755)
        self.hosted_fails = self.root / "fleet-hosted-fails"
        self.hosted_fails.write_text("#!/bin/sh\necho 'boom' 1>&2\nexit 1\n")
        self.hosted_fails.chmod(0o755)
        self.write_rows()
        self.write_hosted_rows()

    def tearDown(self):
        self.temp.cleanup()

    def write_rows(self):
        rows = [
            {
                "identity": "parked",
                "key": "task-issue-62",
                "surface": "gitlab",
                "status": "parked",
                "derived": "parked-input",
                "model": "claude:opus",
                "provider": "claude",
                "model_id": "opus",
                "session": "sid-62",
                "tmux_alive": False,
                "log": str(self.claude_log),
                "last_line": "assistant: Need retry ownership",
                "url": "https://gitlab.example/issues/62",
                "cwd": "/repos/alpha",
                "started_at": time.time() - 90,
            },
            {
                "identity": "working",
                "key": "task-issue-71",
                "surface": "gitlab",
                "status": "working",
                "derived": "working",
                "model": "pi:gpt-5.5:high",
                "provider": "pi",
                "model_id": "gpt-5.5",
                "session": "/tmp/pi-session",
                "tmux_alive": True,
                "log": str(self.pi_log),
                "last_line": "tool: bash git status",
                "url": "https://gitlab.example/issues/71",
                "cwd": "/repos/beta",
                "started_at": time.time() - 360,
            },
        ]
        self.rows_path.write_text(json.dumps(rows))

    def write_hosted_rows(self, rows=None):
        if rows is None:
            rows = [
                {
                    "identity": "hosted-62",
                    "key": "task-issue-62",
                    "surface": "issue",
                    "status": "leased",
                    "derived": "working",
                    "model": "claude:opus",
                    "provider": "claude",
                    "model_id": "opus",
                    "session": "",
                    "tmux_alive": False,
                    "log": "",
                    "url": "https://gitlab.example/issues/62",
                    "cwd": "",
                    "started_at": time.time() - 40,
                    "job_id": "job-62",
                    "owner": "alice",
                    "remote": True,
                    "capabilities": ["logs", "attach", "resume"],
                    "last_heartbeat_at": time.time() - 5,
                },
            ]
        self.hosted_rows_path.write_text(json.dumps(rows))

    def app(self, **kwargs) -> FleetApp:
        def repository_resolver(cwd: str) -> RepositoryIdentity:
            if cwd.endswith("/alpha"):
                return RepositoryIdentity("/git/orchid/.git", "Orchid")
            if cwd.endswith("/beta"):
                return RepositoryIdentity("/git/cobalt/.git", "Cobalt")
            return RepositoryIdentity("unknown", "Unknown")

        kwargs.setdefault("repository_resolver", repository_resolver)
        return FleetApp(
            fleet_status=self.status,
            fleet_resume=self.resume,
            fleet_dismiss=self.dismiss,
            state_path=self.state_path,
            poll_interval_s=60,
            **kwargs,
        )

    async def wait_loaded(self, app: FleetApp, pilot) -> None:
        await self.wait_row_count(app, pilot, 2)

    async def wait_row_count(self, app: FleetApp, pilot, expected: int) -> None:
        for _ in range(120):
            table = app.query_one("#fleet", DataTable)
            if table.row_count == expected:
                return
            await pilot.pause(0.05)
        self.fail(
            f"fleet row count did not reach {expected}: "
            f"rows={len(app.snapshot.rows)} visible={len(app._visible_rows())} "
            f"rendered={table.row_count} query={app.filter_query!r} "
            f"scope={app.state_scope} editing={app._filter_editing} "
            f"focus={type(app.focused).__name__} error={app.snapshot.error} "
            f"running={app._refresh_running}"
        )

    async def wait_trace_contains(self, app: FleetApp, pilot, text: str) -> None:
        # The tail subprocess is debounced behind row selection, so trace
        # content lands a beat after the row itself.
        for _ in range(120):
            if text in render_text(app.query_one("#trace", TraceView)):
                return
            await pilot.pause(0.05)
        self.fail(
            f"trace never showed {text!r}: "
            f"{render_text(app.query_one('#trace', TraceView))!r}"
        )

    async def test_horizontal_layout_loads_rows_and_switches_provider_trace(self):
        app = self.app()
        async with app.run_test(size=(120, 32)) as pilot:
            await self.wait_loaded(app, pilot)
            await pilot.pause(0.1)
            table = app.query_one("#fleet", DataTable)
            self.assertEqual(table.row_count, 2)
            self.assertEqual(app._layout_mode, "compact")
            self.assertEqual(app.selected_identity, "parked")
            await self.wait_trace_contains(app, pilot, "Need retry ownership")

            await pilot.press("down")
            await pilot.pause(0.2)
            self.assertEqual(app.selected_identity, "working")
            await self.wait_trace_contains(app, pilot, "git status")

    async def test_vim_keys_move_fleet_cursor(self):
        app = self.app()
        async with app.run_test(size=(120, 32)) as pilot:
            await self.wait_loaded(app, pilot)
            self.assertEqual(app.selected_identity, "parked")

            await pilot.press("j")
            await pilot.pause()
            self.assertEqual(app.selected_identity, "working")

            await pilot.press("k")
            await pilot.pause()
            self.assertEqual(app.selected_identity, "parked")

    async def test_slash_filters_live_and_enter_keeps_the_query(self):
        app = self.app()
        async with app.run_test(size=(140, 32)) as pilot:
            await self.wait_loaded(app, pilot)

            await pilot.press("slash")
            await pilot.pause()
            await pilot.press("7")
            await pilot.pause()
            await pilot.press("1")
            await self.wait_row_count(app, pilot, 1)

            table = app.query_one("#fleet", DataTable)
            filter_input = app.query_one("#filter", Input)
            self.assertIs(app.focused, filter_input)
            self.assertEqual(table.row_count, 1)
            self.assertEqual(app.selected_identity, "working")
            self.assertEqual(app.filter_query, "71")
            self.assertIn(
                "1/2 VISIBLE  ·  /71",
                render_text(app.query_one("#topbar", Static)),
            )
            await self.wait_trace_contains(app, pilot, "git status")

            await pilot.press("enter")
            await pilot.pause()
            self.assertIs(app.focused, table)
            self.assertEqual(app.filter_query, "71")
            self.assertFalse(filter_input.has_class("visible"))
            self.assertEqual(table.row_count, 1)

    async def test_escape_clears_filter_and_no_matches_are_explicit(self):
        app = self.app()
        async with app.run_test(size=(140, 32)) as pilot:
            await self.wait_loaded(app, pilot)

            await pilot.press("slash", "z", "z", "q", "q")
            await self.wait_row_count(app, pilot, 0)

            table = app.query_one("#fleet", DataTable)
            filter_input = app.query_one("#filter", FleetFilterInput)
            self.assertEqual(table.row_count, 0)
            self.assertIsNone(app.current_row())
            self.assertIn(
                "No conversations match /zzqq",
                render_text(app.query_one("#trace", TraceView)),
            )
            self.assertIn(
                "0/2 VISIBLE  ·  /zzqq",
                render_text(app.query_one("#topbar", Static)),
            )

            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(app.filter_query, "")
            self.assertEqual(filter_input.value, "")
            self.assertFalse(filter_input.has_class("visible"))
            self.assertIs(app.focused, table)
            self.assertEqual(table.row_count, 2)
            self.assertEqual(app.selected_identity, "parked")

    async def test_number_keys_apply_quick_state_scopes(self):
        app = self.app()
        async with app.run_test(size=(140, 32)) as pilot:
            await self.wait_loaded(app, pilot)
            table = app.query_one("#fleet", DataTable)

            await pilot.press("3")
            await self.wait_row_count(app, pilot, 1)
            self.assertEqual(app.state_scope, "running")
            self.assertEqual(table.row_count, 1)
            self.assertEqual(app.selected_identity, "working")
            self.assertIn(
                "RUNNING 1",
                render_text(app.query_one("#topbar", Static)),
            )
            self.assertIn(
                "1-5 states:running",
                render_text(app.query_one("#keys", Static)),
            )

            await pilot.press("2")
            await pilot.pause()
            self.assertEqual(app.state_scope, "needs")
            self.assertEqual(table.row_count, 1)
            self.assertEqual(app.selected_identity, "parked")

            await pilot.press("4")
            await self.wait_row_count(app, pilot, 0)
            self.assertEqual(app.state_scope, "review")
            self.assertEqual(table.row_count, 0)
            self.assertIsNone(app.selected_identity)
            self.assertIn(
                "No conversations in Review",
                render_text(app.query_one("#trace", TraceView)),
            )

            await pilot.press("1")
            await self.wait_row_count(app, pilot, 2)
            self.assertEqual(app.state_scope, "all")
            self.assertEqual(table.row_count, 2)
            self.assertEqual(app.selected_identity, "parked")

    async def test_state_scope_and_fuzzy_filter_use_and_semantics(self):
        app = self.app()
        async with app.run_test(size=(140, 32)) as pilot:
            await self.wait_loaded(app, pilot)

            await pilot.press("3")
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            await pilot.press("6")
            await pilot.pause()
            await pilot.press("2")
            await self.wait_row_count(app, pilot, 0)
            table = app.query_one("#fleet", DataTable)
            self.assertEqual(app.state_scope, "running")
            self.assertEqual(app.filter_query, "62")
            self.assertEqual(table.row_count, 0)

            await pilot.press("escape")
            await self.wait_row_count(app, pilot, 1)
            self.assertEqual(app.state_scope, "running")
            self.assertEqual(app.filter_query, "")
            self.assertEqual(table.row_count, 1)
            self.assertEqual(app.selected_identity, "working")

    async def test_g_scopes_rows_by_repository_and_composes_with_state(self):
        app = self.app()
        async with app.run_test(size=(140, 32)) as pilot:
            await self.wait_loaded(app, pilot)

            await pilot.press("g")
            await pilot.pause()
            self.assertIsInstance(app.screen, RepositorySwitcher)
            options = app.screen.query_one("#repo-options", OptionList)
            self.assertEqual(options.option_count, 3)
            self.assertEqual(options.highlighted, 0)

            await pilot.press("j", "enter")
            await self.wait_row_count(app, pilot, 1)
            self.assertEqual(app.repository_scope, "/git/cobalt/.git")
            self.assertEqual(app.selected_identity, "working")
            self.assertIn(
                "REPO Cobalt 1",
                render_text(app.query_one("#topbar", Static)),
            )
            self.assertIn(
                "g repo:cobalt(1)",
                render_text(app.query_one("#keys", Static)),
            )

            await pilot.press("2")
            await self.wait_row_count(app, pilot, 0)
            self.assertEqual(app.state_scope, "needs")

            await pilot.press("g")
            await pilot.pause()
            options = app.screen.query_one("#repo-options", OptionList)
            self.assertEqual(options.highlighted, 1)
            await pilot.press("j", "enter")
            await self.wait_row_count(app, pilot, 1)
            self.assertEqual(app.repository_scope, "/git/orchid/.git")
            self.assertEqual(app.state_scope, "needs")
            self.assertEqual(app.selected_identity, "parked")

            payload = json.loads(self.rows_path.read_text())
            self.rows_path.write_text(json.dumps([payload[1]]))
            app.refresh_snapshot()
            await self.wait_row_count(app, pilot, 0)
            self.assertEqual(app.repository_scope, "/git/orchid/.git")
            self.assertIn(
                "No conversations in repository Orchid",
                render_text(app.query_one("#trace", TraceView)),
            )

    async def test_repository_resolution_is_cached_across_refreshes(self):
        resolver = mock.Mock(
            side_effect=lambda cwd: RepositoryIdentity(cwd, Path(cwd).name)
        )
        app = self.app(repository_resolver=resolver)
        async with app.run_test(size=(120, 32)) as pilot:
            await self.wait_loaded(app, pilot)
            self.assertEqual(resolver.call_count, 2)
            refreshed_at = app.snapshot.refreshed_at

            app.refresh_snapshot()
            for _ in range(120):
                if app.snapshot.refreshed_at > refreshed_at:
                    break
                await pilot.pause(0.05)
            self.assertGreater(app.snapshot.refreshed_at, refreshed_at)
            self.assertEqual(resolver.call_count, 2)

    async def test_repository_label_participates_in_fuzzy_filter(self):
        app = self.app()
        async with app.run_test(size=(140, 32)) as pilot:
            await self.wait_loaded(app, pilot)
            await pilot.press("slash")
            await pilot.pause()
            for character in "cobalt":
                await pilot.press(character)
                await pilot.pause()
            await self.wait_row_count(app, pilot, 1)
            self.assertEqual(app.selected_identity, "working")
            self.assertEqual(app.repository_scope, ALL_REPOSITORIES)

    async def test_status_uses_one_circle_shape_and_header_stays_on_one_line(self):
        self.assertEqual({symbol for symbol, _ in STATE_STYLE.values()}, {"●"})
        app = self.app()
        async with app.run_test(size=(120, 32)) as pilot:
            await self.wait_loaded(app, pilot)
            topbar = app.query_one("#topbar", Static)
            header = render_text(topbar)
            self.assertEqual(topbar.region.height, 1)
            self.assertNotIn("\n", header)
            self.assertIn("FLEET   2 ACTIVE", header)
            self.assertIn("● 1 RUNNING", header)
            self.assertIn("● 0 REVIEW", header)
            self.assertIn("CYCLE", header)

    async def test_t_opens_theme_switcher_and_applies_gruvbox(self):
        self.assertEqual(
            FLEET_THEMES["catppuccin-mocha"].colors["void"],
            "#1e1e2e",
        )
        self.assertEqual(
            FLEET_THEMES["gruvbox-dark-hard"].colors["void"],
            "#1d2021",
        )
        app = self.app()
        async with app.run_test(size=(120, 32)) as pilot:
            await self.wait_loaded(app, pilot)
            self.assertEqual(app.theme, "catppuccin-mocha")

            await pilot.press("t")
            await pilot.pause()
            self.assertIsInstance(app.screen, ThemeSwitcher)

            await pilot.press("j", "enter")
            await pilot.pause()
            self.assertEqual(app.theme, "gruvbox-dark-hard")
            self.assertNotIsInstance(app.screen, ThemeSwitcher)
            self.assertEqual(
                app.query_one("#trace", TraceView).palette["void"],
                "#1d2021",
            )
            self.assertIn(
                "Gruvbox Dark Hard",
                render_text(app.query_one("#notice", Static)),
            )

    async def test_main_uses_bounded_textual_selection(self):
        self.assertTrue(TraceView.ALLOW_SELECT)
        self.assertFalse(Chrome.ALLOW_SELECT)
        self.assertFalse(FleetTable.ALLOW_SELECT)
        with mock.patch("eastwatch.fleet.tui.FleetApp.run") as run:
            main(argv=[])
        run.assert_called_once_with(mouse=True)

    async def test_copy_uses_macos_clipboard_fallback(self):
        app = self.app()
        completed = mock.Mock(returncode=0)
        with (
            mock.patch("eastwatch.fleet.tui.sys.platform", "darwin"),
            mock.patch(
                "eastwatch.fleet.tui.subprocess.run", return_value=completed
            ) as run,
        ):
            app.copy_to_clipboard("trace only")
        self.assertEqual(app._clipboard, "trace only")
        run.assert_called_once_with(
            ["pbcopy"],
            input="trace only",
            text=True,
            timeout=2,
            check=False,
        )

    async def test_drag_selection_stays_inside_trace_widget(self):
        app = self.app()
        async with app.run_test(size=(120, 32)) as pilot:
            await self.wait_loaded(app, pilot)
            trace = app.query_one("#trace", TraceView)
            trace.show_lines(("alpha beta gamma", "second trace line"))
            await pilot.pause()

            await pilot.mouse_down(trace, offset=(2, 0))
            await pilot.hover(trace, offset=(8, 0))
            await pilot.hover("#fleet", offset=(5, 5))
            await pilot.mouse_up("#fleet", offset=(5, 5))

            selected = app.screen.get_selected_text()
            self.assertTrue(selected)
            self.assertNotIn("issue-62", selected or "")

    async def test_fleet_and_trace_are_side_by_side_and_trace_width_is_adjustable(self):
        app = self.app()
        async with app.run_test(size=(120, 32)) as pilot:
            await self.wait_loaded(app, pilot)
            fleet = app.query_one("#fleet", DataTable)
            trace = app.query_one("#trace", TraceView)
            self.assertLess(fleet.region.x, trace.region.x)
            self.assertEqual(fleet.max_scroll_x, 0)

            initial_width = trace.region.width
            await pilot.press("right_square_bracket")
            await pilot.pause(0.1)
            self.assertLess(trace.region.width, initial_width)
            self.assertEqual(fleet.max_scroll_x, 0)

            await pilot.press("left_square_bracket", "left_square_bracket")
            await pilot.pause(0.1)
            self.assertGreater(trace.region.width, initial_width)
            self.assertEqual(fleet.max_scroll_x, 0)

    async def test_v_toggles_bottom_layout_with_independent_trace_height(self):
        app = self.app()
        async with app.run_test(size=(120, 32)) as pilot:
            await self.wait_loaded(app, pilot)
            fleet = app.query_one("#fleet", DataTable)
            trace = app.query_one("#trace", TraceView)
            selected = app.selected_identity
            trace_model = app._trace_model
            right_width_percent = app._trace_width_percent

            await pilot.press("v")
            await pilot.pause(0.2)
            self.assertEqual(app.trace_layout, "bottom")
            self.assertLess(fleet.region.y, trace.region.y)
            self.assertEqual(fleet.region.x, trace.region.x)
            self.assertEqual(app.selected_identity, selected)
            self.assertIs(app._trace_model, trace_model)
            self.assertIn("Bottom", render_text(app.query_one("#notice", Static)))
            self.assertIn(
                "v layout:bottom",
                render_text(app.query_one("#keys", Static)),
            )

            initial_height = trace.region.height
            await pilot.press("right_square_bracket")
            await pilot.pause(0.2)
            self.assertLess(trace.region.height, initial_height)
            bottom_height_percent = app._trace_height_percent

            await pilot.press("v")
            await pilot.pause(0.2)
            self.assertEqual(app.trace_layout, "right")
            self.assertLess(fleet.region.x, trace.region.x)
            self.assertEqual(app._trace_width_percent, right_width_percent)

            await pilot.press("left_square_bracket", "v")
            await pilot.pause(0.2)
            self.assertEqual(app.trace_layout, "bottom")
            self.assertEqual(app._trace_height_percent, bottom_height_percent)
            self.assertEqual(app.selected_identity, selected)
            self.assertIs(app._trace_model, trace_model)

    async def test_stable_refresh_updates_cells_without_clearing_table(self):
        app = self.app()
        async with app.run_test(size=(120, 32)) as pilot:
            await self.wait_loaded(app, pilot)
            table = app.query_one("#fleet", DataTable)
            payload = json.loads(self.rows_path.read_text())
            payload[0]["derived"] = "crashed"
            self.rows_path.write_text(json.dumps(payload))

            with mock.patch.object(table, "clear", wraps=table.clear) as clear:
                app.refresh_snapshot()
                for _ in range(40):
                    if app.snapshot.rows[0].derived == "crashed":
                        break
                    await pilot.pause(0.05)
                clear.assert_not_called()
            state = table.get_cell("parked", "state")
            self.assertEqual(state.plain, "CRASHED")

    async def test_trace_follows_new_events_and_scrolls_to_latest(self):
        events = [
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": f"event {index}"}]},
            }
            for index in range(40)
        ]
        self.claude_log.write_text(
            "\n".join(json.dumps(event) for event in events) + "\n"
        )

        app = self.app()
        async with app.run_test(size=(100, 24)) as pilot:
            await self.wait_loaded(app, pilot)
            trace = app.query_one("#trace", TraceView)
            fresh = {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "fresh event"}]},
            }
            with self.claude_log.open("a") as stream:
                stream.write(json.dumps(fresh) + "\n")
            for _ in range(40):
                if "fresh event" in render_text(trace):
                    break
                await pilot.pause(0.05)
            await pilot.pause(0.2)
            self.assertIn("fresh event", render_text(trace))
            self.assertGreater(trace.max_scroll_y, 0)
            self.assertEqual(trace.scroll_y, trace.max_scroll_y)

    async def test_trace_pause_counts_new_lines_and_end_resumes_live(self):
        events = [
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": f"trace line {index}"}]
                },
            }
            for index in range(80)
        ]
        self.claude_log.write_text(
            "".join(f"{json.dumps(event)}\n" for event in events)
        )
        app = self.app()
        async with app.run_test(size=(100, 24)) as pilot:
            await self.wait_loaded(app, pilot)
            trace = app.query_one("#trace", TraceView)
            trace.focus()
            for _ in range(120):
                if trace.max_scroll_y > 0 and trace.scroll_y == trace.max_scroll_y:
                    break
                await pilot.pause(0.05)
            self.assertGreater(trace.max_scroll_y, 0)
            self.assertTrue(trace.following)

            await pilot.press("pageup")
            await pilot.pause(0.2)
            self.assertFalse(trace.following)
            paused_at = trace.scroll_y
            self.assertIn(
                "PAUSED",
                render_text(app.query_one("#trace-label", Static)),
            )

            with self.claude_log.open("a") as log:
                for text in ("new line one", "new line two\ncontinued"):
                    log.write(
                        json.dumps(
                            {
                                "type": "assistant",
                                "message": {
                                    "content": [{"type": "text", "text": text}]
                                },
                            }
                        )
                        + "\n"
                    )
            for _ in range(120):
                if trace.unseen_events == 2:
                    break
                await pilot.pause(0.05)
            self.assertEqual(trace.unseen_events, 2)
            await pilot.pause()
            self.assertEqual(trace.scroll_y, paused_at)
            self.assertIn(
                "PAUSED  ·  2 NEW",
                render_text(app.query_one("#trace-label", Static)),
            )

            await pilot.press("end")
            await pilot.pause(0.2)
            self.assertTrue(trace.following)
            self.assertEqual(trace.unseen_events, 0)
            self.assertEqual(trace.scroll_y, trace.max_scroll_y)
            self.assertIn(
                "LIVE",
                render_text(app.query_one("#trace-label", Static)),
            )

    async def test_follow_toggle_survives_redraws_and_resets_on_row_change(self):
        app = self.app()
        async with app.run_test(size=(120, 32)) as pilot:
            await self.wait_loaded(app, pilot)
            trace = app.query_one("#trace", TraceView)
            table = app.query_one("#fleet", DataTable)
            for _ in range(120):
                if (
                    "Need retry ownership" in trace.source_lines
                    and not app._trace_render_pending
                ):
                    break
                await pilot.pause(0.05)
            self.assertIn("Need retry ownership", trace.source_lines)

            await pilot.press("f")
            await pilot.pause()
            self.assertFalse(trace.following)
            trace.show_lines((*trace.source_lines, "unseen event"))
            await pilot.pause()
            self.assertEqual(trace.unseen_events, 1)

            await pilot.press("t")
            await pilot.pause()
            await pilot.press("j", "enter")
            await pilot.pause()
            self.assertFalse(trace.following)
            self.assertEqual(trace.unseen_events, 1)

            await pilot.press("v")
            await pilot.pause(0.2)
            self.assertFalse(trace.following)
            self.assertEqual(trace.unseen_events, 1)

            table.focus()
            await pilot.press("down")
            await pilot.pause(0.2)
            self.assertEqual(app.selected_identity, "working")
            self.assertTrue(trace.following)
            self.assertEqual(trace.unseen_events, 0)

    async def test_f_types_into_filter_instead_of_toggling_trace_follow(self):
        app = self.app()
        async with app.run_test(size=(120, 32)) as pilot:
            await self.wait_loaded(app, pilot)
            trace = app.query_one("#trace", TraceView)
            await pilot.press("slash")
            await pilot.pause()
            await pilot.press("f")
            await pilot.pause()
            self.assertEqual(app.filter_query, "f")
            self.assertTrue(trace.following)

    async def test_trace_cache_reuses_complete_history_without_duplicates(self):
        events = [
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": f"event {index}"}]},
            }
            for index in range(600)
        ]
        self.claude_log.write_text(
            "\n".join(json.dumps(event) for event in events) + "\n"
        )

        app = self.app()
        async with app.run_test(size=(100, 24)) as pilot:
            await self.wait_loaded(app, pilot)
            for _ in range(120):
                if app._trace_model and len(app._trace_model.lines) == 600:
                    break
                await pilot.pause(0.05)

            cached = app._trace_model
            self.assertIsNotNone(cached)
            self.assertEqual(cached.lines[0], "event 0")
            self.assertEqual(cached.lines[-1], "event 599")

            await pilot.press("j")
            await pilot.pause()
            await pilot.press("k")
            await pilot.pause(0.2)

            self.assertIs(app._trace_model, cached)
            self.assertEqual(len(app._trace_model.lines), 600)

    async def test_finished_trace_retention_observes_grace_boundary_and_waits_for_delete(
        self,
    ):
        events = [
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": f"event {index}"}]},
            }
            for index in range(600)
        ]
        self.claude_log.write_text(
            "\n".join(json.dumps(event) for event in events) + "\n"
        )
        now = 1_000_000.0

        app = self.app(clock=lambda: now)
        async with app.run_test(size=(100, 24)) as pilot:
            await self.wait_loaded(app, pilot)
            for _ in range(120):
                if app._trace_model and len(app._trace_model.lines) == 600:
                    break
                await pilot.pause(0.05)

            key = ("parked", str(self.claude_log), "claude")
            cached = app._trace_cache[key]

            payload = json.loads(self.rows_path.read_text())
            payload[0].update(
                status="done",
                derived="finished",
                finished_at=now - FINISHED_TRACE_GRACE_S + 1,
            )
            self.rows_path.write_text(json.dumps(payload))
            app.refresh_snapshot()
            await pilot.pause(0.2)
            self.assertEqual(len(cached.model.lines), 600)
            self.assertEqual(cached.model.lines[0], "event 0")
            self.assertEqual(cached.model.lines[-1], "event 599")

            payload[0]["finished_at"] = now - FINISHED_TRACE_GRACE_S - 1
            self.rows_path.write_text(json.dumps(payload))
            app.refresh_snapshot()
            for _ in range(40):
                if len(cached.model.lines) == FINISHED_TRACE_LINES:
                    break
                await pilot.pause(0.05)
            self.assertEqual(len(cached.model.lines), FINISHED_TRACE_LINES)
            self.assertEqual(cached.model.lines[0], "event 100")
            self.assertEqual(cached.model.lines[-1], "event 599")

            self.rows_path.write_text("[]")
            app.refresh_snapshot()
            for _ in range(40):
                if not app.snapshot.rows:
                    break
                await pilot.pause(0.05)
            self.assertIn(key, app._trace_cache)

    async def test_scrollback_position_is_preserved_while_trace_grows(self):
        events = [
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": f"event {index}"}]},
            }
            for index in range(80)
        ]
        self.claude_log.write_text(
            "\n".join(json.dumps(event) for event in events) + "\n"
        )

        app = self.app()
        async with app.run_test(size=(100, 24)) as pilot:
            await self.wait_loaded(app, pilot)
            trace = app.query_one("#trace", TraceView)
            for _ in range(80):
                if trace.max_scroll_y > 0:
                    break
                await pilot.pause(0.05)
            await pilot.pause(0.2)
            trace.focus()
            await pilot.press("home")
            await pilot.pause()
            top = trace.scroll_y
            self.assertLessEqual(top, 1)
            self.assertLess(top, trace.max_scroll_y)

            fresh = {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "fresh event"}]},
            }
            with self.claude_log.open("a") as stream:
                stream.write(json.dumps(fresh) + "\n")
            for _ in range(40):
                if app._trace_model and app._trace_model.lines[-1] == "fresh event":
                    break
                await pilot.pause(0.05)
            await pilot.pause(0.1)

            self.assertEqual(trace.scroll_y, top)

    async def test_tiny_layout_keeps_signal_and_task_only(self):
        app = self.app()
        async with app.run_test(size=(60, 24)) as pilot:
            await self.wait_loaded(app, pilot)
            table = app.query_one("#fleet", DataTable)
            self.assertEqual(app._layout_mode, "tiny")
            self.assertEqual(len(table.columns), 2)
            self.assertEqual(table.row_count, 2)

    async def test_finished_elapsed_time_freezes_at_run_duration(self):
        now = time.time()
        payload = json.loads(self.rows_path.read_text())
        payload[0].update(
            status="done",
            derived="finished",
            started_at=now - 500,
            finished_at=now - 200,
        )
        self.rows_path.write_text(json.dumps(payload))

        app = self.app()
        async with app.run_test(size=(200, 32)) as pilot:
            await self.wait_loaded(app, pilot)
            table = app.query_one("#fleet", DataTable)
            self.assertEqual(app._layout_mode, "wide")
            self.assertEqual(table.columns["age"].label.plain, "ELAPSED")
            self.assertEqual(table.get_cell("parked", "age"), "5m00")

    async def test_refresh_error_keeps_stale_rows_visible(self):
        app = self.app()
        async with app.run_test(size=(100, 28)) as pilot:
            await self.wait_loaded(app, pilot)
            self.rows_path.write_text('{"rows":[]}')
            app.refresh_snapshot()
            for _ in range(20):
                if app.snapshot.error:
                    break
                await pilot.pause(0.05)
            self.assertEqual(app.query_one("#fleet", DataTable).row_count, 2)
            self.assertIn("array", app.snapshot.error or "")
            self.assertIn("STALE", render_text(app.query_one("#topbar", Static)))

    async def test_stop_requires_second_press_and_refreshes(self):
        app = self.app()
        async with app.run_test(size=(100, 28)) as pilot:
            await self.wait_loaded(app, pilot)
            await pilot.press("down")
            await pilot.pause()
            await pilot.press("s")
            self.assertEqual(app._armed_stop, "working")
            self.assertIn("STOP ARMED", render_text(app.query_one("#notice", Static)))

            completed = mock.Mock(returncode=0, stderr="")
            with mock.patch(
                "eastwatch.fleet.tui.subprocess.run", return_value=completed
            ) as run:
                await pilot.press("s")
                await pilot.pause(0.1)
            self.assertIsNone(app._armed_stop)
            self.assertEqual(
                run.call_args.args[0],
                ["tmux", "kill-session", "-t", "=task-issue-71"],
            )

    async def test_finished_run_stays_attachable_until_x_deletes_it(self):
        payload = json.loads(self.rows_path.read_text())
        payload.append(
            {
                "identity": "finished",
                "key": "task-issue-80",
                "surface": "gitlab",
                "status": "done",
                "derived": "finished",
                "model": "claude:opus",
                "provider": "claude",
                "model_id": "opus",
                "session": "sid-80",
                "tmux_alive": True,
                "log": str(self.claude_log),
                "url": "https://gitlab.example/issues/80",
                "cwd": str(self.root),
                "started_at": time.time() - 20,
                "run_id": "run-80",
            }
        )
        self.rows_path.write_text(json.dumps(payload))

        app = self.app()
        async with app.run_test(size=(120, 32)) as pilot:
            for _ in range(120):
                if app.query_one("#fleet", DataTable).row_count == 3:
                    break
                await pilot.pause(0.05)
            self.assertEqual(app.query_one("#fleet", DataTable).row_count, 3)
            await pilot.press("down", "down")
            self.assertEqual(app.current_row().derived, "finished")

            completed = mock.Mock(returncode=0, stderr="")
            with (
                mock.patch(
                    "eastwatch.fleet.tui.subprocess.run", return_value=completed
                ) as chat,
                mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux"}),
            ):
                await pilot.press("i")
                await pilot.pause(0.1)
            self.assertEqual(
                chat.call_args.args[0],
                [str(self.resume), "finished"],
            )

            with mock.patch(
                "eastwatch.fleet.tui.subprocess.run", return_value=completed
            ) as run:
                await pilot.press("x")
                await pilot.pause(0.1)
            self.assertEqual(
                run.call_args.args[0],
                [str(self.dismiss), "finished", "run-80"],
            )
            self.assertFalse(any(key[0] == "finished" for key in app._trace_cache))

    async def test_invalid_action_explains_why(self):
        app = self.app()
        async with app.run_test(size=(100, 28)) as pilot:
            await self.wait_loaded(app, pilot)
            await pilot.press("a")
            await pilot.pause()
            self.assertIn(
                "needs a live worker",
                render_text(app.query_one("#notice", Static)),
            )

    async def test_hosted_rows_merge_with_local_rows_in_table(self):
        app = self.app(fleet_hosted=self.hosted)
        async with app.run_test(size=(140, 32)) as pilot:
            await self.wait_row_count(app, pilot, 3)
            table = app.query_one("#fleet", DataTable)
            self.assertEqual(table.row_count, 3)
            hosted_row = app.rows_by_id["hosted-62@job-62"]
            self.assertTrue(hosted_row.remote)
            self.assertIn("@alice", table.get_cell("hosted-62@job-62", "task"))
            self.assertIn("hb=", table.get_cell("hosted-62@job-62", "task"))

    async def test_repository_filter_scopes_hosted_rows_and_bypasses_local_scope(self):
        app = self.app(fleet_hosted=self.hosted)
        async with app.run_test(size=(140, 32)) as pilot:
            await self.wait_row_count(app, pilot, 3)

            await pilot.press("g")
            await pilot.pause()
            self.assertIsInstance(app.screen, RepositorySwitcher)
            options = app.screen.query_one("#repo-options", OptionList)
            # All + Cobalt + Orchid + the synthetic Hosted entry.
            self.assertEqual(options.option_count, 4)

            await pilot.press("j", "j", "j", "enter")
            await self.wait_row_count(app, pilot, 1)
            self.assertEqual(app.repository_scope, "hosted")
            self.assertEqual(app.selected_identity, "hosted-62@job-62")

            await pilot.press("g")
            await pilot.pause()
            await pilot.press("k", "k", "enter")
            await pilot.pause()
            self.assertEqual(app.repository_scope, "/git/cobalt/.git")
            # A local repository scope still surfaces hosted rows (they bypass
            # the local filter), so the cobalt-scoped local row plus the
            # hosted row are both visible.
            await self.wait_row_count(app, pilot, 2)
            visible = {row.identity for row in app._visible_rows()}
            self.assertEqual(visible, {"working", "hosted-62@job-62"})

    async def test_hosted_row_key_gating_reflects_capabilities(self):
        self.write_hosted_rows(
            [
                {
                    "identity": "hosted-attach-only",
                    "key": "task-issue-90",
                    "surface": "issue",
                    "status": "leased",
                    "derived": "working",
                    "model": "claude:opus",
                    "provider": "claude",
                    "model_id": "opus",
                    "session": "",
                    "tmux_alive": False,
                    "log": "",
                    "url": "",
                    "cwd": "",
                    "started_at": time.time() - 10,
                    "job_id": "job-90",
                    "owner": "bob",
                    "remote": True,
                    "capabilities": ["attach"],
                    "last_heartbeat_at": time.time() - 2,
                },
            ]
        )
        app = self.app(fleet_hosted=self.hosted)
        async with app.run_test(size=(140, 32)) as pilot:
            await self.wait_row_count(app, pilot, 3)
            for _ in range(6):
                if app.selected_identity == "hosted-attach-only@job-90":
                    break
                await pilot.press("down")
                await pilot.pause()
            self.assertEqual(app.selected_identity, "hosted-attach-only@job-90")

            # Chat is gated off: capabilities has no "resume".
            await pilot.press("i")
            await pilot.pause()
            self.assertIn(
                "needs a parked, crashed, or finished",
                render_text(app.query_one("#notice", Static)),
            )

            # Attach is gated on: capabilities has "attach".
            completed = mock.Mock(returncode=0, stderr="")
            with (
                mock.patch.object(app, "suspend"),
                mock.patch(
                    "eastwatch.fleet.tui.subprocess.run", return_value=completed
                ) as run,
            ):
                await pilot.press("a")
                await pilot.pause(0.1)
            self.assertEqual(run.call_args.args[0], [*BW, "attach", "job-90"])
            self.assertIn(
                "Returned from task-issue-90",
                render_text(app.query_one("#notice", Static)),
            )

    async def test_hosted_rows_for_multiple_runs_of_one_issue_render_uniquely(self):
        base = {
            "key": "task-issue-62",
            "surface": "issue",
            "status": "succeeded",
            "derived": "finished",
            "model": "claude:opus",
            "provider": "claude",
            "model_id": "opus",
            "session": "",
            "tmux_alive": False,
            "log": "",
            "url": "https://gitlab.example/issues/62",
            "cwd": "",
            "owner": "alice",
            "remote": True,
            "capabilities": ["logs"],
        }
        self.write_hosted_rows(
            [
                {
                    **base,
                    "identity": "hosted-62",
                    "job_id": "job-62a",
                    "started_at": time.time() - 40,
                },
                {
                    **base,
                    "identity": "hosted-62",
                    "job_id": "job-62b",
                    "started_at": time.time() - 20,
                },
            ]
        )
        app = self.app(fleet_hosted=self.hosted)
        async with app.run_test(size=(140, 32)) as pilot:
            # Two local rows + two hosted runs of the same issue; a shared
            # issue identity must not collapse or crash the table.
            await self.wait_row_count(app, pilot, 4)
            self.assertIn("hosted-62@job-62a", app.rows_by_id)
            self.assertIn("hosted-62@job-62b", app.rows_by_id)

    async def test_remote_trace_dedupes_replayed_backlog_lines(self):
        # `bw logs --follow --session` is `tail -F` server-side: every tail
        # restart replays its backlog. The trace pane must dedupe those
        # replayed lines instead of rendering them twice.
        duplicate = json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "event A"}]},
            }
        )
        unique = json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "event B"}]},
            }
        )

        async def fake_follow_command_output(argv, on_line, *, cursor=None):
            for raw_line in (duplicate, duplicate, unique):
                await on_line(raw_line)

        app = self.app(fleet_hosted=self.hosted)
        with mock.patch(
            "eastwatch.fleet.tui.follow_command_output",
            fake_follow_command_output,
        ):
            async with app.run_test(size=(140, 32)) as pilot:
                await self.wait_row_count(app, pilot, 3)
                for _ in range(6):
                    if app.selected_identity == "hosted-62@job-62":
                        break
                    await pilot.press("down")
                    await pilot.pause()
                self.assertEqual(app.selected_identity, "hosted-62@job-62")

                for _ in range(40):
                    if app._trace_model and "event B" in app._trace_model.lines:
                        break
                    await pilot.pause(0.05)

                lines = list(app._trace_model.lines)
                self.assertEqual(lines.count("event A"), 1)
                self.assertEqual(lines.count("event B"), 1)

    async def test_partial_line_growth_rewrites_tail_without_rebuild(self):
        # pi's streaming text deltas grow the trace's last line on every
        # flush; that must pop and rewrite one line, never clear() the log —
        # full rebuilds per delta froze the TUI on long transcripts.
        app = self.app()
        async with app.run_test(size=(100, 24)) as pilot:
            await self.wait_loaded(app, pilot)
            trace = app.query_one("#trace", TraceView)
            trace.show_lines(("→ bash git status", "Hel"), force_end=True)
            await pilot.pause()
            with mock.patch.object(trace, "clear", wraps=trace.clear) as tracked_clear:
                trace.show_lines(("→ bash git status", "Hello wor"))
                trace.show_lines(("→ bash git status", "Hello world", "── done"))
                await pilot.pause()
            tracked_clear.assert_not_called()
            self.assertEqual(
                trace.source_lines,
                ("→ bash git status", "Hello world", "── done"),
            )
            rendered = ["".join(seg.text for seg in strip) for strip in trace.lines]
            self.assertEqual(sum("Hel" in line for line in rendered), 1)
            self.assertIn("Hello world", "".join(rendered))

    async def test_rebuild_renders_bounded_tail_of_huge_transcripts(self):
        from eastwatch.fleet.tui import TRACE_RENDER_TAIL_LINES

        app = self.app()
        async with app.run_test(size=(100, 24)) as pilot:
            await self.wait_loaded(app, pilot)
            trace = app.query_one("#trace", TraceView)
            huge = tuple(f"line {i}" for i in range(TRACE_RENDER_TAIL_LINES + 500))
            trace.show_lines(huge, force_end=True)
            await pilot.pause()
            # The full source is retained for append detection, but only the
            # tail is rendered.
            self.assertEqual(trace.source_lines, huge)
            self.assertLessEqual(len(trace.lines), TRACE_RENDER_TAIL_LINES)
            rendered = "".join(
                "".join(seg.text for seg in strip) for strip in trace.lines
            )
            self.assertNotIn("line 0 ", rendered + " ")
            self.assertIn(f"line {len(huge) - 1}", rendered)

            # A subsequent append stays incremental: no rebuild.
            with mock.patch.object(trace, "clear", wraps=trace.clear) as tracked_clear:
                trace.show_lines((*huge, "line new"))
                await pilot.pause()
            tracked_clear.assert_not_called()

    async def test_row_navigation_debounces_tail_subprocess_spawns(self):
        spawns: list[str] = []

        async def fake_follow_command_output(argv, on_line, *, cursor=None):
            spawns.append(" ".join(argv))
            await asyncio.sleep(3600)

        app = self.app(fleet_hosted=self.hosted)
        with mock.patch(
            "eastwatch.fleet.tui.follow_command_output",
            fake_follow_command_output,
        ):
            async with app.run_test(size=(140, 32)) as pilot:
                await self.wait_row_count(app, pilot, 3)
                # Walk down and back up quickly: every highlight changes the
                # selection, but only the row we settle on may spawn a tail.
                await pilot.press("down", "down", "up", "up")
                for _ in range(40):
                    if spawns:
                        break
                    await pilot.pause(0.05)
                await pilot.pause(0.3)
                hosted_spawns = [argv for argv in spawns if "logs" in argv]
                self.assertLessEqual(len(hosted_spawns), 1)

    async def test_dead_tail_surfaces_failure_in_trace_pane(self):
        async def failing_follow_command_output(argv, on_line, *, cursor=None):
            raise RuntimeError("trace follow exited 255: ssh: connect refused")

        app = self.app(fleet_hosted=self.hosted)
        with mock.patch(
            "eastwatch.fleet.tui.follow_command_output",
            failing_follow_command_output,
        ):
            async with app.run_test(size=(140, 32)) as pilot:
                await self.wait_row_count(app, pilot, 3)
                for _ in range(6):
                    if app.selected_identity == "hosted-62@job-62":
                        break
                    await pilot.press("down")
                    await pilot.pause()
                self.assertEqual(app.selected_identity, "hosted-62@job-62")
                await self.wait_trace_contains(app, pilot, "trace tail failed")
                self.assertIn(
                    "connect refused",
                    render_text(app.query_one("#trace", TraceView)),
                )

    async def test_hosted_snapshot_failure_keeps_local_rows_and_sets_notice(self):
        clock_state = {"now": 1_000.0}
        app = self.app(fleet_hosted=self.hosted_fails, clock=lambda: clock_state["now"])
        async with app.run_test(size=(140, 32)) as pilot:
            await self.wait_row_count(app, pilot, 2)

            # Advance past the rate-limit window and force a second hosted fetch
            # so its error notice isn't clobbered by the first local refresh's
            # "Ready" notice. refresh_snapshot is local-only.
            clock_state["now"] += 61
            app.refresh_hosted()
            for _ in range(40):
                if "Hosted" in render_text(app.query_one("#notice", Static)):
                    break
                await pilot.pause(0.05)
            self.assertIn("Hosted", render_text(app.query_one("#notice", Static)))
            self.assertEqual(app.query_one("#fleet", DataTable).row_count, 2)


if __name__ == "__main__":
    unittest.main()
