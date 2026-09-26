"""Render Markdown as readable terminal lines (headings, lists, code, tables, quotes)."""

from __future__ import annotations

import re

from view_and_edit.syntax import Highlighter
from view_and_edit.ui import Line, Style
from view_and_edit.width import text_width, wrap

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)\s*([\w+-]*)")
_BULLET = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(\[[ xX]\]\s+)?(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_INLINE = re.compile(
    r"(?P<code>`[^`]+`)"
    r"|(?P<image>!\[[^\]]*\]\([^)]*\))"
    r"|(?P<link>\[[^\]]+\]\([^)]*\))"
    r"|(?P<bold>\*\*[^*]+\*\*|__[^_]+__)"
    r"|(?P<italic>\*[^*\s][^*]*\*|_[^_\s][^_]*_)"
)
_FENCE_LANGUAGES = {
    "py": "python",
    "python": "python",
    "js": "js",
    "javascript": "js",
    "ts": "js",
    "typescript": "js",
    "tsx": "js",
    "jsx": "js",
    "sh": "shell",
    "bash": "shell",
    "zsh": "shell",
    "shell": "shell",
    "console": "shell",
    "go": "go",
    "rust": "rust",
    "rs": "rust",
    "c": "c",
    "cpp": "c",
    "java": "java",
    "kotlin": "java",
    "swift": "java",
    "sql": "sql",
    "toml": "config",
    "yaml": "config",
    "yml": "config",
    "json": "config",
    "ruby": "ruby",
    "css": "css",
    "lua": "lua",
}

_H1_H2 = 2


def inline(text: str, base: str = Style.PLAIN) -> Line:
    line = Line()
    last = 0
    for match in _INLINE.finditer(text):
        line.add(text[last : match.start()], base)
        token = match.group()
        kind = match.lastgroup
        if kind == "code":
            line.add(token[1:-1], Style.CODE)
        elif kind == "image":
            alt = token[2 : token.index("]")]
            line.add(f"[画像: {alt}]" if alt else "[画像]", Style.ACCENT)
        elif kind == "link":
            line.add(token[1 : token.index("]")], Style.LINK)
        elif kind == "bold":
            line.add(token[2:-2], ";".join(p for p in (base, Style.BOLD) if p))
        else:
            line.add(token[1:-1], ";".join(p for p in (base, Style.ITALIC) if p))
        last = match.end()
    line.add(text[last:], base)
    return line


def _wrap_line(line: Line, width: int, first_prefix: Line, next_prefix: Line) -> list[Line]:
    """Wrap a styled line, keeping styles per span."""
    available = max(10, width - first_prefix.width())
    words: list[tuple[str, str]] = []
    for span in line.spans:
        words.extend((piece, span.style) for piece in re.split(r"(?<= )", span.text) if piece)
    out: list[Line] = []
    current = Line(list(first_prefix.spans))
    used = 0
    for word, style in words:
        piece = word
        w = text_width(piece)
        if used + w > available and used > 0:
            out.append(current)
            current = Line(list(next_prefix.spans))
            used = 0
            piece = piece.lstrip(" ")
            w = text_width(piece)
        if w > available:
            chunks = wrap(piece, available)
            for chunk in chunks[:-1]:
                current.add(chunk, style)
                out.append(current)
                current = Line(list(next_prefix.spans))
            piece = chunks[-1]
            used = 0
            w = text_width(piece)
        current.add(piece, style)
        used += w
    out.append(current)
    return out


def _table(rows: list[str], width: int) -> list[Line]:
    cells = [[c.strip() for c in row.strip().strip("|").split("|")] for row in rows]
    count = max(len(r) for r in cells)
    for r in cells:
        r.extend([""] * (count - len(r)))
    widths = [max(text_width(r[i]) for r in cells) for i in range(count)]
    out: list[Line] = []
    for index, r in enumerate(cells):
        line = Line()
        for i, cell in enumerate(r):
            pad = " " * (widths[i] - text_width(cell))
            line.add("│ " if i else "  ", Style.DIM)
            line.spans.extend(inline(cell, Style.BOLD if index == 0 else Style.PLAIN).spans)
            line.add(pad + " ")
        out.append(line)
        if index == 0:
            out.append(Line.of("  " + "┼".join("─" * (w + 2) for w in widths)[1:], Style.DIM))
    return [line.window(0, width) for line in out]


def render(text: str, width: int) -> list[Line]:
    lines = text.split("\n")
    out: list[Line] = []
    index = 0
    while index < len(lines):
        raw = lines[index]
        fence = _FENCE.match(raw)
        if fence:
            marker = fence[1]
            highlighter = Highlighter(_FENCE_LANGUAGES.get(fence[2].lower()))
            if fence[2]:
                out.append(Line.of(f"  {fence[2]}", Style.DIM))
            index += 1
            while index < len(lines) and not lines[index].strip().startswith(marker):
                out.append(
                    Line([*Line.of("  │ ", Style.DIM).spans, *highlighter.line(lines[index]).spans])
                )
                index += 1
            index += 1
            continue
        if "|" in raw and index + 1 < len(lines) and _TABLE_SEPARATOR.match(lines[index + 1]):
            rows = [raw]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(lines[index])
                index += 1
            out.extend(_table(rows, width))
            continue
        out.extend(_block(raw, width))
        index += 1
    return out


def _block(raw: str, width: int) -> list[Line]:
    heading = _HEADING.match(raw)
    if heading:
        level = len(heading[1])
        style = Style.HEADING if level <= _H1_H2 else Style.HEADING2
        rendered = _wrap_line(inline(heading[2], style), width, Line(), Line())
        if level <= _H1_H2:
            rendered.append(
                Line.of(
                    ("━" if level == 1 else "─") * min(width, max(4, rendered[0].width())),
                    Style.DIM,
                )
            )
        return rendered
    if _RULE.match(raw):
        return [Line.of("─" * width, Style.DIM)]
    quote = _QUOTE.match(raw)
    if quote:
        return _wrap_line(
            inline(quote[1], Style.ITALIC),
            width,
            Line.of("┃ ", Style.ACCENT),
            Line.of("┃ ", Style.ACCENT),
        )
    bullet = _BULLET.match(raw)
    if bullet:
        indent = " " * (len(bullet[1].expandtabs(4)) + 2)
        marker = bullet[2]
        if bullet[3]:
            symbol = "☑ " if "x" in bullet[3].lower() else "☐ "
        elif marker[0].isdigit():
            symbol = marker + " "
        else:
            symbol = "• "
        return _wrap_line(
            inline(bullet[4]),
            width,
            Line.of(indent + symbol, Style.ACCENT),
            Line.of(indent + " " * text_width(symbol)),
        )
    if not raw.strip():
        return [Line()]
    return _wrap_line(inline(raw.strip()), width, Line.of("  "), Line.of("  "))
