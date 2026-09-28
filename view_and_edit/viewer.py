"""Viewer screen and the content pane it shares with the browser's preview."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path

from view_and_edit import kitty
from view_and_edit.app import IDLE_TICK, App, Screen, home_relative
from view_and_edit.documents import (
    CHANGES_VIEW,
    Document,
    View,
    open_document,
    page_lines,
    with_changes_view,
)
from view_and_edit.editor import EditorScreen
from view_and_edit.formats import Kind
from view_and_edit.keys import Event, Key, Mouse
from view_and_edit.media import (
    VIDEO_FPS,
    AudioPlayer,
    ImageRequest,
    PreviewError,
    VideoPlayer,
    copy_to_clipboard,
    format_duration,
    media_duration,
    open_external,
)
from view_and_edit.structured import Page
from view_and_edit.ui import Frame, ImagePlacement, Line, Style, highlight_matches, render_line
from view_and_edit.width import text_width

HSCROLL_STEP = 8
SEEK_SECONDS = 5.0
WHEEL_LINES = 3
AUDIO_TICK = 0.25  # refresh the elapsed-time display while audio plays
VIEWER_IMAGE_SLOT = 1
TABS_ROW = 1  # just under the title
PLAYER_ROW_FROM_BOTTOM = 2  # the play/pause line sits right above the footer
PREVIEW_IMAGE_SLOT = 2
_ASPECT_BUCKETS = 20  # re-render HTML screenshots only when the box shape changes noticeably


@dataclass
class _ImageState:
    key: tuple[object, ...]
    future: Future[bytes]


class ContentPane:
    """Renders one Document view into a box: text lines, or an image loaded in the background."""

    def __init__(self, app: App, doc: Document, image_slot: int, line: int | None = None) -> None:
        self.app = app
        self.doc = doc
        self.image_slot = image_slot
        self.view_index = 0
        self.page = 0
        self.scroll = max(0, (line or 1) - 1 - 3) if line else 0
        self.hscroll = 0
        self.query = ""
        self.focus_line = (line - 1) if line else None
        self._image: _ImageState | None = None
        self.video_frame: bytes | None = None
        self._last_height = 20
        self._last_width = 80
        self._last_pages: list[Page] = []

    # ---- view / page state --------------------------------------------------
    @property
    def view(self) -> View:
        return self.doc.views[self.view_index]

    def cycle_view(self, delta: int = 1) -> None:
        if len(self.doc.views) > 1:
            self.view_index = (self.view_index + delta) % len(self.doc.views)
            self.page = 0
            self.scroll = 0
            self.hscroll = 0
            self.focus_line = None

    def select_view(self, index: int) -> None:
        if 0 <= index < len(self.doc.views) and index != self.view_index:
            self.cycle_view(index - self.view_index)

    def page_count(self) -> int:
        view = self.view
        if view.image is not None:
            return view.page_count() if view.page_count else 1
        return len(self._last_pages) or 1

    def page_title(self) -> str:
        if self.view.pages is not None and self._last_pages:
            return self._last_pages[min(self.page, len(self._last_pages) - 1)].title
        return ""

    def turn_page(self, delta: int) -> bool:
        count = self.page_count()
        if count <= 1:
            return False
        self.page = (self.page + delta) % count
        self.scroll = 0
        self.hscroll = 0
        return True

    def lines(self, width: int) -> Sequence[Line]:
        if self.view.pages is None:
            return []
        self._last_width = width
        self._last_pages = self.view.pages(width)
        return page_lines(self._last_pages, self.page)

    # ---- scrolling -----------------------------------------------------------
    def scroll_by(self, delta: int) -> None:
        self.scroll = max(0, self.scroll + delta)

    def scroll_to_end(self) -> None:
        self.scroll = 1 << 30

    def horizontal(self, delta: int) -> None:
        self.hscroll = max(0, self.hscroll + delta)

    def search(self, query: str, forward: bool = True) -> bool:
        self.query = query
        if not query or self.view.pages is None:
            return False
        lines = self.lines(self._last_width)  # search may run before the first draw
        total = len(lines)
        start = (self.focus_line if self.focus_line is not None else self.scroll - 1) + (
            1 if forward else -1
        )
        needle = query.lower()
        for step in range(total):
            index = (start + (step if forward else -step)) % total
            if needle in lines[index].text.lower():
                self.focus_line = index
                self.scroll = max(0, index - self._last_height // 3)
                return True
        return False

    # ---- rendering -----------------------------------------------------------
    def render(
        self, row: int, col: int, width: int, height: int
    ) -> tuple[list[Line], ImagePlacement | None]:
        self._last_height = height
        if self.view.image is not None:
            return self._render_image(row, col, width, height)
        return self._render_text(width, height), None

    def _render_text(self, width: int, height: int) -> list[Line]:
        lines = self.lines(width - (self._gutter_width(0) if self.view.numbered else 0))
        total = len(lines)
        gutter = self._gutter_width(total) if self.view.numbered else 0
        self.scroll = max(0, min(self.scroll, max(0, total - height)))
        out: list[Line] = []
        for index in range(self.scroll, min(total, self.scroll + height)):
            line = highlight_matches(lines[index], self.query)
            body = line.window(self.hscroll, width - gutter)
            if gutter:
                number_style = Style.ACCENT if index == self.focus_line else Style.DIM
                body = Line(
                    [*Line.of(f"{index + 1:>{gutter - 1}} ", number_style).spans, *body.spans]
                )
            if index == self.focus_line:
                body = body.styled(Style.UNDERLINE) if not gutter else body
            out.append(body)
        return out

    @staticmethod
    def _gutter_width(total: int) -> int:
        return len(str(max(total, 1))) + 1 if total else 0

    def _render_image(
        self, row: int, col: int, width: int, height: int
    ) -> tuple[list[Line], ImagePlacement | None]:
        caption_rows = 1
        box_rows = max(1, height - caption_rows)
        png = self.video_frame
        if png is None:
            png_or_status = self._load_image(width, box_rows)
            if isinstance(png_or_status, str):
                return [Line(), Line.of("  " + png_or_status, Style.DIM)], None
            png = png_or_status
        try:
            size = kitty.png_size(png)
        except kitty.NotPngError:
            return [Line.of("  画像を読み込めませんでした", Style.ERROR)], None
        # Poster and playback frames differ in resolution; filling the box keeps
        # the video the same size on screen in both states.
        cols, rows = kitty.fit_cells(
            size, width, box_rows, self.app.cell_size(), upscale=self.doc.kind is Kind.VIDEO
        )
        left = (width - cols) // 2
        caption = f"{size[0]}×{size[1]}"
        if self.page_count() > 1:
            caption = f"ページ {self.page + 1}/{self.page_count()}   " + caption
        lines = [Line() for _ in range(rows)]
        lines.append(Line.of(" " * max(0, (width - text_width(caption)) // 2) + caption, Style.DIM))
        return lines, ImagePlacement(self.image_slot, png, row, col + left, cols, rows)

    def _load_image(self, width: int, height: int) -> bytes | str:
        image_fn = self.view.image
        if image_fn is None:
            return ""
        cell = self.app.cell_size()
        aspect = (height * cell.height) / max(1.0, width * cell.width)
        bucketed = round(aspect * _ASPECT_BUCKETS) / _ASPECT_BUCKETS
        key = (self.view_index, self.page, bucketed if self.doc.kind is Kind.HTML else 0)
        if self._image is None or self._image.key != key:
            request = ImageRequest(page=self.page, aspect=bucketed)

            def load() -> bytes:
                return image_fn(request).read_bytes()

            self._image = _ImageState(key, self.app.background(load))
        future = self._image.future
        if not future.done():
            return "読み込み中…"
        error = future.exception()
        if error is not None:
            if isinstance(error, (PreviewError, OSError)):
                return str(error)
            return f"表示できませんでした: {error}"
        return future.result()


class ViewerScreen(Screen):
    def __init__(self, app: App, path: Path, line: int | None = None) -> None:
        self.app = app
        self.path = path
        self.pane = ContentPane(app, self._open(), VIEWER_IMAGE_SLOT, line)
        self.video: VideoPlayer | None = None
        self.audio: AudioPlayer | None = None
        self._duration: float | None = None
        # Clickable column spans of the tabs row as last drawn: (start, end, action).
        self._tab_targets: list[tuple[int, int, Callable[[], None]]] = []

    @property
    def doc(self) -> Document:
        return self.pane.doc

    def _open(self) -> Document:
        return with_changes_view(open_document(self.path, self.app.cache))

    def _changes_index(self) -> int | None:
        for index, view in enumerate(self.doc.views):
            if view.name == CHANGES_VIEW:
                return index
        return None

    def reload(self) -> None:
        old = self.pane
        self.pane = ContentPane(self.app, self._open(), VIEWER_IMAGE_SLOT)
        self.pane.view_index = min(old.view_index, len(self.pane.doc.views) - 1)
        self.pane.scroll, self.pane.page, self.pane.query = old.scroll, old.page, old.query

    def resume(self) -> None:
        if self.path.exists():
            self.reload()

    # ---- layout -----------------------------------------------------------
    def render(self, width: int, height: int) -> Frame:
        title = (
            Line()
            .add(f" {self.path.name} ", Style.TITLE)
            .add(f"  {home_relative(self.path.parent)}", Style.DIM)
        )
        facts = "  ".join(
            value for label, value in self.doc.facts if label in ("種類", "サイズ", "長さ", "映像")
        )
        body_top = 2
        body_height = height - body_top - 1
        body, image = self.pane.render(
            body_top, 0, width, body_height - (1 if self.doc.playable else 0)
        )
        tabs = self._tabs_line(width)  # after rendering: page titles come from the content
        if self.doc.playable:
            body = [*body[: body_height - 1]]
            body += [Line()] * (body_height - 1 - len(body))
            body.append(self._player_line())
        header_right = Line.of(facts + " ", Style.DIM)
        header = Line([*title.window(0, max(0, width - header_right.width() - 2)).spans])
        header.add(" " * max(0, width - header.width() - header_right.width()))
        header.spans.extend(header_right.spans)
        lines = [render_line(header, width), render_line(tabs, width)]
        lines += [render_line(line, width) for line in body[:body_height]]
        return Frame(lines, images=[image] if image else [])

    def _tabs_line(self, width: int) -> Line:
        line = Line.of(" ")
        targets: list[tuple[int, int, Callable[[], None]]] = []

        def clickable(text: str, style: str, action: Callable[[], None]) -> None:
            start = line.width()
            line.add(text, style)
            targets.append((start, line.width(), action))

        if len(self.doc.views) > 1:
            for index, view in enumerate(self.doc.views):
                style = Style.REVERSE if index == self.pane.view_index else Style.DIM
                clickable(f" {view.name} ", style, self._view_selector(index))
                line.add(" ")
            line.add(" Tab で切替", Style.DIM)
        count = self.pane.page_count()
        title = self.pane.page_title()
        if count > 1:
            line.add("   ")
            clickable("◀", Style.ACCENT, lambda: self._left_right(-1))
            line.add(f" {self.pane.page + 1}/{count} ", Style.ACCENT)
            clickable("▶", Style.ACCENT, lambda: self._left_right(1))
            line.add(f" {title}", Style.ACCENT)
        elif title:
            line.add(f"   {title}", Style.ACCENT)
        # A text file shows its encoding while it has no tabs besides its changes.
        own_views = len(self.doc.views) - (0 if self._changes_index() is None else 1)
        for label, value in self.doc.facts:
            if label in ("文字コード", "改行") and own_views == 1:
                line.add(f"  {value}", Style.DIM)
        self._tab_targets = targets
        return line.window(0, width)

    def _player_line(self) -> Line:
        if self.doc.playable == "video" and not VideoPlayer.available():
            return Line.of("  再生には ffmpeg が必要です。o で既定のアプリで開けます", Style.DIM)
        if self.doc.playable == "audio" and not AudioPlayer.available():
            return Line.of(
                "  再生できるコマンドがありません。o で既定のアプリで開けます", Style.DIM
            )
        position = 0.0
        state = "■ 停止中"
        if self.video:
            position = self.video.position()
            state = "❚❚ 一時停止" if self.video.paused else "▶ 再生中"
        elif self.audio and self.audio.process:
            position = self.audio.position()
            state = "❚❚ 一時停止" if self.audio.paused else "▶ 再生中"
        if self._duration is None:
            self._duration = media_duration(self.path) or 0.0
        total = format_duration(self._duration) if self._duration else "?"
        return Line.of(f"  {state}   {format_duration(position)} / {total}", Style.ACCENT)

    def tick_interval(self) -> float:
        if self.video and not self.video.paused:
            return 1 / (VIDEO_FPS * 2)
        if self.audio and self.audio.playing:
            return AUDIO_TICK
        return IDLE_TICK

    def tick(self) -> None:
        if self.video:
            frame = self.video.due_frame()
            if frame is not None:
                self.pane.video_frame = frame
            if self.video.ended:
                self.video.stop()
                self.video = None
                self.pane.video_frame = None
        if self.audio and self.audio.finished:
            self.audio.stop()
            self.audio = None

    # ---- input ------------------------------------------------------------
    def hints(self) -> list[tuple[str, str]]:
        hints: list[tuple[str, str]] = []
        if self.doc.playable:
            hints += [("Space", "再生/停止"), ("←", "5秒戻る"), ("→", "5秒送る")]
        elif self.pane.page_count() > 1:
            hints += [("←", "前ページ"), ("→", "次ページ"), ("↑↓", "スクロール")]
        elif self.pane.view.image is None:
            hints += [("↑↓", "スクロール"), ("/", "検索")]
        if len(self.doc.views) > 1:
            hints.append(("Tab", "表示切替"))
        if self._changes_index() is not None:
            hints.append(("d", "変更点"))
        if self.doc.editable:
            hints.append(("e", "編集"))
        hints += [("o", "アプリで開く"), ("q", "戻る")]
        return hints

    def help(self) -> list[tuple[str, str]]:
        return [
            ("↑↓ / j k", "1 行スクロール"),
            ("PgUp PgDn / Space", "1 画面スクロール（音声・動画では Space は再生/停止）"),
            ("g / G", "先頭 / 末尾"),
            ("←→", "ページ移動（無ければ横スクロール、動画・音声は 5 秒移動）"),
            ("Shift+←→", "横スクロール"),
            ("Tab", "表示切替（表示 ⇄ ソース など）"),
            (
                "d",
                "変更点（最後のコミットからの差分）⇄ 元の表示。Git で管理された文字のファイルのみ",
            ),
            ("/  n  N", "検索 / 次 / 前"),
            ("e", "内蔵エディタで編集"),
            ("o", "既定のアプリで開く"),
            ("c", "パスをコピー"),
            ("r", "再読み込み"),
            ("q / Esc", "戻る"),
            ("マウス", "ホイールでスクロール。表示の名前・◀ ▶・再生行はクリックで操作"),
            ("", "下の案内（q 戻る など）もクリックで押せます"),
        ]

    def handle(self, event: Event) -> None:
        if isinstance(event, Mouse):
            self._mouse(event)
            return
        if not isinstance(event, Key):
            return
        name = event.name
        _, height = self.app.terminal.size()
        page = max(1, height - 5)
        pane = self.pane
        simple = {
            "up": lambda: pane.scroll_by(-1),
            "k": lambda: pane.scroll_by(-1),
            "down": lambda: pane.scroll_by(1),
            "j": lambda: pane.scroll_by(1),
            "pageup": lambda: pane.scroll_by(-page),
            "b": lambda: pane.scroll_by(-page),
            "pagedown": lambda: pane.scroll_by(page),
            "g": lambda: setattr(pane, "scroll", 0),
            "home": lambda: setattr(pane, "scroll", 0),
            "G": pane.scroll_to_end,
            "end": pane.scroll_to_end,
            "shift+left": lambda: pane.horizontal(-HSCROLL_STEP),
            "shift+right": lambda: pane.horizontal(HSCROLL_STEP),
            "tab": lambda: self._switch_view(1),
            "backtab": lambda: self._switch_view(-1),
            "n": lambda: self._find_next(True),
            "N": lambda: self._find_next(False),
            "r": self.reload,
            "c": self._copy_path,
            "q": self.app.pop,
            "escape": self.app.pop,
            "/": lambda: self.app.ask("検索", self._start_search, initial=pane.query),
            "e": self._edit,
            "o": self._open_external,
            "d": self._toggle_changes,
        }
        if name in simple:
            simple[name]()
        elif name == " ":
            if self.doc.playable:
                self._toggle_play()
            else:
                pane.scroll_by(page)
        elif name in ("left", "right", "h", "l"):
            self._left_right(-1 if name in ("left", "h") else 1)

    def _mouse(self, event: Mouse) -> None:
        if event.kind == "wheel_up":
            self.pane.scroll_by(-WHEEL_LINES)
        elif event.kind == "wheel_down":
            self.pane.scroll_by(WHEEL_LINES)
        if event.kind != "press" or event.button != 0:
            return
        _, height = self.app.terminal.size()
        if event.y == TABS_ROW:
            for start, end, action in self._tab_targets:
                if start <= event.x < end:
                    action()
                    return
        elif self.doc.playable and event.y == height - PLAYER_ROW_FROM_BOTTOM:
            self._toggle_play()

    def _view_selector(self, index: int) -> Callable[[], None]:
        def select() -> None:
            self.pane.select_view(index)
            self.app.invalidate()

        return select

    def _toggle_changes(self) -> None:
        changes = self._changes_index()
        if changes is None:
            self.app.error("変更点は、Git で管理されている文字のファイルだけ表示できます")
            return
        self.pane.select_view(0 if self.pane.view_index == changes else changes)
        self.app.invalidate()

    def _open_external(self) -> None:
        error = open_external(self.path)
        self.app.message(error or "既定のアプリで開きました")

    def _switch_view(self, delta: int) -> None:
        self.pane.cycle_view(delta)
        self.app.invalidate()

    def _left_right(self, direction: int) -> None:
        if self.doc.playable:
            self._seek(direction * SEEK_SECONDS)
        elif not self.pane.turn_page(direction):
            self.pane.horizontal(direction * HSCROLL_STEP)

    def _start_search(self, query: str) -> None:
        if query and not self.pane.search(query):
            self.app.error(f"見つかりません: {query}")

    def _find_next(self, forward: bool) -> None:
        if self.pane.query and not self.pane.search(self.pane.query, forward):
            self.app.error(f"見つかりません: {self.pane.query}")

    def _copy_path(self) -> None:
        if copy_to_clipboard(str(self.path)):
            self.app.message(f"コピーしました: {self.path}")
        else:
            self.app.error("クリップボードにコピーできませんでした")

    def _edit(self) -> None:
        if not self.doc.editable:
            self.app.error("この形式は内蔵エディタで編集できません（o でアプリを開けます）")
            return
        line = (
            (self.pane.focus_line + 1) if self.pane.focus_line is not None else self.pane.scroll + 1
        )
        self.app.push(EditorScreen(self.app, self.path, line if self.pane.view.numbered else None))

    # ---- playback ---------------------------------------------------------
    def _toggle_play(self) -> None:
        if self.doc.playable == "video":
            if self.video is None:
                if not VideoPlayer.available():
                    self.app.error("動画の再生には ffmpeg が必要です")
                    return
                self.video = VideoPlayer(self.path, self.app.wake)
                self.video.start()
            else:
                self.video.toggle_pause()
        elif self.doc.playable == "audio":
            if self.audio is None or self.audio.process is None:
                if not AudioPlayer.available():
                    self.app.error("音声を再生できるコマンドがありません")
                    return
                self.audio = AudioPlayer(self.path)
                self.audio.start()
            else:
                self.audio.toggle_pause()

    def _seek(self, delta: float) -> None:
        if self.video:
            target = max(0.0, self.video.position() + delta)
            if self._duration:
                target = min(target, max(0.0, self._duration - 1))
            self.video.start(target)
        elif self.audio and self.audio.process:
            if not self.audio.can_seek:
                self.app.error("早送り・巻き戻しには ffplay (ffmpeg) が必要です")
                return
            self.audio.start(max(0.0, self.audio.position() + delta))

    def close(self) -> None:
        if self.video:
            self.video.stop()
        if self.audio:
            self.audio.stop()
