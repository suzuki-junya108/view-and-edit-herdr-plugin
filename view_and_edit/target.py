"""Turn clicked URLs and selected terminal text into an existing local path."""

from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

# `src/a.ts:12`, `src/a.ts:12:5`, compiler output often adds a trailing colon.
_LINE_COL_SUFFIX = re.compile(r"^(?P<path>.+?):(?P<line>\d+)(?::(?P<col>\d+))?:?$")
# `src/a.py(12)` / `src/a.cs(12,5)` style used by Python tracebacks-ish tools and MSBuild.
_PAREN_SUFFIX = re.compile(r"^(?P<path>.+?)\((?P<line>\d+)(?:,\s*(?P<col>\d+))?\)$")
# `#L12` / `#L12C5` fragments used by GitHub-style file links.
_FRAGMENT_LINE = re.compile(r"^L?(?P<line>\d+)(?:C(?P<col>\d+))?$")

# Characters that commonly wrap a path in prose or markdown but are never part of it.
_WRAPPERS = "\"'`<>()[]{}"
_TRAILING_PUNCTUATION = ".,;:!?"
# `git diff` headers prefix paths with `a/` and `b/`.
_DIFF_PREFIXES = ("a/", "b/")


class TargetError(Exception):
    """The input cannot be turned into a local path the user can open."""


@dataclass(frozen=True)
class Target:
    path: Path
    line: int | None = None
    column: int | None = None

    @property
    def is_dir(self) -> bool:
        return self.path.is_dir()


@dataclass(frozen=True)
class _Candidate:
    text: str
    line: int | None = None
    column: int | None = None


def _local_hostnames() -> set[str]:
    names = {"", "localhost", "127.0.0.1", "::1"}
    host = socket.gethostname()
    names.add(host.lower())
    names.add(host.split(".", 1)[0].lower())
    return names


def _optional_int(value: str | None) -> int | None:
    return int(value) if value else None


def parse_file_url(url: str, local_hostnames: set[str] | None = None) -> _Candidate:
    """Decode a `file://` URL, rejecting links that point at another machine."""
    parts = urlsplit(url)
    if parts.scheme != "file":
        raise TargetError(f"file:// 以外のリンクには対応していません: {url}")
    hosts = _local_hostnames() if local_hostnames is None else local_hostnames
    if (parts.hostname or "").lower() not in hosts:
        raise TargetError(f"別ホストのファイルは開けません: {parts.hostname}")
    path = unquote(parts.path)
    if not path:
        raise TargetError(f"パスが空のリンクです: {url}")
    fragment = _FRAGMENT_LINE.match(parts.fragment)
    if fragment:
        return _Candidate(path, _optional_int(fragment["line"]), _optional_int(fragment["col"]))
    return _Candidate(path)


def _strip_decorations(text: str) -> str:
    previous = None
    while previous != text:
        previous = text
        text = text.strip().rstrip(_TRAILING_PUNCTUATION)
        if len(text) >= 2 and text[0] in _WRAPPERS and text[-1] in _WRAPPERS:
            text = text[1:-1]
        elif text[:1] in _WRAPPERS and text[:1] not in "(":
            text = text[1:]
        elif text[-1:] in _WRAPPERS and text[-1:] not in ")":
            text = text[:-1]
    return text


def _text_candidates(text: str) -> list[_Candidate]:
    """Ordered guesses for a selected token: exact text first, then line-number forms."""
    cleaned = _strip_decorations(text)
    candidates: list[_Candidate] = []
    for variant in dict.fromkeys((text.strip(), cleaned)):
        if not variant:
            continue
        candidates.append(_Candidate(variant))
        for pattern in (_LINE_COL_SUFFIX, _PAREN_SUFFIX):
            match = pattern.match(variant)
            if match:
                candidates.append(
                    _Candidate(
                        match["path"],
                        _optional_int(match["line"]),
                        _optional_int(match["col"]),
                    )
                )
    return candidates


def _expand(candidate_text: str, cwd: Path) -> list[Path]:
    expanded = Path(os.path.expanduser(candidate_text))
    if expanded.is_absolute():
        return [expanded]
    paths = [cwd / expanded]
    if candidate_text.startswith(_DIFF_PREFIXES):
        paths.append(cwd / candidate_text[2:])
    return paths


def _first_existing(candidates: list[_Candidate], cwd: Path) -> Target | None:
    for candidate in candidates:
        for path in _expand(candidate.text, cwd):
            if path.exists():
                return Target(path.resolve(), candidate.line, candidate.column)
    return None


def resolve_selection(text: str, cwd: Path) -> Target:
    """Resolve selected terminal text to an existing path.

    The whole selection is tried first so paths containing spaces work; after that
    each whitespace-separated token is tried in order, so selecting a line such as
    `modified: src/app.ts` still finds the file.
    """
    stripped = text.strip()
    if not stripped:
        raise TargetError("パスが選択されていません")
    if stripped.startswith("file://"):
        return resolve_file_url(stripped, cwd)

    lines = [line for line in stripped.splitlines() if line.strip()]
    groups = [_text_candidates(lines[0])] if len(lines) == 1 else []
    for token in (lines[0] if lines else stripped).split():
        groups.append(_text_candidates(token))
    for group in groups:
        found = _first_existing(group, cwd)
        if found:
            return found
    raise TargetError(f"ファイルが見つかりません: {_shorten(stripped)}（基準: {cwd}）")


def resolve_file_url(url: str, cwd: Path, local_hostnames: set[str] | None = None) -> Target:
    candidate = parse_file_url(url, local_hostnames)
    found = _first_existing([candidate], cwd)
    if found:
        return found
    raise TargetError(f"ファイルが見つかりません: {candidate.text}")


_MAX_MESSAGE_CHARS = 80


def _shorten(text: str) -> str:
    one_line = " ".join(text.split())
    if len(one_line) <= _MAX_MESSAGE_CHARS:
        return one_line
    return one_line[: _MAX_MESSAGE_CHARS - 1] + "…"
