"""Local Obsidian-vault TaskNotes board client — the ``forge: vault`` provider.

The vault's authority is each task note's YAML frontmatter ``status:`` field,
structurally the same single-select the GitHub Projects v2 provider watches. So
this client mirrors :class:`~eastwatch.watcher.GitHubProject`: a poll (read every
task note's status) and a small set of guarded writers (move a note's status,
append a result section, stamp a field).

Reads parse frontmatter with a ``--- … ---`` splitter + ``yaml.safe_load``. Writes
are deliberately *surgical* — a single-line regex substitution inside the
frontmatter block, then an atomic same-directory ``os.replace``. We never
``yaml.safe_dump``-round-trip a note: TaskNotes frontmatter carries ``reminders:``
blocks, offset durations, quoted wikilinks and ``cssclasses`` that a dump would
reorder or reformat, corrupting the human's note.
"""

from __future__ import annotations

import glob
import hashlib
import logging
import os
import re
import subprocess
from pathlib import Path

import yaml

log = logging.getLogger("eastwatch")


# Vault status (the authority) -> internal lifecycle/trigger label. Only `agent`
# is a dispatch trigger; the rest are the states eastwatch writes back. `open`,
# `done` and `archived` map to nothing — they are outside reconciliation.
VAULT_STATUS_TO_LABEL = {
    "agent": "agent::ready",
    "in-progress": "agent::working",
    "needs-input": "agent::parked",
    "review": "agent::for-human",
}
# Write-back direction: the lifecycle label a run reaches -> the vault status the
# watcher stamps into the note. MR-flavoured labels fold to `review` (no MRs here).
VAULT_LABEL_TO_STATUS = {
    "agent::working": "in-progress",
    "agent::researching": "in-progress",
    "agent::parked": "needs-input",
    "agent::for-human": "review",
    "agent::mr-ready": "review",
    "agent::failed": "needs-input",  # a crashed/failed run needs the owner
}
# A transition INTO one of these statuses dispatches (parity with GITHUB_DISPATCH_INTO).
VAULT_DISPATCH_INTO = {"agent": "agent::ready"}
# The one status a run "owns" while live. A terminal write-back only lands if the
# note is still here — otherwise a human dragged it away and we must not clobber.
VAULT_ACTIVE_STATUSES = frozenset({"in-progress"})
# Statuses that count as "this blocker is resolved" for the blocked-task skip.
VAULT_DONE_STATUSES = frozenset({"done", "archived"})

# The frontmatter block: fences + inner YAML, anchored to the top of the file.
# Group 1 = opening fence, group 2 = inner YAML body, group 3 = closing fence.
FRONTMATTER_RE = re.compile(r"\A(---\r?\n)(.*?)(\r?\n---[ \t]*\r?\n?)", re.DOTALL)
_STATUS_LINE_RE = re.compile(r"(?m)^(?P<indent>[ \t]*)status:[ \t]*.*$")
_WIKILINK_RE = re.compile(r"\[\[([^\]]+?)\]\]")


def _field_line_re(field: str) -> re.Pattern[str]:
    return re.compile(rf"(?m)^(?P<indent>[ \t]*){re.escape(field)}:[ \t]*.*$")


def split_frontmatter(text: str) -> tuple[dict, str, tuple[int, int] | None]:
    """Return ``(frontmatter_dict, inner_yaml, span)``.

    ``span`` is the ``(start, end)`` char offsets of the inner YAML body (group 2),
    so a writer can substitute inside it and splice the rest of the file back
    byte-for-byte. On a note without frontmatter, returns ``({}, "", None)``. Bad
    YAML yields ``({}, inner, span)`` — the caller decides whether to skip.
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, "", None
    inner = m.group(2)
    try:
        loaded = yaml.safe_load(inner)
    except yaml.YAMLError:
        loaded = None
    fm = loaded if isinstance(loaded, dict) else {}
    return fm, inner, (m.start(2), m.end(2))


def _has_task_tag(fm: dict) -> bool:
    tags = fm.get("tags")
    if isinstance(tags, str):
        return tags.strip() == "task"
    if isinstance(tags, list):
        return any(str(t).strip() == "task" for t in tags)
    return False


def wikilink_targets(value) -> list[str]:
    """The link targets in a frontmatter value (list of ``[[a/b|c]]`` or a string).

    Returns the pre-``|`` target of each wikilink, e.g. ``[[inbox/tasks/foo|foo]]``
    -> ``inbox/tasks/foo``. Bare strings without brackets pass through.
    """
    items = value if isinstance(value, list) else [value] if value else []
    out: list[str] = []
    for item in items:
        s = str(item)
        matches = _WIKILINK_RE.findall(s)
        if matches:
            out.extend(m.split("|", 1)[0].strip() for m in matches)
        elif s.strip():
            out.append(s.strip())
    return out


class VaultBoard:
    """One Obsidian vault's TaskNotes board, read and written on the local disk."""

    def __init__(
        self,
        vault_path: str,
        tasks_glob: str = "inbox/tasks/*.md",
        *,
        commit_results: bool = True,
    ):
        self.vault_path = Path(vault_path).expanduser()
        self.tasks_glob = tasks_glob
        self.commit_results = commit_results
        # Parity with GitHubProject: `repo` for logs, `project_id` namespaces the
        # observation generation. Both are the absolute vault path here.
        self.repo = str(self.vault_path)
        self.project_id = str(self.vault_path)

    # -- identity ------------------------------------------------------------

    @staticmethod
    def item_id_for(rel_path: str) -> str:
        """Stable, ASCII, filesystem/tmux-safe id for a note's relative path.

        Content edits keep the id; a rename changes it (recovered downstream via
        the note's persisted ``session-id:``). Doubles as the conversation key
        and the synthetic issue ``iid``.
        """
        return hashlib.sha1(rel_path.encode("utf-8")).hexdigest()[:12]

    def _rel(self, abs_path: str | Path) -> str:
        return (
            Path(abs_path).resolve().relative_to(self.vault_path.resolve()).as_posix()
        )

    # -- reads ---------------------------------------------------------------

    def fetch_items(self) -> list[dict]:
        """Every task note with a ``status``, as observation dicts.

        Cheap and local, so unlike the GitHub scan this carries the full
        ``title``/``body``/``model`` — there is no separate hydrate step. Notes
        without ``tags: task`` or without a ``status`` are skipped; a note whose
        frontmatter is malformed YAML is skipped with a warning (never crashes
        the cycle).
        """
        items: list[dict] = []
        for path in sorted(glob.glob(str(self.vault_path / self.tasks_glob))):
            try:
                text = Path(path).read_text(encoding="utf-8")
            except OSError as e:
                log.warning("vault: could not read %s: %s", path, e)
                continue
            fm, inner, span = split_frontmatter(text)
            if span is None:
                continue  # no frontmatter -> not a task note
            if not fm and inner.strip():
                log.warning("vault: skipping %s — unparseable frontmatter", path)
                continue
            if not _has_task_tag(fm):
                continue
            status = fm.get("status")
            if not status:
                continue
            rel = self._rel(path)
            body = FRONTMATTER_RE.sub(
                "", text, count=1
            )  # everything after the closing fence
            model = fm.get("model")
            session_id = fm.get("session-id") or fm.get("session_id")
            items.append(
                {
                    "item_id": self.item_id_for(rel),
                    "status": str(status).strip(),
                    "note_path": rel,
                    "abs_path": str(Path(path).resolve()),
                    "title": str(fm.get("title") or Path(path).stem),
                    "body": body.strip(),
                    "model": str(model).strip() if model else None,
                    "session_id": str(session_id).strip() if session_id else None,
                    "mtime": os.path.getmtime(path),
                    "blocked_by": wikilink_targets(
                        fm.get("blockedBy") or fm.get("blocked-by")
                    ),
                }
            )
        return items

    def read_status(self, abs_path: str | Path) -> str | None:
        try:
            text = Path(abs_path).read_text(encoding="utf-8")
        except OSError:
            return None
        fm, _, _ = split_frontmatter(text)
        status = fm.get("status")
        return str(status).strip() if status else None

    # -- writes (surgical, atomic) -------------------------------------------

    def _atomic_write(self, abs_path: str | Path, text: str) -> None:
        """Write via a same-directory temp + ``os.replace`` — the strongest
        atomicity iCloud allows (rename within one directory)."""
        path = Path(abs_path)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def _edit_frontmatter(self, abs_path: str | Path, mutate) -> str | None:
        """Read, apply ``mutate(inner_yaml) -> new_inner`` inside the frontmatter
        block only, splice the rest back byte-for-byte, atomic-write. Returns the
        new text, or None when there is no frontmatter to edit."""
        text = Path(abs_path).read_text(encoding="utf-8")
        m = FRONTMATTER_RE.match(text)
        if not m:
            log.warning("vault: %s has no frontmatter; skipping edit", abs_path)
            return None
        start, end = m.start(2), m.end(2)
        new_inner = mutate(text[start:end])
        new_text = text[:start] + new_inner + text[end:]
        if new_text != text:
            self._atomic_write(abs_path, new_text)
        return new_text

    def set_status(self, abs_path: str | Path, new_status: str) -> None:
        """Move the note's ``status:`` line, touching nothing else."""

        def mutate(inner: str) -> str:
            if _STATUS_LINE_RE.search(inner):
                return _STATUS_LINE_RE.sub(
                    rf"\g<indent>status: {new_status}", inner, count=1
                )
            sep = "" if inner.endswith("\n") or not inner else "\n"
            return f"{inner}{sep}status: {new_status}"

        self._edit_frontmatter(abs_path, mutate)

    def set_status_fenced(
        self,
        abs_path: str | Path,
        new_status: str,
        *,
        active: frozenset[str] = VAULT_ACTIVE_STATUSES,
    ) -> bool:
        """Guarded terminal write: only move to ``new_status`` if the note is
        still one of ``active`` (i.e. we still own it). A human who dragged the
        note away mid-run keeps their drag. Returns True if the write happened."""
        live = self.read_status(abs_path)
        if live is not None and live not in active:
            log.info(
                "vault: skipping status write to %s on %s — human moved it there since dispatch",
                new_status,
                self._rel(abs_path),
            )
            return False
        self.set_status(abs_path, new_status)
        return True

    def write_frontmatter_field(
        self, abs_path: str | Path, field: str, value: str
    ) -> None:
        """Set-or-insert a scalar frontmatter line (e.g. persist ``session-id:``)."""
        pattern = _field_line_re(field)

        def mutate(inner: str) -> str:
            if pattern.search(inner):
                return pattern.sub(rf"\g<indent>{field}: {value}", inner, count=1)
            sep = "" if inner.endswith("\n") or not inner else "\n"
            return f"{inner}{sep}{field}: {value}"

        self._edit_frontmatter(abs_path, mutate)

    def append_result_section(
        self, abs_path: str | Path, body: str, heading: str = "## Result"
    ) -> dict:
        """Append (or replace an existing trailing) ``## Result`` block. Idempotent
        across re-runs. Returns ``{"id", "note_path"}`` for the caller's note
        bookkeeping."""
        text = Path(abs_path).read_text(encoding="utf-8")
        block = f"{heading}\n\n{body.rstrip()}\n"
        # Replace an existing trailing Result section (from a prior run) in place.
        existing = re.search(rf"(?ms)^{re.escape(heading)}[ \t]*$.*\Z", text)
        if existing:
            new_text = text[: existing.start()] + block
        else:
            sep = (
                "" if text.endswith("\n\n") else "\n" if text.endswith("\n") else "\n\n"
            )
            new_text = f"{text}{sep}{block}"
        self._atomic_write(abs_path, new_text)
        rel = self._rel(abs_path)
        return {
            "id": f"result-{hashlib.sha1(body.encode('utf-8')).hexdigest()[:8]}",
            "note_path": rel,
        }

    # -- git -----------------------------------------------------------------

    def git_commit(self, abs_path: str | Path, message: str) -> None:
        """Commit only the one note, best-effort. No-op when ``commit_results`` is
        off; a failure (dirty index, no repo) is logged, never fatal."""
        if not self.commit_results:
            return
        rel = self._rel(abs_path)
        try:
            subprocess.run(
                ["git", "-C", str(self.vault_path), "add", "--", rel],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(self.vault_path), "commit", "-m", message, "--", rel],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("vault: git commit of %s failed (non-fatal): %s", rel, e)
