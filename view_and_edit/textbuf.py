"""Editable text buffer: cursor, selection, undo/redo, search, and faithful load/save."""

from __future__ import annotations

import os
import re
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from view_and_edit.width import column_to_index, index_to_column

INDENT = "    "
# Tried in order; cp932 covers Shift_JIS files that are still common in Japan.
_ENCODINGS = ("utf-8", "cp932")
_WORD = re.compile(r"\w")


class DecodeError(Exception):
    """The file is not text in a supported encoding."""


@dataclass(frozen=True, order=True)
class Pos:
    row: int
    col: int  # character index within the line


@dataclass(frozen=True)
class FileFormat:
    encoding: str = "utf-8"
    newline: str = "\n"
    bom: bool = False
    final_newline: bool = True


@dataclass
class _Edit:
    start: Pos
    removed: str
    inserted: str
    cursor_before: Pos
    cursor_after: Pos
    mergeable: bool = False
    token: int = 0  # identifies the buffer state this edit produces


def decode_text(data: bytes) -> tuple[str, FileFormat]:
    bom = data.startswith(b"\xef\xbb\xbf")
    if bom:
        data = data[3:]
    if b"\x00" in data:
        raise DecodeError("バイナリファイルのようです")
    for encoding in _ENCODINGS:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        newline = "\r\n" if "\r\n" in text else "\n"
        text = text.replace("\r\n", "\n")
        final_newline = text.endswith("\n") or not text
        if text.endswith("\n"):
            text = text[:-1]
        return text, FileFormat(encoding, newline, bom, final_newline)
    raise DecodeError("文字コードを判別できません（UTF-8 / Shift_JIS 以外）")


def encode_text(text: str, fmt: FileFormat) -> bytes:
    body = text + ("\n" if fmt.final_newline and text else "")
    body = body.replace("\n", fmt.newline)
    data = body.encode(fmt.encoding)
    return (b"\xef\xbb\xbf" + data) if fmt.bom else data


def atomic_write(path: Path, data: bytes) -> None:
    """Write via a temp file + rename so a crash never leaves a half-written file."""
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


@dataclass
class TextBuffer:
    lines: list[str] = field(default_factory=lambda: [""])
    cursor: Pos = Pos(0, 0)
    anchor: Pos | None = None
    goal_column: int | None = None
    _undo: list[_Edit] = field(default_factory=list)
    _redo: list[_Edit] = field(default_factory=list)
    _tokens: int = 0
    _saved_token: int = 0

    @classmethod
    def from_text(cls, text: str) -> TextBuffer:
        return cls(lines=text.split("\n"))

    # ---- state -----------------------------------------------------------
    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def _state(self) -> int:
        return self._undo[-1].token if self._undo else 0

    @property
    def dirty(self) -> bool:
        return self._state != self._saved_token

    def mark_saved(self) -> None:
        self._saved_token = self._state
        if self._undo:
            self._undo[-1].mergeable = False  # typing after a save starts a new undo step

    def selection(self) -> tuple[Pos, Pos] | None:
        if self.anchor is None or self.anchor == self.cursor:
            return None
        return (min(self.anchor, self.cursor), max(self.anchor, self.cursor))

    def selected_text(self) -> str:
        sel = self.selection()
        return self._get(*sel) if sel else ""

    def _clamp(self, pos: Pos) -> Pos:
        row = max(0, min(pos.row, len(self.lines) - 1))
        return Pos(row, max(0, min(pos.col, len(self.lines[row]))))

    def _get(self, start: Pos, end: Pos) -> str:
        if start.row == end.row:
            return self.lines[start.row][start.col : end.col]
        parts = [self.lines[start.row][start.col :]]
        parts.extend(self.lines[start.row + 1 : end.row])
        parts.append(self.lines[end.row][: end.col])
        return "\n".join(parts)

    def _raw_replace(self, start: Pos, end: Pos, text: str) -> Pos:
        prefix = self.lines[start.row][: start.col]
        suffix = self.lines[end.row][end.col :]
        new_lines = (prefix + text + suffix).split("\n")
        self.lines[start.row : end.row + 1] = new_lines
        last_row = start.row + len(new_lines) - 1
        return Pos(last_row, len(new_lines[-1]) - len(suffix))

    def _end_of(self, start: Pos, text: str) -> Pos:
        parts = text.split("\n")
        if len(parts) == 1:
            return Pos(start.row, start.col + len(text))
        return Pos(start.row + len(parts) - 1, len(parts[-1]))

    def _edit(self, start: Pos, end: Pos, text: str, mergeable: bool = False) -> None:
        before = self.cursor
        removed = self._get(start, end)
        after = self._raw_replace(start, end, text)
        self._tokens += 1
        edit = _Edit(start, removed, text, before, after, mergeable, self._tokens)
        last = self._undo[-1] if self._undo else None
        if (
            mergeable
            and last is not None
            and last.mergeable
            and not removed
            and last.cursor_after == start
            and "\n" not in text
        ):
            last.inserted += text
            last.cursor_after = after
            last.token = self._tokens
        else:
            self._undo.append(edit)
        self._redo.clear()
        self.cursor = after
        self.anchor = None
        self.goal_column = None

    # ---- editing ----------------------------------------------------------
    def insert(self, text: str) -> None:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        sel = self.selection()
        if sel:
            self._edit(sel[0], sel[1], text)
        else:
            self._edit(self.cursor, self.cursor, text, mergeable=len(text) == 1 and text != " ")

    def newline(self) -> None:
        line = self.lines[self.cursor.row]
        indent = line[: len(line) - len(line.lstrip(" \t"))]
        sel = self.selection()
        start = sel[0] if sel else self.cursor
        indent = indent[: start.col] if start.row == self.cursor.row else indent
        self.insert("\n" + indent)

    def backspace(self) -> None:
        sel = self.selection()
        if sel:
            self._edit(sel[0], sel[1], "")
            return
        cur = self.cursor
        if cur.col == 0 and cur.row == 0:
            return
        if cur.col == 0:
            start = Pos(cur.row - 1, len(self.lines[cur.row - 1]))
        else:
            line = self.lines[cur.row]
            before = line[: cur.col]
            # Remove a whole indent unit when backspacing inside leading spaces.
            if before and not before.strip(" ") and len(before) >= len(INDENT):
                width = len(before) % len(INDENT) or len(INDENT)
                start = Pos(cur.row, cur.col - width)
            else:
                start = Pos(cur.row, cur.col - 1)
        self._edit(start, cur, "")

    def delete(self) -> None:
        sel = self.selection()
        if sel:
            self._edit(sel[0], sel[1], "")
            return
        cur = self.cursor
        if cur.col < len(self.lines[cur.row]):
            self._edit(cur, Pos(cur.row, cur.col + 1), "")
        elif cur.row + 1 < len(self.lines):
            self._edit(cur, Pos(cur.row + 1, 0), "")

    def cut(self) -> str:
        sel = self.selection()
        if not sel:
            # Like most editors: cut the whole line when nothing is selected.
            row = self.cursor.row
            text = self.lines[row] + "\n"
            if row + 1 < len(self.lines):
                self._edit(Pos(row, 0), Pos(row + 1, 0), "")
            elif row > 0:
                self._edit(
                    Pos(row - 1, len(self.lines[row - 1])), Pos(row, len(self.lines[row])), ""
                )
            else:
                self._edit(Pos(0, 0), Pos(0, len(self.lines[0])), "")
            return text
        text = self._get(*sel)
        self._edit(sel[0], sel[1], "")
        return text

    def copy(self) -> str:
        return self.selected_text() or self.lines[self.cursor.row] + "\n"

    def indent(self) -> None:
        sel = self.selection()
        if not sel or sel[0].row == sel[1].row:
            col = index_to_column(self.lines[self.cursor.row], self.cursor.col)
            self.insert(" " * (len(INDENT) - col % len(INDENT)))
            return
        self._map_lines(sel, lambda line: INDENT + line if line else line)

    def dedent(self) -> None:
        sel = self.selection() or (self.cursor, self.cursor)

        def strip(line: str) -> str:
            removable = len(line) - len(line.lstrip(" "))
            return line[min(removable, len(INDENT)) :]

        self._map_lines(sel, strip)

    def _map_lines(self, sel: tuple[Pos, Pos], transform: Callable[[str], str]) -> None:
        """Apply `transform` to every selected line as one undoable edit, keeping them selected."""
        first, last = sel[0].row, sel[1].row
        if sel[1].col == 0 and last > first:
            last -= 1
        old = self.lines[first : last + 1]
        new = [transform(line) for line in old]
        if new == old:
            return
        had_selection = self.selection() is not None
        cursor_at_end = self.anchor is None or self.anchor <= self.cursor
        cursor = self.cursor
        self._edit(Pos(first, 0), Pos(last, len(old[-1])), "\n".join(new))
        if not had_selection:
            shift = len(old[cursor.row - first]) - len(new[cursor.row - first])
            self.cursor = self._clamp(Pos(cursor.row, max(0, cursor.col - shift)))
            return
        start, stop = Pos(first, 0), Pos(last, len(self.lines[last]))
        self.anchor, self.cursor = (start, stop) if cursor_at_end else (stop, start)

    def undo(self) -> bool:
        if not self._undo:
            return False
        edit = self._undo.pop()
        self._raw_replace(edit.start, self._end_of(edit.start, edit.inserted), edit.removed)
        self._redo.append(edit)
        self.cursor = edit.cursor_before
        self.anchor = None
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        edit = self._redo.pop()
        self._raw_replace(edit.start, self._end_of(edit.start, edit.removed), edit.inserted)
        edit.mergeable = False
        self._undo.append(edit)
        self.cursor = edit.cursor_after
        self.anchor = None
        return True

    # ---- movement ---------------------------------------------------------
    def _move_to(self, pos: Pos, select: bool, keep_goal: bool = False) -> None:
        if select:
            if self.anchor is None:
                self.anchor = self.cursor
        else:
            self.anchor = None
        self.cursor = self._clamp(pos)
        if not keep_goal:
            self.goal_column = None

    def left(self, select: bool = False) -> None:
        sel = self.selection()
        if sel and not select:
            self._move_to(sel[0], False)
            return
        cur = self.cursor
        if cur.col > 0:
            self._move_to(Pos(cur.row, cur.col - 1), select)
        elif cur.row > 0:
            self._move_to(Pos(cur.row - 1, len(self.lines[cur.row - 1])), select)
        else:
            self._move_to(cur, select)

    def right(self, select: bool = False) -> None:
        sel = self.selection()
        if sel and not select:
            self._move_to(sel[1], False)
            return
        cur = self.cursor
        if cur.col < len(self.lines[cur.row]):
            self._move_to(Pos(cur.row, cur.col + 1), select)
        elif cur.row + 1 < len(self.lines):
            self._move_to(Pos(cur.row + 1, 0), select)
        else:
            self._move_to(cur, select)

    def vertical(self, delta: int, select: bool = False) -> None:
        cur = self.cursor
        if self.goal_column is None:
            self.goal_column = index_to_column(self.lines[cur.row], cur.col)
        row = max(0, min(len(self.lines) - 1, cur.row + delta))
        if row == cur.row:
            col = 0 if delta < 0 else len(self.lines[row])
        else:
            col = column_to_index(self.lines[row], self.goal_column)
        self._move_to(Pos(row, col), select, keep_goal=row != cur.row)

    def home(self, select: bool = False) -> None:
        """First non-blank character, then column 0 on a second press."""
        line = self.lines[self.cursor.row]
        first = len(line) - len(line.lstrip(" \t"))
        col = 0 if self.cursor.col == first else first
        self._move_to(Pos(self.cursor.row, col), select)

    def end(self, select: bool = False) -> None:
        self._move_to(Pos(self.cursor.row, len(self.lines[self.cursor.row])), select)

    def doc_start(self, select: bool = False) -> None:
        self._move_to(Pos(0, 0), select)

    def doc_end(self, select: bool = False) -> None:
        self._move_to(Pos(len(self.lines) - 1, len(self.lines[-1])), select)

    def word_left(self, select: bool = False) -> None:
        cur = self.cursor
        if cur.col == 0:
            self.left(select)
            return
        line = self.lines[cur.row]
        col = cur.col
        while col > 0 and not _WORD.match(line[col - 1]):
            col -= 1
        while col > 0 and _WORD.match(line[col - 1]):
            col -= 1
        self._move_to(Pos(cur.row, col), select)

    def word_right(self, select: bool = False) -> None:
        cur = self.cursor
        line = self.lines[cur.row]
        if cur.col >= len(line):
            self.right(select)
            return
        col = cur.col
        while col < len(line) and not _WORD.match(line[col]):
            col += 1
        while col < len(line) and _WORD.match(line[col]):
            col += 1
        self._move_to(Pos(cur.row, col), select)

    def select_all(self) -> None:
        self.anchor = Pos(0, 0)
        self.cursor = Pos(len(self.lines) - 1, len(self.lines[-1]))

    def goto(self, row: int, col: int = 0) -> None:
        self._move_to(Pos(row, col), False)

    def click(self, row: int, column: int, select: bool = False) -> None:
        row = max(0, min(row, len(self.lines) - 1))
        self._move_to(Pos(row, column_to_index(self.lines[row], column)), select)

    # ---- search -----------------------------------------------------------
    def find(self, query: str, forward: bool = True) -> bool:
        """Select the next (or previous) case-insensitive match, wrapping around."""
        if not query:
            return False
        needle = query.lower()
        start = (self.selection() or (self.cursor, self.cursor))[1 if forward else 0]
        count = len(self.lines)
        for step in range(count + 1):
            if forward:
                row = (start.row + step) % count
                hay = self.lines[row].lower()
                col = hay.find(needle, start.col if step == 0 else 0)
            else:
                row = (start.row - step) % count
                hay = self.lines[row].lower()
                if step == 0:
                    # Only matches that begin before the current selection.
                    col = hay.rfind(needle, 0, start.col - 1 + len(needle)) if start.col else -1
                else:
                    col = hay.rfind(needle)
            if col >= 0:
                self.anchor = Pos(row, col)
                self.cursor = Pos(row, col + len(query))
                self.goal_column = None
                return True
        return False
