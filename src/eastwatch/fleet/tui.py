"""Fast operator console for the eastwatch agent fleet."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import ModalScreen
from textual.selection import Selection
from textual.strip import Strip
from textual.theme import Theme
from textual.widgets import DataTable, Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from eastwatch.env import getenv
from eastwatch.fleet.core import (
    Command,
    FleetLog,
    FleetRow,
    FleetSnapshot,
    LogCursor,
    RepositoryIdentity,
    command_argv,
    fetch_snapshot,
    follow_log_file,
    fuzzy_filter_rows,
    preserve_selection,
    resolve_repository,
    resume_eligible,
)
from eastwatch.paths import entrypoint_command

FLEET_STATUS = entrypoint_command("eastwatch.fleet.status", "fleet-status")
FLEET_RESUME = entrypoint_command("eastwatch.fleet.resume", "fleet-resume")
FLEET_DISMISS = entrypoint_command("eastwatch.fleet.dismiss", "fleet-dismiss")
FINISHED_TRACE_GRACE_S = 24 * 60 * 60
FINISHED_TRACE_LINES = 500
ALL_REPOSITORIES = "*"
STATE_PATH = Path(
    getenv("EASTWATCH_STATE_DIR") or str(Path.home() / ".local/state/eastwatch")
) / "state.json"

@dataclass(frozen=True)
class FleetTheme:
    name: str
    label: str
    description: str
    colors: dict[str, str]

    def as_textual_theme(self) -> Theme:
        colors = self.colors
        return Theme(
            name=self.name,
            primary=colors["glacier"],
            secondary=colors["mint"],
            warning=colors["amber"],
            error=colors["coral"],
            success=colors["mint"],
            accent=colors["glacier"],
            foreground=colors["fog"],
            background=colors["void"],
            surface=colors["panel"],
            panel=colors["panel_2"],
            dark=True,
            variables={
                f"fleet-{key.replace('_', '-')}": value
                for key, value in colors.items()
            },
        )


# Palette values come from the upstream theme repositories:
# https://github.com/catppuccin/palette/blob/main/palette.json
# https://github.com/morhetz/gruvbox/blob/master/colors/gruvbox.vim
FLEET_THEMES = {
    "catppuccin-mocha": FleetTheme(
        name="catppuccin-mocha",
        label="Catppuccin Mocha",
        description="Cool pastels on a deep blue-black base",
        colors={
            "void": "#1e1e2e",       # base
            "trace": "#181825",      # mantle
            "panel": "#313244",      # surface0
            "panel_2": "#45475a",    # surface1
            "divider": "#585b70",    # surface2
            "fog": "#cdd6f4",        # text
            "muted": "#6c7086",      # overlay0
            "glacier": "#89b4fa",    # blue
            "amber": "#f9e2af",      # yellow
            "coral": "#f38ba8",      # red
            "mint": "#a6e3a1",       # green
        },
    ),
    "gruvbox-dark-hard": FleetTheme(
        name="gruvbox-dark-hard",
        label="Gruvbox Dark Hard",
        description="Warm retro colors with a hard-contrast base",
        colors={
            "void": "#1d2021",       # dark0_hard
            "trace": "#1d2021",      # dark0_hard
            "panel": "#3c3836",      # dark1
            "panel_2": "#504945",    # dark2
            "divider": "#665c54",    # dark3
            "fog": "#ebdbb2",        # light1
            "muted": "#928374",      # gray
            "glacier": "#8ec07c",    # bright_aqua
            "amber": "#fabd2f",      # bright_yellow
            "coral": "#fb4934",      # bright_red
            "mint": "#b8bb26",       # bright_green
        },
    ),
}
DEFAULT_THEME = "catppuccin-mocha"
COLORS = FLEET_THEMES[DEFAULT_THEME].colors

STATE_SCOPES: dict[str, frozenset[str] | None] = {
    "all": None,
    "needs": frozenset({"crashed", "parked-input"}),
    "running": frozenset({"working", "queued", "finishing"}),
    "review": frozenset({"parked-review"}),
    "finished": frozenset({"finished"}),
}
STATE_SCOPE_LABELS = {
    "all": "ALL",
    "needs": "NEEDS YOU",
    "running": "RUNNING",
    "review": "REVIEW",
    "finished": "FINISHED",
}
STATE_SCOPE_KEYS = {
    "1": "all",
    "2": "needs",
    "3": "running",
    "4": "review",
    "5": "finished",
}

STATE_STYLE = {
    "working": ("●", "amber"),
    "finishing": ("●", "mint"),
    "finished": ("●", "mint"),
    "queued": ("●", "glacier"),
    "crashed": ("●", "coral"),
    "parked-input": ("●", "coral"),
    "parked-review": ("●", "mint"),
}


class Chrome(Static):
    """Non-selectable console chrome, excluded from trace copies."""

    ALLOW_SELECT = False


class FleetTable(DataTable):
    """Fleet navigation table, excluded from trace copies."""

    ALLOW_SELECT = False
    BINDINGS = [
        Binding("j", "cursor_down", "Cursor down", show=False),
        Binding("k", "cursor_up", "Cursor up", show=False),
    ]


class FleetFilterInput(Input):
    """Transient filter editor that reports an explicit cancellation."""

    class Cancelled(Message):
        pass

    BINDINGS = [
        Binding("escape", "cancel", "Clear filter", show=False, priority=True),
    ]

    def action_cancel(self) -> None:
        self.post_message(self.Cancelled())


@dataclass
class TraceCacheEntry:
    model: FleetLog
    cursor: LogCursor
    finished_at: float | None = None
    compacted: bool = False
    pending_events: int = 0


class TraceView(RichLog):
    """Scrollable plain-text trace with explicit live-follow state."""

    class FollowStateChanged(Message):
        def __init__(self, following: bool, unseen_events: int):
            super().__init__()
            self.following = following
            self.unseen_events = unseen_events

    ALLOW_SELECT = True
    can_focus = True

    def __init__(
        self,
        *,
        id: str | None = None,
        colors: dict[str, str] | None = None,
    ):
        super().__init__(
            id=id,
            max_lines=None,
            min_width=1,
            wrap=True,
            auto_scroll=False,
        )
        self.palette = colors or COLORS
        self.source_lines: tuple[str, ...] = ()
        self.following = True
        self.unseen_events = 0

    def _publish_follow_state(self) -> None:
        self.post_message(self.FollowStateChanged(self.following, self.unseen_events))

    def set_following(self, following: bool) -> None:
        changed = (
            following != self.following or following and self.unseen_events > 0
        )
        self.following = following
        if following:
            self.unseen_events = 0
        if changed:
            self._publish_follow_state()

    def resume_following(self) -> None:
        self.set_following(True)
        self.call_after_refresh(
            self.scroll_end,
            animate=False,
            force=True,
            immediate=True,
            x_axis=False,
        )

    def toggle_following(self) -> None:
        if self.following:
            self.set_following(False)
        else:
            self.resume_following()

    def action_scroll_up(self) -> None:
        if self.max_scroll_y:
            self.set_following(False)
        super().action_scroll_up()

    def action_page_up(self) -> None:
        if self.max_scroll_y:
            self.set_following(False)
        super().action_page_up()

    def action_scroll_home(self) -> None:
        if self.max_scroll_y:
            self.set_following(False)
        super().action_scroll_home()

    def action_scroll_end(self) -> None:
        self.set_following(True)
        super().action_scroll_end()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if self.max_scroll_y:
            self.set_following(False)

    def set_colors(self, colors: dict[str, str]) -> None:
        """Apply a palette and recolor the retained trace."""
        self.palette = colors
        lines = self.source_lines
        self.source_lines = ()
        if lines:
            self.show_lines(lines, count_new=False)

    def show_lines(
        self,
        lines: Iterable[str],
        *,
        force_end: bool = False,
        count_new: bool = True,
        new_events: int | None = None,
    ) -> None:
        lines = tuple(lines)
        previous_scroll_y = self.scroll_y
        append_from = (
            len(self.source_lines)
            if len(lines) >= len(self.source_lines)
            and lines[: len(self.source_lines)] == self.source_lines
            else None
        )
        appended = len(lines) - append_from if append_from is not None else 0
        rebuilt = append_from is None
        if force_end:
            self.set_following(True)
        if append_from is None:
            self.clear()
            append_from = 0
        for line in lines[append_from:]:
            style = self.palette["fog"]
            if line.startswith("→"):
                style = self.palette["glacier"]
            elif line.startswith("·"):
                style = self.palette["muted"]
            elif line.startswith("──"):
                style = self.palette["mint"]
            self.write(Text(line, style=style), scroll_end=False)
        self.source_lines = lines
        unseen = appended if new_events is None else new_events
        if not self.following and count_new and unseen:
            self.unseen_events += unseen
            self._publish_follow_state()
        if self.following:
            self.call_after_refresh(
                self.scroll_end,
                animate=False,
                force=True,
                immediate=True,
                x_axis=False,
            )
        elif rebuilt:
            self.call_after_refresh(
                self.scroll_to,
                y=previous_scroll_y,
                animate=False,
                force=True,
                immediate=True,
            )

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        if not self.lines:
            return None
        text = "\n".join(line.text for line in self.lines)
        return selection.extract(text), "\n"

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        source_y = self.scroll_offset.y + y
        selection = self.text_selection
        selected_span = selection.get_span(source_y) if selection is not None else None
        source_x = 0
        segments: list[Segment] = []
        for text, style, control in strip:
            segment_end = source_x + len(text)
            cuts = {source_x, segment_end}
            if selected_span is not None:
                selected_start, selected_end = selected_span
                cuts.add(max(source_x, min(segment_end, selected_start)))
                cuts.add(max(source_x, min(segment_end, selected_end)))
            ordered_cuts = sorted(cuts)
            for start, end in zip(ordered_cuts, ordered_cuts[1:]):
                part_style = style or Style()
                if (
                    selected_span is not None
                    and start < selected_span[1]
                    and end > selected_span[0]
                ):
                    part_style += self.selection_style
                part_style += Style.from_meta({"offset": (start, source_y)})
                segments.append(
                    Segment(
                        text[start - source_x : end - source_x],
                        part_style,
                        control,
                    )
                )
            source_x += len(text)
        return Strip(segments, strip.cell_length)


class ThemeOptionList(OptionList):
    """Theme choices with the same vim navigation as the fleet table."""

    BINDINGS = [
        Binding("j", "cursor_down", "Cursor down", show=False),
        Binding("k", "cursor_up", "Cursor up", show=False),
    ]


class ThemeSwitcher(ModalScreen[str | None]):
    """Keyboard-first modal for choosing a fleet theme."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("t", "cancel", "Cancel", show=False),
    ]

    def __init__(self, current_theme: str):
        super().__init__()
        self.current_theme = current_theme

    def compose(self) -> ComposeResult:
        with Vertical(id="theme-dialog"):
            yield Static("THEME", id="theme-title")
            yield ThemeOptionList(
                *(
                    Option(
                        Text.assemble(
                            ("●  ", theme.colors["glacier"]),
                            (theme.label, "bold"),
                            (f"\n   {theme.description}", "dim"),
                        ),
                        id=theme.name,
                    )
                    for theme in FLEET_THEMES.values()
                ),
                id="theme-options",
            )
            yield Static("enter apply  ·  esc cancel", id="theme-help")

    def on_mount(self) -> None:
        options = self.query_one("#theme-options", OptionList)
        options.highlighted = options.get_option_index(self.current_theme)
        options.focus()

    def on_option_list_option_selected(
        self,
        event: OptionList.OptionSelected,
    ) -> None:
        self.dismiss(event.option_id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class RepositorySwitcher(ModalScreen[str | None]):
    """Keyboard-first repository scope selector."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("g", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self,
        repositories: tuple[tuple[RepositoryIdentity, int], ...],
        current_repository: str,
    ):
        super().__init__()
        self.repositories = repositories
        self.current_repository = current_repository
        self.option_values = {
            "repo-all": ALL_REPOSITORIES,
            **{
                f"repo-{index}": repository.key
                for index, (repository, _) in enumerate(repositories)
            },
        }

    def compose(self) -> ComposeResult:
        options = [
            Option("All repositories", id="repo-all"),
            *(
                Option(
                    Text.assemble(
                        (repository.label, "bold"),
                        (f"  ·  {count} worker{'s' if count != 1 else ''}", "dim"),
                    ),
                    id=f"repo-{index}",
                )
                for index, (repository, count) in enumerate(self.repositories)
            ),
        ]
        with Vertical(id="repo-dialog"):
            yield Static("REPOSITORY", id="repo-title")
            yield ThemeOptionList(*options, id="repo-options")
            yield Static("enter apply  ·  esc cancel", id="repo-help")

    def on_mount(self) -> None:
        options = self.query_one("#repo-options", OptionList)
        selected_id = next(
            (
                option_id
                for option_id, value in self.option_values.items()
                if value == self.current_repository
            ),
            "repo-all",
        )
        options.highlighted = options.get_option_index(selected_id)
        options.focus()

    def on_option_list_option_selected(
        self,
        event: OptionList.OptionSelected,
    ) -> None:
        self.dismiss(self.option_values[str(event.option_id)])

    def action_cancel(self) -> None:
        self.dismiss(None)


class FleetApp(App):
    TITLE = "eastwatch fleet"
    ENABLE_COMMAND_PALETTE = False
    ALLOW_SELECT = True

    CSS = """
    Screen {
        background: $fleet-void;
        color: $fleet-fog;
    }

    #shell {
        height: 100%;
        background: $fleet-void;
    }

    #workbench {
        height: 1fr;
        layout: horizontal;
        overflow: hidden hidden;
    }

    #filter {
        display: none;
        height: 1;
        padding: 0 2;
        background: $fleet-panel-2;
        color: $fleet-fog;
        border: none;
    }

    #filter.visible {
        display: block;
    }

    #filter:focus {
        border: none;
    }

    #topbar {
        height: 1;
        padding: 0 2;
        background: $fleet-panel;
        color: $fleet-fog;
        overflow: hidden hidden;
    }

    #fleet {
        width: 1fr;
        height: 1fr;
        margin: 0;
        background: $fleet-void;
        color: $fleet-fog;
        border: none;
        overflow-x: hidden;
        scrollbar-color: $fleet-muted;
        scrollbar-color-hover: $fleet-glacier;
        scrollbar-color-active: $fleet-glacier;
        scrollbar-background: $fleet-void;
    }

    #fleet:focus {
        border: none;
    }

    #split {
        width: 1;
        height: 100%;
        background: $fleet-divider;
    }

    #trace-pane {
        width: 55%;
        height: 100%;
        background: $fleet-trace;
    }

    #trace-label {
        height: 1;
        width: 100%;
        padding: 0 1;
        background: $fleet-panel-2;
        color: $fleet-glacier;
        text-style: bold;
    }

    #trace {
        width: 100%;
        height: 1fr;
        min-height: 5;
        padding: 0 1 1 1;
        background: $fleet-trace;
        color: $fleet-fog;
        overflow-x: hidden;
        overflow-y: auto;
        scrollbar-color: $fleet-muted;
        scrollbar-background: $fleet-trace;
    }

    #trace:focus {
        background-tint: $fleet-glacier 4%;
    }

    #notice {
        height: 1;
        padding: 0 2;
        background: $fleet-panel;
        color: $fleet-muted;
    }

    #notice.error {
        color: $fleet-coral;
    }

    #notice.armed {
        background: $fleet-coral;
        color: $fleet-void;
        text-style: bold;
    }

    #keys {
        height: 1;
        padding: 0 2;
        background: $fleet-panel-2;
        color: $fleet-muted;
    }

    ThemeSwitcher {
        align: center middle;
        background: $fleet-void 72%;
    }

    #theme-dialog {
        width: 52;
        height: auto;
        max-height: 16;
        padding: 1 2;
        background: $fleet-panel;
        border: round $fleet-divider;
    }

    #theme-title {
        height: 1;
        margin-bottom: 1;
        color: $fleet-fog;
        text-style: bold;
    }

    #theme-options {
        height: 6;
        background: $fleet-panel;
        color: $fleet-fog;
        border: none;
        scrollbar-size: 0 0;
    }

    #theme-help {
        height: 1;
        margin-top: 1;
        color: $fleet-muted;
        text-align: right;
    }

    RepositorySwitcher {
        align: center middle;
        background: $fleet-void 72%;
    }

    #repo-dialog {
        width: 58;
        height: auto;
        max-height: 22;
        padding: 1 2;
        background: $fleet-panel;
        border: round $fleet-divider;
    }

    #repo-title {
        height: 1;
        margin-bottom: 1;
        color: $fleet-fog;
        text-style: bold;
    }

    #repo-options {
        height: auto;
        max-height: 12;
        background: $fleet-panel;
        color: $fleet-fog;
        border: none;
        scrollbar-size: 0 0;
    }

    #repo-help {
        height: 1;
        margin-top: 1;
        color: $fleet-muted;
        text-align: right;
    }
    """

    BINDINGS = [
        Binding("a", "attach", "Attach", show=False),
        Binding("i", "chat", "Chat", show=False),
        Binding("o", "open", "Open", show=False),
        Binding("r", "refresh_now", "Refresh", show=False),
        Binding("s", "stop_worker", "Stop", show=False),
        Binding("t", "theme_switcher", "Theme", show=False),
        Binding("g", "repository_switcher", "Repository", show=False),
        Binding("f", "toggle_trace_follow", "Toggle trace follow", show=False),
        Binding("v", "toggle_trace_layout", "Toggle trace layout", show=False),
        Binding("/", "focus_filter", "Filter", show=False),
        Binding("0", "digit('0')", "Type 0", show=False, priority=True),
        Binding("1", "digit('1')", "All states", show=False, priority=True),
        Binding("2", "digit('2')", "Needs you", show=False, priority=True),
        Binding("3", "digit('3')", "Running", show=False, priority=True),
        Binding("4", "digit('4')", "Review", show=False, priority=True),
        Binding("5", "digit('5')", "Finished", show=False, priority=True),
        Binding("6", "digit('6')", "Type 6", show=False, priority=True),
        Binding("7", "digit('7')", "Type 7", show=False, priority=True),
        Binding("8", "digit('8')", "Type 8", show=False, priority=True),
        Binding("9", "digit('9')", "Type 9", show=False, priority=True),
        Binding("x", "delete_finished", "Delete finished", show=False),
        Binding("[", "widen_trace", "Widen trace", show=False),
        Binding("]", "narrow_trace", "Narrow trace", show=False),
        Binding("?", "help", "Keys", show=False),
        Binding("q", "quit", "Quit", show=False),
    ]

    def get_theme_variable_defaults(self) -> dict[str, str]:
        return FLEET_THEMES[DEFAULT_THEME].as_textual_theme().variables

    @property
    def colors(self) -> dict[str, str]:
        return FLEET_THEMES[self.theme].colors

    def __init__(
        self,
        *,
        fleet_status: Command = FLEET_STATUS,
        fleet_resume: Command = FLEET_RESUME,
        fleet_dismiss: Command = FLEET_DISMISS,
        state_path: Path = STATE_PATH,
        poll_interval_s: float = 2.0,
        repository_resolver: Callable[[str], RepositoryIdentity] = resolve_repository,
        clock: Callable[[], float] = time.time,
    ):
        super().__init__()
        for fleet_theme in FLEET_THEMES.values():
            self.register_theme(fleet_theme.as_textual_theme())
        self.theme = DEFAULT_THEME
        self.fleet_status = fleet_status
        self.fleet_resume = fleet_resume
        self.fleet_dismiss = fleet_dismiss
        self.state_path = state_path
        self.poll_interval_s = poll_interval_s
        self.repository_resolver = repository_resolver
        self.clock = clock
        self.snapshot = FleetSnapshot((), time.time(), None)
        self.rows_by_id: dict[str, FleetRow] = {}
        self.selected_identity: str | None = None
        self.filter_query = ""
        self._filter_editing = False
        self.state_scope = "all"
        self.repository_scope = ALL_REPOSITORIES
        self._repository_cache: dict[str, RepositoryIdentity] = {}
        self._row_repositories: dict[str, RepositoryIdentity] = {}
        self._repositories_by_key: dict[str, RepositoryIdentity] = {}
        self._layout_mode = ""
        self._configured_table_width = 0
        self.trace_layout = "right"
        self._trace_width_percent = 55
        self._trace_height_percent = 45
        self._rebuilding = False
        self._rendered_order: tuple[str, ...] = ()
        self._rendered_cells: dict[str, tuple[object, ...]] = {}
        self._tail_identity: str | None = None
        self._tail_log_path: str | None = None
        self._trace_model: FleetLog | None = None
        self._trace_cursor: LogCursor | None = None
        self._trace_entry: TraceCacheEntry | None = None
        self._trace_cache: dict[tuple[str, str, str], TraceCacheEntry] = {}
        self._trace_render_pending = False
        self._notice_generation = 0
        self._initial_refresh_done = False
        self._refresh_running = False
        self._armed_stop: str | None = None
        self._armed_generation = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="shell"):
            yield Chrome(id="topbar")
            yield FleetFilterInput(placeholder="Filter fleet…", id="filter")
            with Horizontal(id="workbench"):
                yield FleetTable(
                    id="fleet",
                    cursor_type="row",
                    zebra_stripes=True,
                )
                yield Chrome(id="split")
                with Vertical(id="trace-pane"):
                    yield Chrome("TRACE", id="trace-label")
                    yield TraceView(id="trace", colors=self.colors)
            yield Chrome("Starting fleet monitor…", id="notice")
            yield Chrome(id="keys")

    def on_mount(self) -> None:
        self._apply_trace_width()
        self._configure_table(force=True)
        self._render_header()
        self._render_keys()
        self.query_one("#fleet", DataTable).focus()
        self.refresh_snapshot()
        self.set_interval(self.poll_interval_s, self.refresh_snapshot)

    def on_unmount(self) -> None:
        self._notice_generation += 1
        self._armed_generation += 1
        self._tail_identity = None
        self._tail_log_path = None
        self._trace_model = None
        self._trace_cursor = None
        self._trace_entry = None
        self._trace_render_pending = False
        self.workers.cancel_group(self, "tail")
        self.workers.cancel_group(self, "snapshot")

    def copy_to_clipboard(self, text: str) -> None:
        """Copy selected trace text, with a native macOS clipboard fallback."""
        super().copy_to_clipboard(text)
        if sys.platform == "darwin":
            try:
                subprocess.run(
                    ["pbcopy"],
                    input=text,
                    text=True,
                    timeout=2,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass

    def on_resize(self, event: events.Resize) -> None:
        self.call_after_refresh(self._configure_table)
        self._render_header()
        self._render_keys()

    def action_focus_filter(self) -> None:
        filter_input = self.query_one("#filter", FleetFilterInput)
        self._filter_editing = True
        filter_input.add_class("visible")
        filter_input.focus()
        filter_input.cursor_position = len(filter_input.value)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "filter":
            return
        query = event.value.strip()
        if query == self.filter_query:
            return
        self.filter_query = query
        self._apply_row_filters()
        event.input.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "filter":
            return
        self._filter_editing = False
        event.input.remove_class("visible")
        self.query_one("#fleet", DataTable).focus()

    def on_fleet_filter_input_cancelled(
        self,
        event: FleetFilterInput.Cancelled,
    ) -> None:
        event.stop()
        filter_input = self.query_one("#filter", FleetFilterInput)
        self._filter_editing = False
        filter_input.remove_class("visible")
        filter_input.value = ""
        self.query_one("#fleet", DataTable).focus()
        if self.filter_query:
            self.filter_query = ""
            self._apply_row_filters()

    def _repository_for_cwd(self, cwd: str) -> RepositoryIdentity:
        repository = self._repository_cache.get(cwd)
        if repository is None:
            repository = self.repository_resolver(cwd)
            self._repository_cache[cwd] = repository
        return repository

    def _repository_rows(self) -> tuple[FleetRow, ...]:
        if self.repository_scope == ALL_REPOSITORIES:
            return self.snapshot.rows
        return tuple(
            row
            for row in self.snapshot.rows
            if (repository := self._row_repositories.get(row.identity)) is not None
            and repository.key == self.repository_scope
        )

    def _scoped_rows(self) -> tuple[FleetRow, ...]:
        rows = self._repository_rows()
        states = STATE_SCOPES[self.state_scope]
        if states is None:
            return rows
        return tuple(row for row in rows if row.derived in states)

    def _visible_rows(self) -> tuple[FleetRow, ...]:
        extra_values = {
            row.identity: (self._row_repositories[row.identity].label,)
            for row in self.snapshot.rows
            if row.identity in self._row_repositories
        }
        return fuzzy_filter_rows(
            self._scoped_rows(),
            self.filter_query,
            extra_values=extra_values,
        )

    def _repository_label(self, key: str | None = None) -> str:
        key = self.repository_scope if key is None else key
        if key == ALL_REPOSITORIES:
            return "All"
        repository = self._repositories_by_key.get(key)
        return repository.label if repository is not None else "Unknown"

    def _repository_choices(self) -> tuple[tuple[RepositoryIdentity, int], ...]:
        counts: dict[str, int] = {}
        for repository in self._row_repositories.values():
            counts[repository.key] = counts.get(repository.key, 0) + 1
        if self.repository_scope != ALL_REPOSITORIES:
            counts.setdefault(self.repository_scope, 0)
        repositories = (
            self._repositories_by_key[key]
            for key in counts
            if key in self._repositories_by_key
        )
        return tuple(
            (repository, counts[repository.key])
            for repository in sorted(
                repositories,
                key=lambda item: (item.label.casefold(), item.key),
            )
        )

    def _apply_row_filters(self) -> None:
        previous = self.selected_identity
        self.selected_identity = preserve_selection(self._visible_rows(), previous)
        self._render_rows()
        if self.selected_identity != previous:
            self._clear_stop_arm()
            self._start_selected_tail()
        self._render_header()
        self._render_keys()

    def _set_state_scope(self, scope: str) -> None:
        if scope == self.state_scope:
            return
        self.state_scope = scope
        self._apply_row_filters()
        label = STATE_SCOPE_LABELS[scope].title()
        self._set_notice(f"State scope · {label} · {len(self._visible_rows())} visible")

    def action_repository_switcher(self) -> None:
        self.push_screen(
            RepositorySwitcher(
                self._repository_choices(),
                self.repository_scope,
            ),
            self._apply_repository_choice,
        )

    def _apply_repository_choice(self, repository_key: str | None) -> None:
        if repository_key is None or repository_key == self.repository_scope:
            return
        self.repository_scope = repository_key
        self._apply_row_filters()
        label = self._repository_label()
        self._set_notice(f"Repository scope · {label} · {len(self._visible_rows())} visible")

    def action_digit(self, digit: str) -> None:
        if self._filter_editing:
            filter_input = self.query_one("#filter", FleetFilterInput)
            filter_input.insert_text_at_cursor(digit)
            filter_input.focus()
            return
        scope = STATE_SCOPE_KEYS.get(digit)
        if scope is not None:
            self._set_state_scope(scope)

    def _fleet_width(self) -> int:
        if self.is_mounted:
            width = self.query_one("#fleet", DataTable).size.width
            if width:
                return width
        return max(1, int(self.size.width * (100 - self._trace_width_percent) / 100) - 1)

    def _mode_for_width(self, width: int | None = None) -> str:
        width = self._fleet_width() if width is None else width
        if width < 38:
            return "tiny"
        if width < 72:
            return "compact"
        return "wide"

    def _configure_table(self, *, force: bool = False) -> None:
        table_width = self._fleet_width()
        mode = self._mode_for_width(table_width)
        if (
            not force
            and mode == self._layout_mode
            and table_width == self._configured_table_width
        ):
            return
        self._layout_mode = mode
        self._configured_table_width = table_width
        table = self.query_one("#fleet", DataTable)
        table.clear(columns=True)
        self._rendered_order = ()
        self._rendered_cells.clear()
        table.add_column("", width=2, key="signal")
        content_width = max(8, table_width - 4)
        if mode == "tiny":
            table.add_column("TASK", width=max(4, content_width - 6), key="task")
        else:
            table.add_column("STATE", width=14, key="state")
            if mode == "compact":
                table.add_column("TASK", width=max(8, content_width - 22), key="task")
            else:
                table.add_column("TASK", width=max(12, content_width - 52), key="task")
                table.add_column("MODEL", width=18, key="model")
                table.add_column("ELAPSED", width=8, key="age")
        self._render_rows(force_rebuild=True)

    @work(group="snapshot")
    async def refresh_snapshot(self) -> None:
        if self._refresh_running:
            return
        self._refresh_running = True
        try:
            await self._refresh_snapshot_once()
        finally:
            self._refresh_running = False

    async def _refresh_snapshot_once(self) -> None:
        snapshot = await fetch_snapshot(
            self.fleet_status,
            timeout_s=6.0,
            state_path=self.state_path,
        )
        if snapshot.error:
            self.snapshot = FleetSnapshot(
                self.snapshot.rows,
                self.snapshot.refreshed_at,
                snapshot.heartbeat_age_s,
                snapshot.error,
            )
            self._set_notice(snapshot.error, tone="error", timeout_s=5)
        else:
            previous = self.selected_identity
            self.snapshot = snapshot
            self.rows_by_id = {row.identity: row for row in snapshot.rows}
            self._row_repositories = {
                row.identity: self._repository_for_cwd(row.cwd)
                for row in snapshot.rows
            }
            for repository in self._row_repositories.values():
                self._repositories_by_key[repository.key] = repository
            for row in snapshot.rows:
                entry = self._trace_cache.get((row.identity, row.log, row.provider))
                if entry is not None:
                    self._apply_trace_retention(row, entry)
            self.selected_identity = preserve_selection(self._visible_rows(), previous)
            self._render_rows()
            selected = self.current_row()
            if (
                self.selected_identity != previous
                or selected is None
                or selected.log != self._tail_log_path
                or selected.provider != (self._trace_model.provider if self._trace_model else None)
            ):
                self._start_selected_tail()
            if not self._initial_refresh_done:
                self._set_notice("Ready", timeout_s=0)
                self._initial_refresh_done = True
        self._render_header()
        self._render_keys()

    def _column_keys(self) -> tuple[str, ...]:
        if self._layout_mode == "tiny":
            return ("signal", "task")
        if self._layout_mode == "compact":
            return ("signal", "state", "task")
        return ("signal", "state", "task", "model", "age")

    def _render_rows(self, *, force_rebuild: bool = False) -> None:
        if not self.is_mounted:
            return
        table = self.query_one("#fleet", DataTable)
        rows = self._visible_rows()
        selected = self.selected_identity
        order = tuple(row.identity for row in rows)
        rebuild = (
            force_rebuild
            or order != self._rendered_order
            or table.row_count != len(rows)
        )
        self._rebuilding = True
        try:
            if rebuild:
                table.clear()
                for row in rows:
                    cells = self._row_cells(row)
                    table.add_row(*cells, key=row.identity)
                    self._rendered_cells[row.identity] = cells
                if selected:
                    for index, row in enumerate(rows):
                        if row.identity == selected:
                            table.move_cursor(row=index)
                            break
            else:
                column_keys = self._column_keys()
                for row in rows:
                    cells = self._row_cells(row)
                    if cells == self._rendered_cells.get(row.identity):
                        continue
                    for column_key, value in zip(column_keys, cells, strict=True):
                        table.update_cell(row.identity, column_key, value)
                    self._rendered_cells[row.identity] = cells
        finally:
            self._rebuilding = False
        self._rendered_order = order
        if rebuild:
            self._rendered_cells = {
                row.identity: self._row_cells(row)
                for row in rows
            }
        if not rows:
            self.query_one("#trace-label", Static).update("TRACE")
            if self.filter_query:
                message = f"No conversations match /{self.filter_query}"
            elif (
                self.repository_scope != ALL_REPOSITORIES
                and not self._repository_rows()
            ):
                message = f"No conversations in repository {self._repository_label()}"
            elif self.state_scope != "all":
                label = STATE_SCOPE_LABELS[self.state_scope].title()
                message = f"No conversations in {label}"
            else:
                message = "No active conversations. Waiting for next cycle…"
            self.query_one("#trace", TraceView).show_lines((message,))

    def _row_cells(self, row: FleetRow) -> tuple[object, ...]:
        symbol, color_name = STATE_STYLE.get(row.derived, ("○", "muted"))
        color = self.colors[color_name]
        signal = Text(symbol, style=f"bold {color}")
        state = Text(row.derived.upper(), style=color)
        task = row.key.removeprefix("task-")
        if self._layout_mode == "tiny":
            return signal, task
        if self._layout_mode == "compact":
            return signal, state, task
        return signal, state, task, row.model, self._format_elapsed(row.started_at, row.finished_at)

    @staticmethod
    def _format_elapsed(started_at: float | None, finished_at: float | None) -> str:
        if not started_at:
            return "—"
        ended_at = finished_at if finished_at is not None else time.time()
        seconds = max(0, int(ended_at - started_at))
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60}m{seconds % 60:02d}"
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}"

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if self._rebuilding or not event.row_key:
            return
        identity = str(event.row_key.value)
        if identity == self.selected_identity:
            return
        self.selected_identity = identity
        self._clear_stop_arm()
        self._start_selected_tail()
        self._render_keys()

    def current_row(self) -> FleetRow | None:
        if not self.selected_identity:
            return None
        return self.rows_by_id.get(self.selected_identity)

    def _start_selected_tail(self) -> None:
        row = self.current_row()
        if not row:
            self._tail_identity = None
            self._tail_log_path = None
            self._trace_model = None
            self._trace_cursor = None
            self._trace_entry = None
            self.workers.cancel_group(self, "tail")
            return
        self._tail_identity = row.identity
        self._tail_log_path = row.log
        cache_key = (row.identity, row.log, row.provider)
        entry = self._trace_cache.get(cache_key)
        if entry is None:
            entry = TraceCacheEntry(
                FleetLog(row.provider, max_lines=None, max_chars=None),
                LogCursor(),
            )
            self._trace_cache[cache_key] = entry
        self._apply_trace_retention(row, entry)
        self._trace_model = entry.model
        self._trace_cursor = entry.cursor
        self._trace_entry = entry
        entry.pending_events = 0
        self._trace_render_pending = False
        self.query_one("#trace", TraceView).set_following(True)
        self._render_trace_label(row)
        if not row.log:
            self.query_one("#trace", TraceView).show_lines(
                ("No retained run trace for this conversation.",),
                force_end=True,
            )
            self.workers.cancel_group(self, "tail")
            return
        lines = entry.model.lines
        self.query_one("#trace", TraceView).show_lines(
            lines or ("Loading trace…",),
            force_end=True,
        )
        self.follow_selected_log(row, entry)

    @work(exclusive=True, group="tail")
    async def follow_selected_log(self, row: FleetRow, entry: TraceCacheEntry) -> None:
        model = entry.model

        async def consume(raw_line: str) -> None:
            if row.identity != self._tail_identity or model is not self._trace_model:
                return
            if model.feed_line(raw_line):
                entry.pending_events += 1
                self._schedule_trace_render()

        await follow_log_file(
            Path(row.log),
            consume,
            initial_bytes=None,
            cursor=entry.cursor,
        )

    def _apply_trace_retention(
        self,
        row: FleetRow,
        entry: TraceCacheEntry,
    ) -> None:
        if row.derived != "finished":
            return
        now = self.clock()
        if row.finished_at is not None:
            entry.finished_at = row.finished_at
        elif entry.finished_at is None:
            entry.finished_at = now
        if (
            not entry.compacted
            and entry.finished_at is not None
            and now - entry.finished_at >= FINISHED_TRACE_GRACE_S
        ):
            entry.model.set_limits(
                max_lines=FINISHED_TRACE_LINES,
                max_chars=None,
            )
            entry.compacted = True
            if entry.model is self._trace_model:
                self._schedule_trace_render()

    def _schedule_trace_render(self) -> None:
        if self._trace_render_pending:
            return
        self._trace_render_pending = True
        self.set_timer(0.04, self._flush_trace)

    def _flush_trace(self) -> None:
        self._trace_render_pending = False
        if self._trace_model is not None:
            lines = self._trace_model.lines or ("Waiting for next event…",)
            entry = self._trace_entry
            new_events = (
                entry.pending_events
                if entry is not None and entry.model is self._trace_model
                else 0
            )
            if entry is not None:
                entry.pending_events = 0
            try:
                self.query_one("#trace", TraceView).show_lines(
                    lines,
                    new_events=new_events,
                )
            except NoMatches:
                pass

    def on_trace_view_follow_state_changed(
        self,
        event: TraceView.FollowStateChanged,
    ) -> None:
        row = self.current_row()
        if row is not None:
            self._render_trace_label(row)
        self._render_keys()

    def _render_trace_label(self, row: FleetRow) -> None:
        colors = self.colors
        trace = self.query_one("#trace", TraceView)
        if trace.following:
            follow_label = "LIVE"
            follow_color = colors["mint"]
        else:
            follow_label = "PAUSED"
            if trace.unseen_events:
                follow_label += f"  ·  {trace.unseen_events} NEW"
            follow_color = colors["amber"]
        self.query_one("#trace-label", Static).update(
            Text.assemble(
                ("TRACE", f"bold {colors['glacier']}"),
                (f"  ·  {row.key.removeprefix('task-')}", colors["fog"]),
                (f"  ·  {row.model}", colors["muted"]),
                (f"  ·  {follow_label}", f"bold {follow_color}"),
            )
        )

    def _render_header(self) -> None:
        rows = self.snapshot.rows
        repository_rows = self._repository_rows()
        scoped_rows = self._scoped_rows()
        visible_rows = self._visible_rows()
        repository_status = (
            f"   REPO {self._repository_label()} {len(repository_rows)}"
        )
        scope_status = (
            f"   {STATE_SCOPE_LABELS[self.state_scope]} {len(scoped_rows)}"
        )
        filter_status = (
            f"   {len(visible_rows)}/{len(rows)} VISIBLE  ·  /{self.filter_query}"
            if self.filter_query
            else ""
        )
        needs = sum(row.derived in {"crashed", "parked-input"} for row in rows)
        working = sum(row.derived == "working" for row in rows)
        review = sum(row.derived == "parked-review" for row in rows)
        age = self.snapshot.heartbeat_age_s
        stale = age is None or age > 180
        health = "NO CYCLE" if age is None else f"CYCLE {int(age)}s"
        if self.snapshot.error:
            health = f"FEED STALE  ·  {health}"
        colors = self.colors
        health_color = colors["coral"] if stale else colors["mint"]

        left_width = (
            len("FLEET")
            + len(f"   {len(rows)} ACTIVE")
            + len(repository_status)
            + len(scope_status)
            + len(filter_status)
            + len(f"  ·  {needs} NEED YOU")
            + len(f"   ● {working} RUNNING")
            + len(f"   ● {review} REVIEW")
        )
        gap = max(2, self.size.width - left_width - len(health) - 4)
        header = Text()
        header.append("FLEET", style=f"bold {colors['fog']}")
        header.append(f"   {len(rows)} ACTIVE", style=colors["glacier"])
        header.append(repository_status, style=colors["glacier"])
        header.append(scope_status, style=f"bold {colors['fog']}")
        if filter_status:
            header.append(filter_status, style=colors["glacier"])
        header.append(
            f"  ·  {needs} NEED YOU",
            style=colors["coral"] if needs else colors["muted"],
        )
        header.append(f"   ● {working} RUNNING", style=colors["amber"])
        header.append(f"   ● {review} REVIEW", style=colors["mint"])
        header.append(" " * gap)
        header.append(health, style=f"bold {health_color}")
        self.query_one("#topbar", Static).update(header)

    def _render_keys(self) -> None:
        row = self.current_row()
        compact = self.size.width < 120
        actions = [
            ("a", "attach", bool(row and row.tmux_alive)),
            ("i", "chat", bool(row and resume_eligible(row))),
            ("o", "open", bool(row and row.url)),
            ("r", "refresh", True),
            ("/", "filter", True),
            (
                "1-5",
                f"states:{self.state_scope}({len(self._scoped_rows())})",
                True,
            ),
            ("t", "theme", True),
            (
                "g",
                f"repo:{self._repository_label().casefold()}({len(self._repository_rows())})",
                True,
            ),
            ("v", f"layout:{self.trace_layout}", True),
            (
                "f",
                "follow:live"
                if self.query_one("#trace", TraceView).following
                else "follow:paused",
                True,
            ),
            (
                "s",
                "stop",
                bool(
                    row
                    and row.tmux_alive
                    and row.derived in {"working", "queued"}
                ),
            ),
            ("x", "delete", bool(row and row.derived == "finished")),
            ("[", "widen", True),
            ("]", "narrow", True),
            ("?", "keys", True),
            ("q", "quit", True),
        ]
        if compact:
            actions = [(key, "", enabled) for key, _, enabled in actions]
        footer = Text()
        colors = self.colors
        for index, (key, label, enabled) in enumerate(actions):
            if index:
                footer.append("   ", style=colors["muted"])
            style = colors["fog"] if enabled else colors["muted"]
            footer.append(key, style=f"bold {style}")
            if label:
                footer.append(f" {label}", style=style)
        self.query_one("#keys", Static).update(footer)

    def action_toggle_trace_follow(self) -> None:
        if self._filter_editing:
            filter_input = self.query_one("#filter", FleetFilterInput)
            filter_input.insert_text_at_cursor("f")
            filter_input.focus()
            return
        self.query_one("#trace", TraceView).toggle_following()

    def action_theme_switcher(self) -> None:
        self.push_screen(ThemeSwitcher(self.theme), self._apply_theme_choice)

    def _apply_theme_choice(self, theme_name: str | None) -> None:
        if theme_name is None or theme_name == self.theme:
            return
        fleet_theme = FLEET_THEMES.get(theme_name)
        if fleet_theme is None:
            self._set_notice(f"Unknown theme: {theme_name}", tone="error")
            return
        self.theme = theme_name
        self.query_one("#trace", TraceView).set_colors(fleet_theme.colors)
        self._render_rows(force_rebuild=True)
        row = self.current_row()
        if row is not None:
            self._render_trace_label(row)
        self._render_header()
        self._render_keys()
        self._set_notice(f"Theme · {fleet_theme.label}")

    def _apply_trace_width(self) -> None:
        if not self.is_mounted:
            return
        workbench = self.query_one("#workbench", Horizontal)
        divider = self.query_one("#split", Chrome)
        trace_pane = self.query_one("#trace-pane", Vertical)
        if self.trace_layout == "right":
            workbench.styles.layout = "horizontal"
            divider.styles.width = 1
            divider.styles.height = "100%"
            trace_pane.styles.width = f"{self._trace_width_percent}%"
            trace_pane.styles.height = "100%"
        else:
            workbench.styles.layout = "vertical"
            divider.styles.width = "100%"
            divider.styles.height = 1
            trace_pane.styles.width = "100%"
            trace_pane.styles.height = f"{self._trace_height_percent}%"

    def action_toggle_trace_layout(self) -> None:
        self.trace_layout = "bottom" if self.trace_layout == "right" else "right"
        self._apply_trace_width()
        self._render_keys()
        self._set_notice(f"Trace layout · {self.trace_layout.title()}")
        self.call_after_refresh(lambda: self._configure_table(force=True))

    def _resize_trace(self, change: int) -> None:
        if self.trace_layout == "right":
            size = max(30, min(75, self._trace_width_percent + change))
            if size == self._trace_width_percent:
                self._set_notice(f"Trace width is already {size}%")
                return
            self._trace_width_percent = size
            dimension = "width"
        else:
            size = max(25, min(75, self._trace_height_percent + change))
            if size == self._trace_height_percent:
                self._set_notice(f"Trace height is already {size}%")
                return
            self._trace_height_percent = size
            dimension = "height"
        self._apply_trace_width()
        self._set_notice(f"Trace {dimension} {size}%")
        self.call_after_refresh(lambda: self._configure_table(force=True))

    def action_narrow_trace(self) -> None:
        self._resize_trace(-5)

    def action_widen_trace(self) -> None:
        self._resize_trace(5)

    def _set_notice(self, text: str, *, tone: str = "", timeout_s: float = 4) -> None:
        notice = self.query_one("#notice", Static)
        notice.update(text)
        notice.set_class(tone == "error", "error")
        notice.set_class(tone == "armed", "armed")
        self._notice_generation += 1
        generation = self._notice_generation
        if timeout_s:
            self.set_timer(timeout_s, lambda: self._clear_notice(generation))

    def _clear_notice(self, generation: int) -> None:
        if generation != self._notice_generation or self._armed_stop:
            return
        notice = self.query_one("#notice", Static)
        notice.update("Ready")
        notice.remove_class("error")
        notice.remove_class("armed")

    def action_refresh_now(self) -> None:
        self._set_notice("Refreshing fleet status…", timeout_s=2)
        self.refresh_snapshot()

    def action_attach(self) -> None:
        row = self.current_row()
        if not row or not row.tmux_alive:
            self._set_notice("Attach needs a live worker session", tone="error")
            return
        command = ["tmux", "attach-session", "-t", f"={row.key}"]
        try:
            if os.environ.get("TMUX"):
                command = ["tmux", "switch-client", "-t", f"={row.key}"]
                result = subprocess.run(command, capture_output=True, text=True, check=False)
            else:
                with self.suspend():
                    result = subprocess.run(command, check=False)
        except OSError as exc:
            self._set_notice(f"Could not run tmux: {exc}", tone="error")
            return
        if result.returncode:
            self._set_notice(f"Could not attach {row.key}", tone="error")
        else:
            self._set_notice(f"Returned from {row.key}")

    def action_chat(self) -> None:
        row = self.current_row()
        if not row or not resume_eligible(row):
            self._set_notice(
                "Chat needs a parked, crashed, or finished conversation",
                tone="error",
            )
            return
        command = [*command_argv(self.fleet_resume), row.identity]
        try:
            if os.environ.get("TMUX"):
                result = subprocess.run(command, capture_output=True, text=True, check=False)
                if result.returncode:
                    detail = result.stderr.strip().splitlines()
                    self._set_notice(
                        detail[-1] if detail else f"Could not open chat for {row.key}",
                        tone="error",
                    )
                    return
            else:
                with self.suspend():
                    result = subprocess.run(command, check=False)
                if result.returncode:
                    self._set_notice(f"Chat exited {result.returncode}", tone="error")
                    return
        except OSError as exc:
            self._set_notice(f"Could not open chat: {exc}", tone="error")
            return
        self._set_notice(f"Opened interactive chat for {row.key}")

    def action_open(self) -> None:
        row = self.current_row()
        if not row or not row.url:
            self._set_notice("Selected row has no GitLab link", tone="error")
            return
        try:
            result = subprocess.run(["open", row.url], capture_output=True, check=False)
        except OSError as exc:
            self._set_notice(f"Could not open GitLab link: {exc}", tone="error")
            return
        if result.returncode:
            self._set_notice("Could not open GitLab link", tone="error")
        else:
            self._set_notice(f"Opened {row.url}")

    def action_stop_worker(self) -> None:
        row = self.current_row()
        if (
            not row
            or not row.tmux_alive
            or row.derived not in {"working", "queued"}
        ):
            self._set_notice("Stop needs a live worker session", tone="error")
            return
        if self._armed_stop != row.identity:
            self._armed_stop = row.identity
            self._armed_generation += 1
            generation = self._armed_generation
            self._set_notice(
                f"STOP ARMED · press s again within 5s to kill {row.key}",
                tone="armed",
                timeout_s=0,
            )
            self.set_timer(5, lambda: self._expire_stop_arm(generation))
            return
        try:
            result = subprocess.run(
                ["tmux", "kill-session", "-t", f"={row.key}"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._clear_stop_arm()
            self._set_notice(f"Could not stop {row.key}: {exc}", tone="error")
            return
        self._clear_stop_arm()
        if result.returncode:
            self._set_notice(f"Could not stop {row.key}", tone="error")
        else:
            self._set_notice(f"Stopped {row.key}")
            self.refresh_snapshot()

    def action_delete_finished(self) -> None:
        row = self.current_row()
        if not row or row.derived != "finished":
            self._set_notice("Delete only applies to a finished run", tone="error")
            return
        try:
            result = subprocess.run(
                [*command_argv(self.fleet_dismiss), row.identity, row.run_id],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._set_notice(f"Could not delete {row.key}: {exc}", tone="error")
            return
        if result.returncode:
            detail = result.stderr.strip().splitlines()
            self._set_notice(
                detail[-1] if detail else f"Could not delete {row.key}",
                tone="error",
            )
            return
        self._trace_cache = {
            key: entry
            for key, entry in self._trace_cache.items()
            if key[0] != row.identity
        }
        self._set_notice(f"Deleted finished run for {row.key}")
        self.refresh_snapshot()

    def _expire_stop_arm(self, generation: int) -> None:
        if generation != self._armed_generation:
            return
        self._clear_stop_arm()
        self._set_notice("Stop cancelled")

    def _clear_stop_arm(self) -> None:
        was_armed = self._armed_stop is not None
        self._armed_stop = None
        self._armed_generation += 1
        if self.is_mounted:
            notice = self.query_one("#notice", Static)
            notice.remove_class("armed")
            if was_armed:
                notice.update("Ready")

    def action_help(self) -> None:
        self._set_notice(
            "↑↓/jk select · a attach · i interactive chat · o GitLab · "
            "r refresh · / filter · 1-5 state scopes · g repository · t theme · "
            "v trace layout · f follow · end latest · s,s stop · "
            "x delete finished · [ ] resize trace · "
            "focus trace + home/pgup scroll · drag + ctrl-c copy · q quit",
            timeout_s=8,
        )


def main() -> None:
    FleetApp().run(mouse=True)


if __name__ == "__main__":
    main()
