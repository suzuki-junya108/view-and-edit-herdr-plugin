"""Pick one of several files: the ones on the pane's screen, or all matches of a name."""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from view_and_edit.app import App, home_relative
from view_and_edit.browser import LIST_MIN_WIDTH, PREVIEW_MIN_WIDTH, BrowserScreen
from view_and_edit.documents import Entry
from view_and_edit.editor import EditorScreen
from view_and_edit.formats import Kind, detect, is_editable
from view_and_edit.keys import Event, Key
from view_and_edit.target import Target
from view_and_edit.viewer import ViewerScreen
from view_and_edit.width import truncate_left

# Browser keys that act on "the current folder", which a list of picks does not have.
_FOLDER_ONLY_KEYS = frozenset({"n", "m", "r", ".", "~"})
# Picks are paths rather than names, so the list gets more of the width than the browser's.
LIST_RATIO = 0.5
LIST_MAX_WIDTH = 72
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
        return max(LIST_MIN_WIDTH, min(LIST_MAX_WIDTH, int(width * LIST_RATIO)))

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
            ("←", "フォルダを見る"),
            ("q", "閉じる"),
        ]

    def help(self) -> list[tuple[str, str]]:
        return [
            ("↑↓ / j k", "移動（画面の下の方に出ていたものほど上に並びます）"),
            ("Enter / →", "開く（行番号があればその行へ）"),
            ("e", "内蔵エディタで編集"),
            ("/", "名前で絞り込み（Esc で解除）"),
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
