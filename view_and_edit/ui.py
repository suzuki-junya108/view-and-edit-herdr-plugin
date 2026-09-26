"""Styled text lines and a minimal full-screen renderer built on raw ANSI escapes."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from view_and_edit.width import printable, slice_columns, text_width


class Style:
    """SGR parameter strings; kept few so the UI stays calm."""

    PLAIN = ""
    BOLD = "1"
    DIM = "2"
    ITALIC = "3"
    UNDERLINE = "4"
    REVERSE = "7"
    TITLE = "1;7"
    HEADING = "1;36"
    HEADING2 = "1;34"
    ACCENT = "36"
    KEYWORD = "35"
    STRING = "32"
    COMMENT = "2;3"
    NUMBER = "33"
    TYPE = "34"
    ERROR = "31"
    CODE = "33"
    SELECTED = "7"
    DIRECTORY = "1;34"
    MATCH = "30;43"
    LINK = "4;36"
    BUTTON = "1;7;36"


@dataclass(frozen=True)
class Span:
    text: str
    style: str = Style.PLAIN


@dataclass
class Line:
    spans: list[Span] = field(default_factory=list)

    @classmethod
    def of(cls, text: str, style: str = Style.PLAIN) -> Line:
        return cls([Span(text, style)])

    @property
    def text(self) -> str:
        return "".join(span.text for span in self.spans)

    def add(self, text: str, style: str = Style.PLAIN) -> Line:
        if text:
            self.spans.append(Span(text, style))
        return self

    def width(self) -> int:
        return text_width(self.text)

    def styled(self, style: str) -> Line:
        """Overlay `style` on every span (e.g. to highlight a selected row)."""
        return Line([Span(s.text, ";".join(p for p in (s.style, style) if p)) for s in self.spans])

    def window(self, start: int, width: int) -> Line:
        """Columns [start, start+width) of the line, keeping span styles."""
        out = Line()
        col = 0
        for span in self.spans:
            text = printable(span.text)
            span_width = text_width(text)
            lo = max(start, col)
            hi = min(start + width, col + span_width)
            if hi > lo:
                out.add(slice_columns(text, lo - col, hi - lo), span.style)
            col += span_width
            if col >= start + width:
                break
        return out


def sgr(style: str) -> str:
    return f"\x1b[0;{style}m" if style else "\x1b[0m"


def render_line(line: Line, width: int, base_style: str = "") -> str:
    """ANSI string for `line` clipped/padded to exactly `width` columns."""
    out: list[str] = []
    used = 0
    for span in line.window(0, width).spans:
        style = ";".join(p for p in (base_style, span.style) if p)
        out.append(sgr(style) + span.text)
        used += text_width(span.text)
    if used < width:
        out.append(sgr(base_style) + " " * (width - used))
    out.append("\x1b[0m")
    return "".join(out)


def hint_bar(
    hints: Iterable[tuple[str, str]], width: int
) -> tuple[Line, list[tuple[int, int, str]]]:
    """Bottom help line such as `↑↓ 移動  Enter 開く  q 閉じる`.

    Also returns the column span `(start, end, key)` of each hint so clicks can press it.
    """
    line = Line()
    spans: list[tuple[int, int, str]] = []
    for key, label in hints:
        if line.width() + text_width(key) + text_width(label) + 3 > width:
            break
        start = line.width()
        line.add(f" {key}", Style.BOLD).add(f" {label} ", Style.DIM)
        spans.append((start, line.width(), key))
    return line, spans


@dataclass
class Frame:
    """What one screen wants shown; the terminal diffs frames line by line."""

    lines: list[str]
    cursor: tuple[int, int] | None = None  # (row, col) when a text cursor is shown
    images: list[ImagePlacement] = field(default_factory=list)


@dataclass(frozen=True)
class ImagePlacement:
    image_id: int
    png: bytes
    row: int
    col: int
    cols: int
    rows: int


def highlight_matches(line: Line, query: str, style: str = Style.MATCH) -> Line:
    """Overlay `style` on case-insensitive occurrences of `query`."""
    if not query:
        return line
    text = line.text
    lowered, needle = text.lower(), query.lower()
    if len(lowered) != len(text):  # rare case-mappings change length; skip highlighting
        return line
    ranges = []
    start = lowered.find(needle)
    while start >= 0:
        ranges.append((start, start + len(needle)))
        start = lowered.find(needle, start + len(needle))
    if not ranges:
        return line
    out = Line()
    pos = 0
    for span in line.spans:
        span_end = pos + len(span.text)
        cursor = pos
        for match_lo, match_hi in ranges:
            lo, hi = max(match_lo, pos), min(match_hi, span_end)
            if lo >= hi or lo < cursor:
                continue
            out.add(span.text[cursor - pos : lo - pos], span.style)
            out.add(span.text[lo - pos : hi - pos], style)
            cursor = hi
        out.add(span.text[cursor - pos :], span.style)
        pos = span_end
    return out
