"""List the files mentioned on a pane's screen, newest (lowest) first."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from view_and_edit.locate import Locator
from view_and_edit.target import Target, resolve_mention

# Split on whitespace, box-drawing characters, and the prompt and bullet glyphs agents
# draw in front of lines (Claude Code's among them). `=` splits `--out=path`;
# `](` splits Markdown links so both the label and the target are tried.
_SEPARATORS = re.compile(r"[\s─-╿⎿⏺❯✻•·=]+|\]\(")
# A token is worth resolving only if it looks like a path: a slash, or a file
# extension, or a well-known extensionless file name.
_EXTENSION = re.compile(r"\.[A-Za-z0-9_+-]{1,12}$")
_LINE_SUFFIX = re.compile(r"(?::\d+){1,2}:?$|\(\d+(?:,\s*\d+)?\)$")
_BARE_NAMES = frozenset(
    {
        "Makefile",
        "Dockerfile",
        "Gemfile",
        "Rakefile",
        "Procfile",
        "Justfile",
        "README",
        "LICENSE",
        "CHANGELOG",
    }
)
_MIN_TOKEN_LENGTH = 2
# Agents show tool calls as `Read(src/app.py)` or `Edit(src/app.py)`.
_TOOL_CALL = re.compile(r"^[A-Z][A-Za-z]*\((?P<argument>[^()]+)\)$")


@dataclass(frozen=True)
class _Mention:
    text: str
    row: int
    # A path the terminal or the agent wrapped onto the next line, joined back together.
    joined: bool = False


def _tokens(line: str) -> list[str]:
    tokens = []
    for piece in _SEPARATORS.split(line):
        call = _TOOL_CALL.match(piece)
        token = call["argument"] if call else piece
        if token:
            tokens.append(token)
    return tokens


def looks_like_path(token: str) -> bool:
    text = _LINE_SUFFIX.sub("", token.strip("\"'`<>()[]{},;"))
    if len(text) < _MIN_TOKEN_LENGTH or "://" in text or text.startswith("-"):
        return False
    if re.fullmatch(r"[\d.:,]+", text):  # versions, times, and numbers
        return False
    if not re.search(r"[^/~.]", text):  # `//`, `~/`, `../` name no particular file
        return False
    return "/" in text or bool(_EXTENSION.search(text)) or text in _BARE_NAMES


def _mentions(lines: list[str]) -> list[_Mention]:
    """Path-like tokens bottom-up, plus joins of a line's last token with the next line's
    first, because a long path wrapped by the terminal or the agent is split in two."""
    tokens = [_tokens(line) for line in lines]
    found: list[_Mention] = []
    for row in range(len(lines) - 1, -1, -1):
        if row + 1 < len(lines) and tokens[row] and tokens[row + 1]:
            joined = tokens[row][-1] + tokens[row + 1][0]
            if looks_like_path(joined):
                found.append(_Mention(joined, row, joined=True))
        found.extend(
            _Mention(token, row) for token in reversed(tokens[row]) if looks_like_path(token)
        )
    return found


def files_on_screen(text: str, cwd: Path, locator: Locator | None = None) -> list[Target]:
    """Existing files and folders the screen mentions, lowest on the screen first.

    A wrapped path joined back together replaces its two halves, so the halves do not
    turn into unrelated suffix matches.
    """
    lines = text.splitlines()
    mentions = _mentions(lines)
    joined_rows = set()
    resolved: list[tuple[_Mention, list[Target]]] = []
    for mention in mentions:
        if mention.joined:
            targets = resolve_mention(mention.text, cwd, locator)
            if targets:
                joined_rows.add(mention.row)
                resolved.append((mention, targets))
    results: dict[Path, Target] = {}
    for mention in mentions:
        if mention.joined:
            continue
        if _is_half_of_join(mention, joined_rows, lines):
            continue
        resolved.append((mention, resolve_mention(mention.text, cwd, locator)))
    resolved.sort(key=lambda item: -item[0].row)
    for _mention, targets in resolved:
        for target in targets:
            results.setdefault(target.path, target)
    return list(results.values())


def _is_half_of_join(mention: _Mention, joined_rows: set[int], lines: list[str]) -> bool:
    """Whether `mention` is the last token of a joined row or the first of the row after."""
    if mention.row in joined_rows:
        last = _tokens(lines[mention.row])
        if last and last[-1] == mention.text:
            return True
    if mention.row - 1 in joined_rows:
        first = _tokens(lines[mention.row])
        if first and first[0] == mention.text:
            return True
    return False
