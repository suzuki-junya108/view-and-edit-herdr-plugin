"""List screens with a live preview on the right (like Finder's column view).

`BrowserScreen` lists a folder, `PickerScreen` a fixed set of files (the ones on the
pane's screen, or every match of a name), and `SearchScreen` the project's files
matching what the user types.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from pathlib import Path

from view_and_edit.app import IDLE_TICK, App, Choice, Screen, home_relative, mouse_in
from view_and_edit.documents import (
    Entry,
    list_directory,
    open_document,
    too_large_for_quick_preview,
)
from view_and_edit.editor import EditorScreen
from view_and_edit.formats import Kind, detect, is_editable
from view_and_edit.fuzzy import rank
from view_and_edit.keys import Event, Key, Mouse, Paste
from view_and_edit.locate import FileIndex, build_index
from view_and_edit.media import copy_to_clipboard, move_to_trash, open_external
from view_and_edit.structured import human_size
from view_and_edit.target import Target
from view_and_edit.ui import Frame, ImagePlacement, Line, Style, render_line
from view_and_edit.viewer import PREVIEW_IMAGE_SLOT, ContentPane, ViewerScreen
from view_and_edit.width import fit, text_width, truncate, truncate_left

LIST_MIN_WIDTH = 24
LIST_MAX_WIDTH = 44
LIST_RATIO = 0.36
# Only show the side preview when the popup is wide enough for both columns.
PREVIEW_MIN_WIDTH = 70
# Wait for the cursor to settle before building a preview, so holding ↓ stays fast.
PREVIEW_DELAY = 0.12
DOUBLE_CLICK_SECONDS = 0.4
WHEEL_LINES = 3


class BrowserScreen(Screen):
    def __init__(self, app: App, directory: Path, select: Path | None = None) -> None:
        self.app = app
        self.directory = directory
        self.show_hidden = False
        self.filter = ""
        self.entries: list[Entry] = []
        self.all_entries: list[Entry] = []
        self.index = 0
        self.top = 0
        self.error: str | None = None
        self._preview: ContentPane | None = None
        self._preview_for: Path | None = None
        self._moved_at = 0.0
        self._last_click: tuple[float, int] = (0.0, -1)
        # A file started with `n` exists only once the editor saves it; select it on return.
        self._new_file: Path | None = None
        self.load(select)

    # ---- data -------------------------------------------------------------
    def load(self, select: Path | None = None) -> None:
        current = select or (self.selected.path if self.selected else None)
        try:
            self.all_entries = self._read_entries()
            self.error = None
        except OSError as error:
            self.all_entries = []
            self.error = str(error)
        self._apply_filter()
        if current is not None:
            for i, entry in enumerate(self.entries):
                if entry.path == current:
                    self.index = i
                    break
        self._clamp()
        self._invalidate_preview()

    def _read_entries(self) -> list[Entry]:
        return list_directory(self.directory, self.show_hidden)

    def _apply_filter(self) -> None:
        needle = self.filter.lower()
        self.entries = (
            [e for e in self.all_entries if needle in e.name.lower()]
            if needle
            else list(self.all_entries)
        )

    @property
    def selected(self) -> Entry | None:
        return self.entries[self.index] if 0 <= self.index < len(self.entries) else None

    def _clamp(self) -> None:
        self.index = max(0, min(self.index, len(self.entries) - 1))

    def _invalidate_preview(self) -> None:
        self._moved_at = time.monotonic()
        self._preview = None
        self._preview_for = None

    @property
    def has_parent(self) -> bool:
        return self.directory.parent != self.directory

    def _parent_rows(self) -> int:
        """The `..` row above the entries: a click target for going up without the keyboard."""
        return 1 if self.has_parent else 0

    def _parent_label(self) -> str:
        return "上のフォルダへ"

    def _heading(self) -> str:
        return home_relative(self.directory)

    def _count_label(self) -> str:
        return f"{len(self.entries)} 項目" + ("（隠しファイル表示中）" if self.show_hidden else "")

    def _empty_label(self) -> str:
        return " （何もありません）" if not self.filter else " （一致なし）"

    def move(self, delta: int) -> None:
        if not self.entries:
            return
        self.index = max(0, min(len(self.entries) - 1, self.index + delta))
        self._invalidate_preview()

    # ---- rendering --------------------------------------------------------
    def _list_width(self, width: int) -> int:
        if width < PREVIEW_MIN_WIDTH:
            return width
        return max(LIST_MIN_WIDTH, min(LIST_MAX_WIDTH, int(width * LIST_RATIO)))

    def render(self, width: int, height: int) -> Frame:
        list_width = self._list_width(width)
        body_height = height - 2
        header = Line().add(f" {self._heading()} ", Style.TITLE)
        if self.filter:
            header.add(f"  絞り込み: {self.filter}", Style.ACCENT)
        count = self._count_label()
        header.add(" " * max(1, width - header.width() - text_width(count) - 1)).add(
            count, Style.DIM
        )
        lines = [render_line(header, width)]

        list_height = max(1, body_height - self._parent_rows())
        if self.index < self.top:
            self.top = self.index
        elif self.index >= self.top + list_height:
            self.top = self.index - list_height + 1
        left = [
            self._entry_line(i, list_width)
            for i in range(self.top, min(len(self.entries), self.top + list_height))
        ]
        if self.error:
            left = [Line.of(f" {self.error}", Style.ERROR)]
        elif not self.entries:
            left = [Line.of(self._empty_label(), Style.DIM)]
        if self.has_parent:
            left.insert(
                0, Line().add("   ..", Style.DIRECTORY).add(f"  {self._parent_label()}", Style.DIM)
            )

        right: list[Line] = []
        image = None
        preview_width = width - list_width - 1
        if preview_width > 0:
            right, image = self._render_preview(1, list_width + 1, preview_width, body_height)
        for row in range(body_height):
            left_line = left[row] if row < len(left) else Line()
            if preview_width > 0:
                right_line = right[row] if row < len(right) else Line()
                combined = Line([*left_line.window(0, list_width).spans])
                combined.add(" " * (list_width - combined.width()))
                combined.add("│", Style.DIM)
                combined.spans.extend(right_line.window(0, preview_width).spans)
                lines.append(render_line(combined, width))
            else:
                lines.append(render_line(left_line, width))
        return Frame(lines, images=[image] if image else [])

    def _entry_line(self, index: int, width: int) -> Line:
        entry = self.entries[index]
        name = entry.name + ("/" if entry.is_dir else "")
        size = "" if entry.is_dir else human_size(entry.size)
        name_width = width - 3 - (text_width(size) + 1 if size and width > 30 else 0)
        line = Line.of(" ▸ " if index == self.index else "   ")
        line.add(
            fit(self._fit_name(name, name_width), name_width),
            Style.DIRECTORY if entry.is_dir else "",
        )
        if size and width > 30:
            line.add(" " + size, Style.DIM)
        return line.styled(Style.REVERSE) if index == self.index else line

    def _fit_name(self, name: str, width: int) -> str:
        return truncate(name, width)

    def _render_preview(
        self, row: int, col: int, width: int, height: int
    ) -> tuple[list[Line], ImagePlacement | None]:
        entry = self.selected
        if entry is None:
            return [], None
        kind = Kind.DIRECTORY if entry.is_dir else detect(entry.path)
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.mtime))
        info = Line().add(f" {kind.value}", Style.ACCENT)
        if not entry.is_dir:
            info.add(f"  {human_size(entry.size)}", Style.DIM)
        info.add(f"  {stamp}", Style.DIM)
        lines = [info, Line()]
        if time.monotonic() - self._moved_at < PREVIEW_DELAY:
            return lines, None
        notice = self._preview_notice(entry, kind)
        if notice is not None:
            return [*lines, notice], None
        if self._preview is None:
            return lines, None
        content, image = self._preview.render(row + 2, col + 1, width - 1, height - 2)
        return [*lines, *(Line([*Line.of(" ").spans, *c.spans]) for c in content)], image

    def _preview_notice(self, entry: Entry, kind: Kind) -> Line | None:
        """A one-line message instead of a preview, or None once the preview is ready."""
        if too_large_for_quick_preview(entry.path, kind):
            return Line.of(" 大きいファイルです。Enter で開きます", Style.DIM)
        if kind is Kind.AUDIO:
            return Line.of(" Enter で開いて Space で再生", Style.DIM)
        if self._preview_for != entry.path:
            try:
                doc = open_document(entry.path, self.app.cache, kind)
            except OSError as error:
                return Line.of(f" {error}", Style.ERROR)
            self._preview = ContentPane(self.app, doc, PREVIEW_IMAGE_SLOT)
            self._preview_for = entry.path
        return None

    def tick_interval(self) -> float:
        # Wake up right when the cursor has rested long enough to build the preview.
        remaining = PREVIEW_DELAY - (time.monotonic() - self._moved_at)
        return max(0.01, remaining) if remaining > 0 else IDLE_TICK

    def resume(self) -> None:
        created, self._new_file = self._new_file, None
        self.load(select=created if created is not None and created.exists() else None)

    # ---- input ------------------------------------------------------------
    def hints(self) -> list[tuple[str, str]]:
        return [
            ("↑↓", "移動"),
            ("Enter", "開く"),
            ("←", "上へ"),
            ("/", "絞り込み"),
            ("f", "探す"),
            ("e", "編集"),
            ("n", "新規"),
            ("q", "閉じる"),
        ]

    def help(self) -> list[tuple[str, str]]:
        return [
            ("↑↓ / j k", "移動（PgUp PgDn / g G でページ・先頭末尾）"),
            ("Enter / →", "開く（フォルダなら中へ）"),
            ("← / Backspace", "ひとつ上のフォルダへ"),
            ("/", "名前で絞り込み（Esc で解除）"),
            ("f", "ファイル名で探す（下の階層も含めてプロジェクト全体から）"),
            ("e", "内蔵エディタで編集"),
            ("o", "既定のアプリで開く（Finder など）"),
            ("n", "新しいファイル"),
            ("m", "新しいフォルダ"),
            ("r", "名前を変更"),
            ("d", "ゴミ箱に移動（確認あり）"),
            ("c", "パスをコピー"),
            (".", "隠しファイルの表示切替"),
            ("~", "ホームフォルダへ"),
            ("マウス", "クリックで選択、ダブルクリックで開く、.. で上へ、ホイールで移動"),
            ("", "下の案内（Enter 開く など）もクリックで押せます"),
            ("q / Esc", "閉じる"),
        ]

    def handle(self, event: Event) -> None:
        if isinstance(event, Mouse):
            self._mouse(event)
            return
        if not isinstance(event, Key):
            return
        name = event.name
        _, height = self.app.terminal.size()
        page = max(1, height - 3)
        actions = {
            "up": lambda: self.move(-1),
            "k": lambda: self.move(-1),
            "down": lambda: self.move(1),
            "j": lambda: self.move(1),
            "pageup": lambda: self.move(-page),
            "pagedown": lambda: self.move(page),
            "g": lambda: self.move(-len(self.entries)),
            "home": lambda: self.move(-len(self.entries)),
            "G": lambda: self.move(len(self.entries)),
            "end": lambda: self.move(len(self.entries)),
            "enter": self.open_selected,
            "right": self.open_selected,
            "l": self.open_selected,
            "left": self.go_up,
            "h": self.go_up,
            "backspace": self.go_up,
            "/": self._ask_filter,
            "f": self._open_search,
            ".": self._toggle_hidden,
            "~": lambda: self.change_directory(Path.home()),
            "e": self._edit_selected,
            "o": self._open_external,
            "n": self._ask_new_file,
            "m": self._ask_new_folder,
            "r": self._ask_rename,
            "d": self._ask_trash,
            "delete": self._ask_trash,
            "c": self._copy_path,
        }
        if name in actions:
            actions[name]()
        elif name in ("q", "escape"):
            if self.filter:
                self.filter = ""
                self.load()
            else:
                self.app.pop()

    def _mouse(self, event: Mouse) -> None:
        width, height = self.app.terminal.size()
        list_width = self._list_width(width)
        if event.kind in ("wheel_up", "wheel_down"):
            delta = -WHEEL_LINES if event.kind == "wheel_up" else WHEEL_LINES
            if event.x < list_width:
                self.move(delta)
            elif self._preview:
                self._preview.scroll_by(delta)
            return
        if (
            event.kind != "press"
            or event.button != 0
            or not mouse_in(event, 1, 0, height - 2, list_width)
        ):
            return
        row = event.y - 1 - self._parent_rows()
        if row < 0:
            self.go_up()
            return
        index = self.top + row
        if index >= len(self.entries):
            return
        now = time.monotonic()
        if self._last_click[1] == index and now - self._last_click[0] < DOUBLE_CLICK_SECONDS:
            self.index = index
            self.open_selected()
            self._last_click = (0.0, -1)
            return
        self._last_click = (now, index)
        if index != self.index:
            self.index = index
            self._invalidate_preview()

    # ---- actions ----------------------------------------------------------
    def change_directory(self, directory: Path, select: Path | None = None) -> None:
        self.directory = directory
        self.filter = ""
        self.index = 0
        self.top = 0
        self.load(select)

    def go_up(self) -> None:
        if self.has_parent:
            self.change_directory(self.directory.parent, select=self.directory)

    def open_selected(self) -> None:
        entry = self.selected
        if entry is None:
            return
        if entry.is_dir and detect(entry.path) is Kind.DIRECTORY:
            self.change_directory(entry.path)
        else:
            self.app.push(ViewerScreen(self.app, entry.path))

    def _open_search(self) -> None:
        self.app.push(SearchScreen(self.app, self.directory))

    def _edit_selected(self) -> None:
        entry = self.selected
        if entry is None or entry.is_dir:
            return
        if not is_editable(detect(entry.path)):
            self.app.error("この形式は内蔵エディタで編集できません（o でアプリを開けます）")
            return
        self.app.push(EditorScreen(self.app, entry.path))

    def _open_external(self) -> None:
        target = self.selected.path if self.selected else self.directory
        error = open_external(target)
        self.app.message(error or f"既定のアプリで開きました: {target.name}")

    def _copy_path(self) -> None:
        target = self.selected.path if self.selected else self.directory
        if copy_to_clipboard(str(target)):
            self.app.message(f"コピーしました: {target}")

    def _toggle_hidden(self) -> None:
        self.show_hidden = not self.show_hidden
        self.load()

    def _ask_filter(self) -> None:
        def change(text: str) -> None:
            self.filter = text
            self._apply_filter()
            self.index = 0
            self._invalidate_preview()

        def cancel() -> None:
            change("")

        self.app.ask(
            "絞り込み（Enter で確定 / Esc で解除）",
            lambda _t: None,
            initial=self.filter,
            on_change=change,
            on_cancel=cancel,
        )

    def _valid_name(self, name: str) -> str | None:
        name = name.strip()
        if not name or name in (".", "..") or "/" in name or "\x00" in name:
            self.app.error("使えない名前です")
            return None
        return name

    def _ask_new_file(self) -> None:
        def submit(text: str) -> None:
            name = self._valid_name(text)
            if not name:
                return
            path = self.directory / name
            if path.exists():
                self.app.error(f"すでにあります: {name}")
                return
            self._new_file = path
            self.app.push(EditorScreen(self.app, path))

        self.app.ask("新しいファイル名（保存すると作成されます）", submit)

    def _ask_new_folder(self) -> None:
        def submit(text: str) -> None:
            name = self._valid_name(text)
            if not name:
                return
            try:
                (self.directory / name).mkdir()
            except OSError as error:
                self.app.error(f"作れませんでした: {error}")
                return
            self.load(select=self.directory / name)

        self.app.ask("新しいフォルダ名", submit)

    def _ask_rename(self) -> None:
        entry = self.selected
        if entry is None:
            return

        def submit(text: str) -> None:
            name = self._valid_name(text)
            if not name or name == entry.name:
                return
            target = self.directory / name
            if target.exists():
                self.app.error(f"すでにあります: {name}")
                return
            try:
                entry.path.rename(target)
            except OSError as error:
                self.app.error(f"変更できませんでした: {error}")
                return
            self.load(select=target)

        self.app.ask("新しい名前", submit, initial=entry.name)

    def _ask_trash(self) -> None:
        entry = self.selected
        if entry is None:
            return

        def trash() -> None:
            error = move_to_trash(entry.path)
            if error:
                self.app.error(f"ゴミ箱に移動できませんでした: {error}")
            else:
                self.app.message(f"ゴミ箱に移動しました: {entry.name}")
            self.load()

        self.app.confirm_choice(
            f"「{entry.name}」をゴミ箱に移動しますか？",
            [Choice("y", "はい", trash), Choice("n", "いいえ", lambda: None)],
        )


# Browser keys that act on "the current folder", which a list of picks does not have.
_FOLDER_ONLY_KEYS = frozenset({"n", "m", "r", ".", "~"})
# Picks are paths rather than names, so the list gets more of the width than the browser's.
PICK_LIST_RATIO = 0.5
PICK_LIST_MAX_WIDTH = 72
# `../../docs/a.md` still reads as "near here"; further away, a `~/` path is clearer.
_MAX_PARENT_STEPS = 2


def label_for(target: Target, base: Path) -> str:
    """How a pick is listed: relative to the pane's folder when near it, with its line."""
    relative = os.path.relpath(target.path, base)
    if relative.split(os.sep).count("..") <= _MAX_PARENT_STEPS:
        shown = relative
    else:
        shown = home_relative(target.path)
    if target.is_dir:
        shown += "/"
    return f"{shown}:{target.line}" if target.line else shown


class PickerScreen(BrowserScreen):
    """The browser's list and live preview, over a fixed set of files.

    The `..` row and `←` leave the list for the ordinary file browser of `base`.
    """

    def __init__(
        self,
        app: App,
        targets: Sequence[Target],
        base: Path,
        heading: str,
        note: str | None = None,
    ) -> None:
        self.targets = list(targets)
        self.heading = heading
        self._lines = {target.path: target.line for target in self.targets}
        super().__init__(app, base)
        if note:
            app.message(note)

    def _read_entries(self) -> list[Entry]:
        entries = []
        for target in self.targets:
            try:
                stat = target.path.stat()
            except OSError:
                continue  # trashed or moved since the screen was read
            name = label_for(target, self.directory)
            entries.append(Entry(name, target.path, target.is_dir, stat.st_size, stat.st_mtime))
        return entries

    @property
    def has_parent(self) -> bool:
        return True

    def _parent_label(self) -> str:
        return "フォルダを見る（ファイルブラウザ）"

    def _heading(self) -> str:
        return self.heading

    def _list_width(self, width: int) -> int:
        if width < PREVIEW_MIN_WIDTH:
            return width
        return max(LIST_MIN_WIDTH, min(PICK_LIST_MAX_WIDTH, int(width * PICK_LIST_RATIO)))

    def _fit_name(self, name: str, width: int) -> str:
        # The file name is at the end of a path; cut the folders in front instead.
        return truncate_left(name, width)

    def go_up(self) -> None:
        self.app.push(BrowserScreen(self.app, self.directory))

    def open_selected(self) -> None:
        entry = self.selected
        if entry is None:
            return
        if entry.is_dir and detect(entry.path) is Kind.DIRECTORY:
            self.app.push(BrowserScreen(self.app, entry.path))
        else:
            self.app.push(ViewerScreen(self.app, entry.path, self._lines.get(entry.path)))

    def _edit_selected(self) -> None:
        entry = self.selected
        if entry is None or entry.is_dir:
            return
        if not is_editable(detect(entry.path)):
            self.app.error("この形式は内蔵エディタで編集できません（o でアプリを開けます）")
            return
        self.app.push(EditorScreen(self.app, entry.path, self._lines.get(entry.path)))

    def hints(self) -> list[tuple[str, str]]:
        return [
            ("↑↓", "移動"),
            ("Enter", "開く"),
            ("e", "編集"),
            ("/", "絞り込み"),
            ("f", "探す"),
            ("←", "フォルダを見る"),
            ("q", "閉じる"),
        ]

    def help(self) -> list[tuple[str, str]]:
        return [
            ("↑↓ / j k", "移動（画面の下の方に出ていたものほど上に並びます）"),
            ("Enter / →", "開く（行番号があればその行へ）"),
            ("e", "内蔵エディタで編集"),
            ("/", "名前で絞り込み（Esc で解除）"),
            ("f", "ファイル名で探す（プロジェクト全体から）"),
            ("← / Backspace", "今いるフォルダをファイルブラウザで見る"),
            ("o", "既定のアプリで開く（Finder など）"),
            ("c", "パスをコピー"),
            ("d", "ゴミ箱に移動（確認あり）"),
            ("マウス", "クリックで選択、ダブルクリックで開く、.. でファイルブラウザへ"),
            ("", "下の案内（Enter 開く など）もクリックで押せます"),
            ("q / Esc", "閉じる"),
        ]

    def handle(self, event: Event) -> None:
        if isinstance(event, Key) and event.name in _FOLDER_ONLY_KEYS:
            return
        super().handle(event)


# Searching is something the user asked for and waits on, so a folder outside git may
# be walked longer than the automatic lookup behind the screen list.
SEARCH_WALK_BUDGET_SECONDS = 3.0
_POLL_SECONDS = 0.05


def _search_index(directory: Path) -> FileIndex:
    return build_index(directory, budget=SEARCH_WALK_BUDGET_SECONDS)


class SearchScreen(PickerScreen):
    """Type to narrow every file of the project; the list and preview follow each key."""

    captures_text = True  # letters go into the query, not to browser shortcuts

    def __init__(
        self,
        app: App,
        directory: Path,
        builder: Callable[[Path], FileIndex] = _search_index,
    ) -> None:
        self.query = ""
        self.total = 0
        self._results: list[str] = []
        self._index: FileIndex | None = None
        super().__init__(app, [], directory, "ファイル名で探す")
        self._future: Future[FileIndex] = app.background(lambda: builder(directory))

    # ---- data -------------------------------------------------------------
    def _read_entries(self) -> list[Entry]:
        index = self._index
        if index is None:
            return []
        entries = []
        for relative in self._results:
            path = index.root / relative
            try:
                stat = path.stat()
            except OSError:
                continue  # listed by git but deleted since
            is_dir = path.is_dir()
            name = relative + ("/" if is_dir else "")
            entries.append(Entry(name, path, is_dir, stat.st_size, stat.st_mtime))
        return entries

    def _search(self) -> None:
        if self._index is None:
            return
        self._results, self.total = rank(self.query, self._index.paths)
        self.index = 0
        self.top = 0
        self.load(select=None)
        # `load` keeps the previous selection when it is still listed; a new query
        # should start from its best match instead.
        self.index = 0
        self._invalidate_preview()

    def tick(self) -> None:
        if self._index is None and self._future.done():
            try:
                self._index = self._future.result()
            except OSError as error:
                self.error = f"探せませんでした: {error}"
                return
            if not self._index.complete:
                self.app.message("大きいフォルダなので一部だけ探しています")
            self._search()

    def tick_interval(self) -> float:
        if self._index is None:
            return _POLL_SECONDS
        return min(super().tick_interval(), IDLE_TICK)

    def resume(self) -> None:
        # Coming back from a file: keep the query and the list as they were.
        current = self.selected.path if self.selected else None
        self.load(select=current)

    # ---- rendering --------------------------------------------------------
    def _heading(self) -> str:
        return f"探す: {self.query}"

    @property
    def has_parent(self) -> bool:
        return False

    def _empty_label(self) -> str:
        if self._index is None:
            return " 探しています…"
        return " （一致なし）" if self.query else " （ファイルがありません）"

    def _count_label(self) -> str:
        if self._index is None:
            return ""
        if self.total > len(self._results):
            return f"{self.total} 件中 上位 {len(self._results)} 件"
        return f"{self.total} 件"

    def render(self, width: int, height: int) -> Frame:
        frame = super().render(width, height)
        # The text cursor sits after the query, like any input field.
        return Frame(frame.lines, (0, 1 + text_width(self._heading())), frame.images)

    def hints(self) -> list[tuple[str, str]]:
        return [("↑↓", "選ぶ"), ("Enter", "開く"), ("Esc", "戻る")]

    def help(self) -> list[tuple[str, str]]:
        return [
            (
                "文字を入力",
                "名前の一部で絞り込み（順番どおりに含んでいれば当たり。例: srcag → src/agent.py）",
            ),
            ("", "ファイル名に当たったもの、続けて当たったもの、短いパスほど上に並びます"),
            ("↑↓ / PgUp PgDn", "選ぶ"),
            ("Enter", "開く（フォルダならファイルブラウザで中を見る）"),
            ("Backspace / Ctrl+U", "1 文字消す / 全部消す"),
            ("マウス", "クリックで選択、ダブルクリックで開く、ホイールで移動"),
            ("Esc", "元の画面に戻る"),
        ]

    # ---- input ------------------------------------------------------------
    def handle(self, event: Event) -> None:
        if isinstance(event, Mouse):
            self._mouse(event)
            return
        if isinstance(event, Paste):
            self._set_query(self.query + " ".join(event.text.split()))
            return
        if not isinstance(event, Key):
            return
        _, height = self.app.terminal.size()
        page = max(1, height - 3)
        moves = {"up": -1, "down": 1, "pageup": -page, "pagedown": page}
        name = event.name
        if name in moves:
            self.move(moves[name])
        elif name == "enter":
            self.open_selected()
        elif name in ("escape", "ctrl+c", "ctrl+g"):
            self.app.pop()
        elif name == "backspace":
            self._set_query(self.query[:-1])
        elif name == "ctrl+u":
            self._set_query("")
        elif event.char and event.char.isprintable():
            self._set_query(self.query + event.char)

    def _set_query(self, query: str) -> None:
        if query != self.query:
            self.query = query
            self._search()
