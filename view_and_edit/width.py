"""Terminal display width for text, so wide (Japanese) characters line up."""

from __future__ import annotations

import unicodedata
from functools import lru_cache

TAB_WIDTH = 4
_ZERO_WIDTH = frozenset({"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"})


@lru_cache(maxsize=4096)
def char_width(ch: str) -> int:
    if ch in _ZERO_WIDTH or unicodedata.combining(ch):
        return 0
    code = ord(ch)
    if 0xFE00 <= code <= 0xFE0F or 0xE0100 <= code <= 0xE01EF:  # variation selectors
        return 0
    if code < 0x20 or 0x7F <= code < 0xA0:
        return 1  # rendered through `printable()` as a placeholder
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def printable(text: str) -> str:
    """Replace control characters so they cannot move the terminal cursor."""
    if text.isprintable():
        return text
    out = []
    for ch in text:
        if ch == "\t":
            out.append(" " * TAB_WIDTH)
        elif ord(ch) < 0x20 or 0x7F <= ord(ch) < 0xA0:
            out.append("·")
        else:
            out.append(ch)
    return "".join(out)


def text_width(text: str) -> int:
    return sum(char_width(ch) for ch in text)


def expand_tabs(text: str) -> str:
    return text.replace("\t", " " * TAB_WIDTH)


def slice_columns(text: str, start: int, width: int) -> str:
    """Return the part of `text` shown in display columns [start, start + width).

    A wide character cut in half by either edge is replaced by a space so the
    result never exceeds `width` columns.
    """
    out: list[str] = []
    col = 0
    used = 0
    for ch in text:
        w = char_width(ch)
        if col + w <= start:
            col += w
            continue
        if col < start:  # wide char straddling the left edge
            out.append(" " * (col + w - start))
            used += col + w - start
            col += w
            continue
        if used + w > width:
            if used < width:
                out.append(" " * (width - used))
                used = width
            break
        out.append(ch)
        used += w
        col += w
    return "".join(out)


def fit(text: str, width: int) -> str:
    """Clip or pad `text` to exactly `width` display columns."""
    clipped = slice_columns(text, 0, width)
    return clipped + " " * (width - text_width(clipped))


def truncate(text: str, width: int, ellipsis: str = "…") -> str:
    if text_width(text) <= width:
        return text
    if width <= 0:
        return ""
    return slice_columns(text, 0, width - text_width(ellipsis)).rstrip() + ellipsis


def truncate_left(text: str, width: int, ellipsis: str = "…") -> str:
    """Keep the end of `text` (useful for long paths)."""
    total = text_width(text)
    if total <= width:
        return text
    if width <= 0:
        return ""
    keep = width - text_width(ellipsis)
    return ellipsis + slice_columns(text, total - keep, keep)


def wrap(text: str, width: int) -> list[str]:
    """Hard-wrap by display width, preferring to break after spaces."""
    if width <= 0:
        return [text]
    lines: list[str] = []
    current: list[str] = []
    used = 0
    last_space = -1
    for ch in text:
        w = char_width(ch)
        if used + w > width and current:
            if ch == " ":
                lines.append("".join(current).rstrip())
                current, used, last_space = [], 0, -1
                continue
            if last_space > 0:
                head, tail = current[: last_space + 1], current[last_space + 1 :]
                lines.append("".join(head).rstrip())
                current = tail
            else:
                lines.append("".join(current))
                current = []
            used = text_width("".join(current))
            last_space = -1
        current.append(ch)
        used += w
        if ch == " ":
            last_space = len(current) - 1
    lines.append("".join(current))
    return lines


def column_to_index(text: str, column: int) -> int:
    """Index of the character displayed at `column` (clamped to len(text))."""
    col = 0
    for index, ch in enumerate(text):
        w = char_width(ch) if ch != "\t" else TAB_WIDTH
        if col + w > column:
            return index
        col += w
    return len(text)


def index_to_column(text: str, index: int) -> int:
    return sum(char_width(ch) if ch != "\t" else TAB_WIDTH for ch in text[:index])
