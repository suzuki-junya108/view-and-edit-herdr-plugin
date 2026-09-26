"""Full-screen app shell: terminal I/O, the screen stack, prompts, and image layers."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import select
import signal
import struct
import sys
import termios
import time
import tty
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeVar

from view_and_edit import kitty
from view_and_edit.keys import Event, InputParser, Key, Mouse, Paste
from view_and_edit.media import Cache
from view_and_edit.textbuf import TextBuffer
from view_and_edit.ui import Frame, ImagePlacement, Line, Style, hint_bar, render_line
from view_and_edit.width import text_width

ESCAPE_TIMEOUT = 0.05
MESSAGE_SECONDS = 4.0
IDLE_TICK = 0.5
_ENTER_SCREEN = "\x1b[?1049h\x1b[?25l\x1b[?1000h\x1b[?1002h\x1b[?1006h\x1b[?2004h"
_LEAVE_SCREEN = "\x1b[?2004l\x1b[?1006l\x1b[?1002l\x1b[?1000l\x1b[?25h\x1b[0m\x1b[?1049l"

T = TypeVar("T")

# Footer hints whose label is not simply the key to press (`e`, `q`, `/` press themselves).
_HINT_KEYS = {
    "Enter": "enter",
    "Tab": "tab",
    "Space": " ",
    "Esc": "escape",
    "F1": "f1",
    "←": "left",
    "→": "right",
}
_CTRL_HINT_LENGTH = 2  # `^S` and the like


def hint_key(label: str) -> str | None:
    """The key a footer hint stands for, or None when it names several keys (`↑↓`)."""
    if label in _HINT_KEYS:
        return _HINT_KEYS[label]
    if len(label) == _CTRL_HINT_LENGTH and label.startswith("^"):
        return "ctrl+" + label[1].lower()
    if len(label) == 1 and label.isprintable() and label not in "↑↓":
        return label
    return None


class Terminal(Protocol):
    def size(self) -> tuple[int, int]: ...
    def cell_size(self) -> kitty.CellSize: ...
    def write(self, data: str) -> None: ...


class TtyTerminal:
    def __init__(self, fd_in: int, fd_out: int) -> None:
        self.fd_in = fd_in
        self.fd_out = fd_out
        self._saved: list[Any] | None = None

    def __enter__(self) -> TtyTerminal:
        self._saved = termios.tcgetattr(self.fd_in)
        tty.setraw(self.fd_in)
        self.write(_ENTER_SCREEN)
        return self

    def __exit__(self, *_exc: object) -> None:
        self.write(_LEAVE_SCREEN)
        if self._saved is not None:
            termios.tcsetattr(self.fd_in, termios.TCSADRAIN, self._saved)

    def _winsize(self) -> tuple[int, int, int, int]:
        try:
            packed = fcntl.ioctl(self.fd_out, termios.TIOCGWINSZ, b"\0" * 8)
            rows, cols, xpix, ypix = struct.unpack("HHHH", packed)
            return rows, cols, xpix, ypix
        except OSError:
            return 24, 80, 0, 0

    def size(self) -> tuple[int, int]:
        rows, cols, _, _ = self._winsize()
        return max(cols, 20), max(rows, 5)

    def cell_size(self) -> kitty.CellSize:
        rows, cols, xpix, ypix = self._winsize()
        if xpix and ypix and cols and rows:
            return kitty.CellSize(xpix / cols, ypix / rows)
        return kitty.FALLBACK_CELL

    def write(self, data: str) -> None:
        encoded = data.encode("utf-8", "replace")
        while encoded:
            written = os.write(self.fd_out, encoded)
            encoded = encoded[written:]


class Screen:
    """One full-screen mode (browser, viewer, editor). Subclasses override hooks."""

    app: App

    def title(self) -> str:
        return ""

    def render(self, width: int, height: int) -> Frame:
        raise NotImplementedError

    def handle(self, event: Event) -> None:
        pass

    def hints(self) -> list[tuple[str, str]]:
        return []

    def help(self) -> list[tuple[str, str]]:
        return self.hints()

    def tick(self) -> None:
        """Called periodically and after background work finishes."""

    def tick_interval(self) -> float:
        """Longest the app may sleep before calling `tick` again."""
        return IDLE_TICK

    def resume(self) -> None:
        """Called when a screen above this one closes."""

    def close(self) -> None:
        """Release resources (players, processes)."""


@dataclass
class Prompt:
    """Single-line input shown in the footer (filter, find, rename, ...)."""

    label: str
    on_submit: Callable[[str], None]
    on_change: Callable[[str], None] | None = None
    on_cancel: Callable[[], None] | None = None
    buffer: TextBuffer = field(default_factory=TextBuffer)

    @property
    def text(self) -> str:
        return self.buffer.lines[0]


@dataclass(frozen=True)
class Choice:
    """One answer to a `Confirm`: pressed with `key` or clicked in the footer."""

    key: str
    label: str
    action: Callable[[], None]


@dataclass
class Confirm:
    """A yes/no style question answered with one key or a click."""

    question: str
    choices: Sequence[Choice]
    on_cancel: Callable[[], None] | None = None
    cancel_label: str | None = None  # shown as an `Esc` button when set


class App:
    def __init__(self, terminal: Terminal, cache: Cache | None = None) -> None:
        self.terminal = terminal
        self.cache = cache or Cache()
        self.screens: list[Screen] = []
        self.prompt: Prompt | None = None
        self.confirm: Confirm | None = None
        self.help_visible = False
        self.running = True
        self._message: tuple[str, str, float] | None = None
        self._last_lines: list[str] = []
        self._shown_images: dict[int, tuple[bytes, tuple[int, int, int, int]]] = {}
        self._executor = ThreadPoolExecutor(max_workers=3)
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_w, False)
        self._force_redraw = True
        # Clickable column spans of the footer as last drawn: (start, end, action).
        self._footer_targets: list[tuple[int, int, Callable[[], None]]] = []

    # ---- screen stack -----------------------------------------------------
    def push(self, screen: Screen) -> None:
        screen.app = self
        self.screens.append(screen)
        self.invalidate()

    def pop(self) -> None:
        if self.screens:
            self.screens.pop().close()
        if not self.screens:
            self.running = False
            return
        self.screens[-1].resume()
        self.invalidate()

    def replace(self, screen: Screen) -> None:
        if self.screens:
            self.screens.pop().close()
        self.push(screen)

    @property
    def top(self) -> Screen:
        return self.screens[-1]

    def quit(self) -> None:
        while self.screens:
            self.screens.pop().close()
        self.running = False

    # ---- helpers for screens ----------------------------------------------
    def message(self, text: str, style: str = Style.ACCENT) -> None:
        self._message = (text, style, time.monotonic() + MESSAGE_SECONDS)

    def error(self, text: str) -> None:
        self.message(text, Style.ERROR)

    def ask(
        self,
        label: str,
        on_submit: Callable[[str], None],
        initial: str = "",
        on_change: Callable[[str], None] | None = None,
        on_cancel: Callable[[], None] | None = None,
    ) -> None:
        prompt = Prompt(label, on_submit, on_change, on_cancel)
        if initial:
            prompt.buffer.insert(initial)
        self.prompt = prompt

    def confirm_choice(
        self,
        question: str,
        choices: Sequence[Choice],
        on_cancel: Callable[[], None] | None = None,
        cancel_label: str | None = None,
    ) -> None:
        self.confirm = Confirm(question, choices, on_cancel, cancel_label)

    def background(self, fn: Callable[[], T]) -> Future[T]:
        """Run slow work (conversions) off the UI thread; the UI redraws when it finishes."""
        future = self._executor.submit(fn)
        future.add_done_callback(lambda _f: self.wake())
        return future

    def wake(self) -> None:
        with contextlib.suppress(BlockingIOError):  # a pending wake-up is enough
            os.write(self._wake_w, b"x")

    def invalidate(self) -> None:
        self._force_redraw = True

    def cell_size(self) -> kitty.CellSize:
        return self.terminal.cell_size()

    # ---- rendering --------------------------------------------------------
    def compose(self) -> Frame:
        width, height = self.terminal.size()
        if not self.screens:
            return Frame([""] * height)
        frame = self.top.render(width, height)
        lines = frame.lines[: height - 1]
        lines += [render_line(Line(), width)] * (height - 1 - len(lines))
        cursor = frame.cursor
        images = frame.images
        footer, footer_cursor = self._footer(width)
        if footer_cursor is not None:
            cursor = (height - 1, footer_cursor)
        if self.help_visible:
            lines = self._help_overlay(width, height - 1)
            images = []
            cursor = None
        return Frame([*lines, footer], cursor, images)

    def _footer(self, width: int) -> tuple[str, int | None]:
        self._footer_targets = []
        if self.confirm:
            return render_line(self._confirm_line(self.confirm), width), None
        if self.prompt:
            label = f" {self.prompt.label}: "
            text = self.prompt.text
            cursor_col = text_width(label) + text_width(text[: self.prompt.buffer.cursor.col])
            line = Line().add(label, Style.BOLD).add(text)
            return render_line(line, width), min(cursor_col, width - 1)
        message = None
        if self._message and self._message[2] > time.monotonic():
            message = Line.of(" " + self._message[0], self._message[1])
        hints = self.top.hints() if self.screens else []
        if self.screens and not getattr(self.top, "captures_text", False):
            hints = [*hints, ("?", "ヘルプ")]  # where `?` is typed, the screen offers F1 instead
        # With a message, trailing hints make room for it; the leading ones keep their
        # place so a click right after a message still lands. A message is never cut off.
        room = width - message.width() - 1 if message is not None else width
        line, spans = hint_bar(hints, max(0, room))
        if message is not None:
            line.add(" " * max(0, width - line.width() - message.width() - 1))
            line.spans.extend(message.spans)
        for start, end, label in spans:
            key = hint_key(label)
            if key is not None:
                self._footer_targets.append((start, end, self._presser(key)))
        return render_line(line, width, Style.PLAIN), None

    def _presser(self, key: str) -> Callable[[], None]:
        return lambda: self.dispatch(Key(key))

    def _confirm_line(self, confirm: Confirm) -> Line:
        line = Line().add(" " + confirm.question + " ", Style.REVERSE)
        buttons = [(c.key, c.label, self._answer(c)) for c in confirm.choices]
        if confirm.cancel_label:
            buttons.append(("Esc", confirm.cancel_label, self._cancel_confirm))
        for key, label, action in buttons:
            line.add("  ")
            start = line.width()
            line.add(f" {key} {label} ", Style.BUTTON)
            self._footer_targets.append((start, line.width(), action))
        return line

    def _answer(self, choice: Choice) -> Callable[[], None]:
        def answer() -> None:
            self.confirm = None
            choice.action()

        return answer

    def _cancel_confirm(self) -> None:
        confirm, self.confirm = self.confirm, None
        if confirm and confirm.on_cancel:
            confirm.on_cancel()

    def _help_overlay(self, width: int, height: int) -> list[str]:
        rows = [Line.of(" キー操作  （何かキーを押すか、クリックで閉じます）", Style.TITLE), Line()]
        for key, label in self.top.help():
            rows.append(Line().add(f"  {key:<14}", Style.BOLD).add(label))
        rows = rows[:height]
        return [
            render_line(row, width, Style.TITLE if i == 0 else "") for i, row in enumerate(rows)
        ] + [render_line(Line(), width)] * (height - len(rows))

    def draw(self) -> None:
        frame = self.compose()
        out: list[str] = []
        force = self._force_redraw or len(frame.lines) != len(self._last_lines)
        if force:
            out.append("\x1b[0m\x1b[2J")
        for row, line in enumerate(frame.lines):
            if force or self._last_lines[row] != line:
                out.append(f"\x1b[{row + 1};1H{line}")
        out.append(self._image_updates(frame.images, force))
        if frame.cursor is not None:
            out.append(f"\x1b[{frame.cursor[0] + 1};{frame.cursor[1] + 1}H\x1b[?25h")
        else:
            out.append("\x1b[?25l")
        self._last_lines = frame.lines
        self._force_redraw = False
        self.terminal.write("".join(out))

    def _image_updates(self, images: list[ImagePlacement], force: bool) -> str:
        out: list[str] = []
        wanted = {img.image_id: img for img in images}
        for image_id in list(self._shown_images):
            if image_id not in wanted:
                out.append(kitty.delete_image(image_id))
                del self._shown_images[image_id]
        for image_id, img in wanted.items():
            digest = hashlib.blake2b(img.png, digest_size=16).digest()
            rect = (img.row, img.col, img.cols, img.rows)
            shown = self._shown_images.get(image_id)
            if shown and shown == (digest, rect) and not force:
                continue
            if shown:
                out.append(kitty.delete_placements(image_id))
            if not shown or shown[0] != digest:
                out.append(kitty.transmit(image_id, img.png))
            out.append(kitty.place(image_id, img.row, img.col, img.cols, img.rows))
            self._shown_images[image_id] = (digest, rect)
        return "".join(out)

    def clear_images(self) -> None:
        self.terminal.write("".join(kitty.delete_image(i) for i in self._shown_images))
        self._shown_images.clear()

    # ---- input ------------------------------------------------------------
    def dispatch(self, event: Event) -> None:
        pressed = isinstance(event, Mouse) and event.kind == "press"
        if self.help_visible:
            if isinstance(event, Key) or pressed:
                self.help_visible = False
                self.invalidate()
            return
        if isinstance(event, Mouse) and event.y == self.terminal.size()[1] - 1:
            if pressed and event.button == 0:
                self._click_footer(event.x)
            return
        if self.confirm:
            self._confirm_event(event)
            return
        if self.prompt:
            self._prompt_event(event)
            return
        if (
            isinstance(event, Key)
            and event.name == "?"
            and not getattr(self.top, "captures_text", False)
        ):
            self.help_visible = True
            return
        if isinstance(event, Key) and event.name == "f1":
            self.help_visible = True
            return
        self.top.handle(event)

    def _click_footer(self, x: int) -> None:
        for start, end, action in list(self._footer_targets):
            if start <= x < end:
                action()
                return

    def _confirm_event(self, event: Event) -> None:
        if not isinstance(event, Key) or self.confirm is None:
            return
        if event.name in ("escape", "ctrl+c", "ctrl+g"):
            self._cancel_confirm()
            return
        for choice in self.confirm.choices:
            if choice.key == event.name.lower():
                self._answer(choice)()
                return

    def _prompt_event(self, event: Event) -> None:
        prompt = self.prompt
        if prompt is None:
            return
        buf = prompt.buffer
        before = prompt.text
        if isinstance(event, Paste):
            buf.insert(event.text.replace("\n", " "))
        elif isinstance(event, Key):
            name = event.name
            if name == "enter":
                self.prompt = None
                prompt.on_submit(prompt.text)
                return
            if name in ("escape", "ctrl+c", "ctrl+g"):
                self.prompt = None
                if prompt.on_cancel:
                    prompt.on_cancel()
                return
            edits: dict[str, Callable[[], None]] = {
                "backspace": buf.backspace,
                "delete": buf.delete,
                "left": buf.left,
                "right": buf.right,
                "home": buf.home,
                "ctrl+a": buf.home,
                "end": buf.end,
                "ctrl+e": buf.end,
                "ctrl+u": self._clear_prompt,
            }
            if name in edits:
                edits[name]()
            elif event.char and event.char.isprintable():
                buf.insert(event.char)
        if prompt.on_change and prompt.text != before:
            prompt.on_change(prompt.text)

    def _clear_prompt(self) -> None:
        if self.prompt:
            self.prompt.buffer.select_all()
            self.prompt.buffer.backspace()

    # ---- main loop --------------------------------------------------------
    def run(self, fd_in: int) -> None:
        parser = InputParser()
        resized = [False]

        def on_resize(_signum: int, _frame: object) -> None:
            resized[0] = True
            self.wake()

        previous = signal.signal(signal.SIGWINCH, on_resize)
        try:
            while self.running and self.screens:
                self.top.tick()
                if not self.running:
                    break
                if resized[0]:
                    resized[0] = False
                    self.invalidate()
                self.draw()
                timeout = ESCAPE_TIMEOUT if parser.has_pending_escape else self.top.tick_interval()
                ready, _, _ = select.select([fd_in, self._wake_r], [], [], timeout)
                if self._wake_r in ready:
                    os.read(self._wake_r, 4096)
                if fd_in in ready:
                    data = os.read(fd_in, 65536)
                    if not data:
                        break
                    for event in parser.feed(data):
                        self.dispatch(event)
                elif parser.has_pending_escape:
                    for event in parser.flush():
                        self.dispatch(event)
        finally:
            signal.signal(signal.SIGWINCH, previous)
            self.clear_images()
            while self.screens:
                self.screens.pop().close()
            self._executor.shutdown(wait=False, cancel_futures=True)


def run_app(first: Callable[[App], Screen]) -> int:
    if not sys.stdin.isatty():
        print("ターミナルから起動してください", file=sys.stderr)
        return 1
    fd_in, fd_out = sys.stdin.fileno(), sys.stdout.fileno()
    with TtyTerminal(fd_in, fd_out) as terminal:
        app = App(terminal)
        app.push(first(app))
        app.run(fd_in)
    return 0


def mouse_in(event: Mouse, row: int, col: int, rows: int, cols: int) -> bool:
    return row <= event.y < row + rows and col <= event.x < col + cols


def home_relative(path: Path) -> str:
    home = Path.home()
    try:
        return "~/" + str(path.relative_to(home)) if path != home else "~"
    except ValueError:
        return str(path)
