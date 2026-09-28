"""Built-in editor: notepad-style keys (Ctrl+S save, Ctrl+Q close, Shift+arrows select)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from view_and_edit.app import App, Choice, Screen, home_relative
from view_and_edit.keys import Event, Key, Mouse, Paste
from view_and_edit.media import copy_to_clipboard, paste_from_clipboard
from view_and_edit.syntax import Highlighter, language_for
from view_and_edit.textbuf import (
    DecodeError,
    FileFormat,
    Pos,
    TextBuffer,
    atomic_write,
    decode_text,
    encode_text,
)
from view_and_edit.ui import Frame, Line, Span, Style, highlight_matches, render_line
from view_and_edit.width import index_to_column

WHEEL_LINES = 3
SCROLL_MARGIN = 2
HSCROLL_MARGIN = 8
# Colouring re-runs from the top each frame; skip it for very long files to stay responsive.
HIGHLIGHT_MAX_LINES = 20000


class EditorScreen(Screen):
    captures_text = True  # `?` is typed into the file instead of opening help

    def __init__(self, app: App, path: Path, line: int | None = None) -> None:
        self.app = app
        self.path = path
        self.fmt = FileFormat()
        self.mtime: float | None = None
        self.read_only_reason: str | None = None
        self.buffer = TextBuffer()
        self.scroll = 0
        self.hscroll = 0
        self.query = ""
        self._dragging = False
        self._load()
        if line:
            self.buffer.goto(line - 1)
            self.scroll = max(0, line - 1 - 5)
        self.highlighter = Highlighter(language_for(path))

    def _load(self) -> None:
        if not self.path.exists():
            return  # new file: saved on first Ctrl+S
        try:
            text, self.fmt = decode_text(self.path.read_bytes())
        except (DecodeError, OSError) as error:
            self.read_only_reason = str(error)
            return
        self.buffer = TextBuffer.from_text(text)
        self.mtime = self.path.stat().st_mtime

    # ---- rendering --------------------------------------------------------
    def render(self, width: int, height: int) -> Frame:
        buf = self.buffer
        body_height = height - 2
        gutter = len(str(len(buf.lines))) + 2
        text_width = width - gutter
        self._follow_cursor(body_height, text_width)

        marker = " ●" if buf.dirty else ""
        title = (
            Line()
            .add(f" {self.path.name}{marker} ", Style.TITLE)
            .add(f"  {home_relative(self.path.parent)}", Style.DIM)
        )
        column = index_to_column(buf.lines[buf.cursor.row], buf.cursor.col) + 1
        status = f"{buf.cursor.row + 1}:{column}  {self._format_label()} "
        if self.read_only_reason:
            status = f"読み取り専用: {self.read_only_reason} "
        header = title.window(0, width - len(status) - 1)
        header.add(" " * max(0, width - header.width() - len(status))).add(status, Style.DIM)
        lines = [render_line(header, width)]

        selection = buf.selection()
        highlight = len(buf.lines) <= HIGHLIGHT_MAX_LINES
        self.highlighter.reset()
        if highlight:
            for row in range(min(self.scroll, len(buf.lines))):
                self.highlighter.line(buf.lines[row])
        for screen_row in range(body_height):
            row = self.scroll + screen_row
            if row >= len(buf.lines):
                lines.append(render_line(Line.of(" " * (gutter - 2) + "~", Style.DIM), width))
                continue
            text = buf.lines[row]
            styled = self.highlighter.line(text) if highlight else Line.of(text)
            styled = highlight_matches(styled, self.query)
            if selection:
                styled = _overlay_selection(styled, row, selection)
            number_style = Style.ACCENT if row == buf.cursor.row else Style.DIM
            gutter_line = Line.of(f"{row + 1:>{gutter - 2}}  ", number_style)
            body = styled.window(self.hscroll, text_width)
            lines.append(render_line(Line([*gutter_line.spans, *body.spans]), width))
        cursor_col = (
            gutter + index_to_column(buf.lines[buf.cursor.row], buf.cursor.col) - self.hscroll
        )
        cursor = (1 + buf.cursor.row - self.scroll, min(width - 1, cursor_col))
        return Frame(lines, cursor=cursor)

    def _format_label(self) -> str:
        parts = ["UTF-8" if self.fmt.encoding == "utf-8" else "Shift_JIS"]
        if self.fmt.bom:
            parts.append("BOM")
        parts.append("CRLF" if self.fmt.newline == "\r\n" else "LF")
        return " ".join(parts)

    def _follow_cursor(self, height: int, width: int) -> None:
        row = self.buffer.cursor.row
        margin = min(SCROLL_MARGIN, height // 4)
        if row < self.scroll + margin:
            self.scroll = max(0, row - margin)
        elif row >= self.scroll + height - margin:
            self.scroll = row - height + margin + 1
        col = index_to_column(self.buffer.lines[row], self.buffer.cursor.col)
        if col < self.hscroll:
            self.hscroll = max(0, col - HSCROLL_MARGIN)
        elif col >= self.hscroll + width - 1:
            self.hscroll = col - width + HSCROLL_MARGIN

    # ---- input ------------------------------------------------------------
    def hints(self) -> list[tuple[str, str]]:
        return [
            ("^S", "保存"),
            ("^Q", "閉じる"),
            ("^F", "検索"),
            ("^Z", "元に戻す"),
            ("^C", "コピー"),
            ("^X", "切取"),
            ("^V", "貼付"),
            ("^G", "行へ移動"),
            ("F1", "ヘルプ"),
        ]

    def help(self) -> list[tuple[str, str]]:
        return [
            ("矢印 / Home End", "カーソル移動（Home は行頭の文字 ⇄ 行頭）"),
            ("PgUp PgDn", "1 画面移動"),
            ("Ctrl+Home / End", "ファイルの先頭 / 末尾"),
            ("Alt+← →", "単語単位で移動（Ctrl+← → も可）"),
            ("Shift+矢印", "選択（マウスのドラッグでも選択）"),
            ("Ctrl+A", "すべて選択"),
            ("Ctrl+C / X / V", "コピー / 切り取り / 貼り付け（選択なしなら行ごと）"),
            ("Tab / Shift+Tab", "字下げ / 字下げ解除（選択行まとめて）"),
            ("Ctrl+Z / Ctrl+Y", "元に戻す / やり直し"),
            ("Ctrl+F", "検索（Enter で次、F3 / Ctrl+N で次、Shift+F3 / Ctrl+P で前）"),
            ("Ctrl+G", "指定行へ移動"),
            ("Ctrl+S", "保存"),
            ("Esc / Ctrl+Q / Ctrl+W", "閉じる（未保存なら確認。選択中の Esc は選択の解除）"),
            ("マウス", "クリックでカーソル移動、ドラッグで選択、ホイールでスクロール"),
            ("", "下の案内（^S 保存 など）もクリックで押せます"),
        ]

    def handle(self, event: Event) -> None:
        if isinstance(event, Paste):
            self._modify(lambda: self.buffer.insert(event.text))
        elif isinstance(event, Mouse):
            self._mouse(event)
        elif isinstance(event, Key):
            self._key(event.name)

    def _modify(self, action: Callable[[], object]) -> None:
        if self.read_only_reason:
            self.app.error(f"編集できません: {self.read_only_reason}")
            return
        action()

    def _key(self, name: str) -> None:
        buf = self.buffer
        _, height = self.app.terminal.size()
        page = max(1, height - 4)
        select = name.startswith("shift+") or name.startswith("ctrl+shift+")
        base = name.removeprefix("ctrl+shift+").removeprefix("shift+")
        moves: dict[str, Callable[[bool], None]] = {
            "left": buf.left,
            "right": buf.right,
            "up": lambda s: buf.vertical(-1, s),
            "down": lambda s: buf.vertical(1, s),
            "home": buf.home,
            "end": buf.end,
            "pageup": lambda s: buf.vertical(-page, s),
            "pagedown": lambda s: buf.vertical(page, s),
        }
        word_moves: dict[str, Callable[[bool], None]] = {
            "ctrl+left": buf.word_left,
            "alt+left": buf.word_left,
            "alt+b": buf.word_left,
            "ctrl+right": buf.word_right,
            "alt+right": buf.word_right,
            "alt+f": buf.word_right,
            "ctrl+home": buf.doc_start,
            "ctrl+end": buf.doc_end,
        }
        plain = name.replace("shift+", "")
        if base in moves and not name.startswith("ctrl+") and not name.startswith("alt+"):
            moves[base](select)
            return
        if plain in word_moves:
            word_moves[plain]("shift+" in name)
            return
        commands: dict[str, Callable[[], None]] = {
            "ctrl+s": self.save,
            "ctrl+q": self.request_close,
            "ctrl+w": self.request_close,
            "ctrl+f": self._ask_find,
            "f3": lambda: self._find(True),
            "ctrl+n": lambda: self._find(True),
            "shift+f3": lambda: self._find(False),
            "ctrl+p": lambda: self._find(False),
            "ctrl+g": self._ask_goto,
            "ctrl+a": buf.select_all,
            "ctrl+c": self._copy,
            "escape": self._escape,
        }
        if name in commands:
            commands[name]()
            return
        edits: dict[str, Callable[[], object]] = {
            "enter": buf.newline,
            "backspace": buf.backspace,
            "ctrl+h": buf.backspace,
            "delete": buf.delete,
            "tab": buf.indent,
            "backtab": buf.dedent,
            "ctrl+z": buf.undo,
            "ctrl+y": buf.redo,
            "ctrl+shift+z": buf.redo,
            "ctrl+x": self._cut,
            "ctrl+v": self._paste,
        }
        if name in edits:
            self._modify(edits[name])
        elif len(name) == 1 and name.isprintable():
            self._modify(lambda: buf.insert(name))

    def _mouse(self, event: Mouse) -> None:
        buf = self.buffer
        gutter = len(str(len(buf.lines))) + 2
        row = self.scroll + event.y - 1
        column = self.hscroll + event.x - gutter
        if event.kind == "wheel_up":
            self.scroll = max(0, self.scroll - WHEEL_LINES)
            buf.vertical(-WHEEL_LINES)
        elif event.kind == "wheel_down":
            buf.vertical(WHEEL_LINES)
        elif event.kind == "press" and event.button == 0 and event.y >= 1:
            buf.click(row, max(0, column), select=event.shift)
            self._dragging = True
        elif event.kind == "drag" and self._dragging:
            buf.click(row, max(0, column), select=True)
        elif event.kind == "release":
            self._dragging = False

    # ---- commands ---------------------------------------------------------
    def _copy(self) -> None:
        if copy_to_clipboard(self.buffer.copy()):
            self.app.message("コピーしました")

    def _cut(self) -> None:
        text = self.buffer.cut()
        copy_to_clipboard(text)

    def _paste(self) -> None:
        text = paste_from_clipboard()
        if text:
            self.buffer.insert(text)

    def _ask_find(self) -> None:
        start = self.buffer.cursor

        def live(query: str) -> None:
            self.query = query
            self.buffer.goto(start.row, start.col)
            if query:
                self.buffer.find(query)

        def submit(query: str) -> None:
            self.query = query
            if query and not self.buffer.selection() and not self.buffer.find(query):
                self.app.error(f"見つかりません: {query}")

        self.app.ask(
            "検索（Enter で確定、以後 F3 / Ctrl+N で次）",
            submit,
            initial=self.query,
            on_change=live,
        )

    def _find(self, forward: bool) -> None:
        if not self.query:
            self._ask_find()
        elif not self.buffer.find(self.query, forward):
            self.app.error(f"見つかりません: {self.query}")

    def _ask_goto(self) -> None:
        def submit(text: str) -> None:
            if text.strip().isdigit():
                self.buffer.goto(int(text) - 1)

        self.app.ask("移動する行番号", submit)

    def save(self, then: Callable[[], None] | None = None) -> None:
        if self.read_only_reason:
            self.app.error(f"保存できません: {self.read_only_reason}")
            return
        changed_outside = (
            self.mtime is not None
            and self.path.exists()
            and self.path.stat().st_mtime != self.mtime
        )

        def write() -> None:
            try:
                atomic_write(self.path, encode_text(self.buffer.text, self.fmt))
            except (OSError, UnicodeEncodeError) as error:
                self.app.error(f"保存できませんでした: {error}")
                return
            self.buffer.mark_saved()
            self.mtime = self.path.stat().st_mtime
            self.app.message(f"保存しました: {self.path.name}")
            if then:
                then()

        if changed_outside:
            self.app.confirm_choice(
                "ほかで変更されています。上書きしますか？",
                [Choice("y", "上書き", write), Choice("n", "やめる", lambda: None)],
            )
        else:
            write()

    def _escape(self) -> None:
        # Esc goes back on every screen. A selection is cleared first, so a stray Esc
        # after Shift+arrows does not close the file.
        if self.buffer.selection() is not None:
            self.buffer.anchor = None
        else:
            self.request_close()

    def request_close(self) -> None:
        if not self.buffer.dirty:
            self.app.pop()
            return
        self.app.confirm_choice(
            "変更を保存しますか？",
            [
                Choice("y", "保存して閉じる", lambda: self.save(then=self.app.pop)),
                Choice("n", "保存せず閉じる", self.app.pop),
            ],
            cancel_label="編集に戻る",
        )


def _overlay_selection(line: Line, row: int, selection: tuple[Pos, Pos]) -> Line:
    start, end = selection
    if row < start.row or row > end.row:
        return line
    lo = start.col if row == start.row else 0
    hi = end.col if row == end.row else len(line.text) + 1
    out = Line()
    pos = 0
    for span in line.spans:
        span_end = pos + len(span.text)
        a, b = max(lo, pos), min(hi, span_end)
        if a >= b:
            out.spans.append(span)
        else:
            out.add(span.text[: a - pos], span.style)
            out.add(
                span.text[a - pos : b - pos], ";".join(p for p in (span.style, Style.SELECTED) if p)
            )
            out.add(span.text[b - pos :], span.style)
        pos = span_end
    if hi > len(line.text):  # show that the line break is selected too
        out.spans.append(Span(" ", Style.SELECTED))
    return out
