"""Lightweight, dependency-free syntax colouring good enough for reading code."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from view_and_edit.ui import Line, Style


@dataclass(frozen=True)
class Language:
    keywords: frozenset[str]
    line_comment: tuple[str, ...] = ()
    block_comment: tuple[str, str] | None = None
    types: frozenset[str] = frozenset()
    case_insensitive: bool = False


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


_C_LIKE_KEYWORDS = """if else for while do switch case default break continue return goto
struct union enum typedef static const extern volatile sizeof void inline"""
_JS_KEYWORDS = """break case catch class const continue debugger default delete do else export
extends finally for function if import in instanceof let new return super switch this throw try
typeof var void while with yield async await of from as interface type implements enum
public private protected readonly static abstract declare namespace true false null undefined"""

LANGUAGES: dict[str, Language] = {
    "python": Language(
        _words(
            """False None True and as assert async await break class continue def del elif
            else except finally for from global if import in is lambda nonlocal not or pass
            raise return try while with yield match case self"""
        ),
        ("#",),
        types=_words("int str float bool list dict set tuple bytes object type"),
    ),
    "js": Language(_words(_JS_KEYWORDS), ("//",), ("/*", "*/")),
    "go": Language(
        _words(
            """break case chan const continue default defer else fallthrough for func go
            goto if import interface map package range return select struct switch type var
            nil true false"""
        ),
        ("//",),
        ("/*", "*/"),
        _words("int int64 int32 uint string bool byte rune error float64 any"),
    ),
    "rust": Language(
        _words(
            """as async await break const continue crate dyn else enum extern false fn for
            if impl in let loop match mod move mut pub ref return self Self static struct
            super trait true type unsafe use where while"""
        ),
        ("//",),
        ("/*", "*/"),
        _words("i32 i64 u8 u32 u64 usize isize f64 bool str String Vec Option Result Box"),
    ),
    "c": Language(
        _words(_C_LIKE_KEYWORDS + " class namespace template public private protected new"),
        ("//",),
        ("/*", "*/"),
        _words("int char long short float double unsigned signed bool size_t auto"),
    ),
    "java": Language(
        _words(
            """abstract class extends implements interface package import public private
            protected static final new return if else for while do switch case default break
            continue try catch finally throw throws this super null true false void var val
            fun object when override func let guard struct enum protocol extension"""
        ),
        ("//",),
        ("/*", "*/"),
    ),
    "shell": Language(
        _words(
            """if then else elif fi for while until do done case esac function in return
            export local readonly set unset source alias echo exit"""
        ),
        ("#",),
    ),
    "ruby": Language(
        _words(
            """def end class module if elsif else unless while until for in do return yield
            begin rescue ensure raise nil true false self require attr_accessor"""
        ),
        ("#",),
    ),
    "sql": Language(
        frozenset(
            w.lower()
            for w in [
                "SELECT",
                "FROM",
                "WHERE",
                "INSERT",
                "INTO",
                "VALUES",
                "UPDATE",
                "SET",
                "DELETE",
                "CREATE",
                "TABLE",
                "DROP",
                "ALTER",
                "INDEX",
                "JOIN",
                "LEFT",
                "RIGHT",
                "INNER",
                "OUTER",
                "ON",
                "AND",
                "OR",
                "NOT",
                "NULL",
                "AS",
                "ORDER",
                "BY",
                "GROUP",
                "HAVING",
                "LIMIT",
                "OFFSET",
                "PRIMARY",
                "KEY",
                "FOREIGN",
                "REFERENCES",
                "DISTINCT",
                "UNION",
            ]
        ),
        ("--",),
        ("/*", "*/"),
        case_insensitive=True,
    ),
    "css": Language(_words("important media import from to"), (), ("/*", "*/")),
    "config": Language(_words("true false null yes no on off"), ("#",)),
    "lua": Language(
        _words(
            """and break do else elseif end false for function if in local nil not or repeat
            return then true until while"""
        ),
        ("--",),
    ),
}

_EXTENSIONS = {
    "python": "py pyi pyw",
    "js": "js mjs cjs jsx ts tsx mts cts vue svelte",
    "go": "go",
    "rust": "rs",
    "c": "c h cc cpp cxx hpp hh m mm cs",
    "java": "java kt kts scala swift dart groovy",
    "shell": "sh bash zsh fish ksh",
    "ruby": "rb rake gemspec",
    "sql": "sql",
    "css": "css scss sass less",
    "config": "toml yaml yml ini cfg conf env properties",
    "lua": "lua",
}
_BY_EXTENSION = {ext: lang for lang, exts in _EXTENSIONS.items() for ext in exts.split()}
_BY_NAME = {
    "makefile": "shell",
    "dockerfile": "shell",
    ".zshrc": "shell",
    ".bashrc": "shell",
    ".gitignore": "config",
    "gemfile": "ruby",
}

_TOKEN = re.compile(
    r"""(?P<string>"(?:[^"\\]|\\.)*"?|'(?:[^'\\]|\\.)*'?|`(?:[^`\\]|\\.)*`?)"""
    r"""|(?P<number>\b(?:0x[0-9a-fA-F_]+|\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?)\b)"""
    r"""|(?P<word>[A-Za-z_][A-Za-z0-9_]*)"""
)


def language_for(path: Path) -> str | None:
    name = path.name.lower()
    if name in _BY_NAME:
        return _BY_NAME[name]
    return _BY_EXTENSION.get(path.suffix.lower().lstrip("."))


class Highlighter:
    """Colours lines in order, remembering whether a block comment is still open."""

    def __init__(self, language: str | None) -> None:
        self.language = LANGUAGES.get(language) if language else None
        self._in_block = False

    def reset(self) -> None:
        self._in_block = False

    def line(self, text: str) -> Line:
        lang = self.language
        if lang is None:
            return Line.of(text)
        out = Line()
        pos = 0
        if self._in_block and lang.block_comment:
            end = text.find(lang.block_comment[1])
            if end < 0:
                return Line.of(text, Style.COMMENT)
            pos = end + len(lang.block_comment[1])
            out.add(text[:pos], Style.COMMENT)
            self._in_block = False
        self._code(text, pos, lang, out)
        return out

    def _code(self, text: str, pos: int, lang: Language, out: Line) -> None:
        while pos < len(text):
            comment_at = self._comment_start(text, pos, lang)
            segment_end = comment_at[0] if comment_at else len(text)
            self._tokens(text[pos:segment_end], lang, out)
            if not comment_at:
                return
            start, kind = comment_at
            if kind == "line":
                out.add(text[start:], Style.COMMENT)
                return
            opener, closer = lang.block_comment or ("", "")
            end = text.find(closer, start + len(opener))
            if end < 0:
                out.add(text[start:], Style.COMMENT)
                self._in_block = True
                return
            end += len(closer)
            out.add(text[start:end], Style.COMMENT)
            pos = end

    @staticmethod
    def _comment_start(text: str, pos: int, lang: Language) -> tuple[int, str] | None:
        """Earliest comment opener outside of string literals."""
        quote: str | None = None
        index = pos
        while index < len(text):
            ch = text[index]
            if quote:
                if ch == "\\":
                    index += 2
                    continue
                if ch == quote:
                    quote = None
            elif ch in "\"'`":
                quote = ch
            else:
                for marker in lang.line_comment:
                    if text.startswith(marker, index):
                        return index, "line"
                if lang.block_comment and text.startswith(lang.block_comment[0], index):
                    return index, "block"
            index += 1
        return None

    @staticmethod
    def _tokens(segment: str, lang: Language, out: Line) -> None:
        last = 0
        for match in _TOKEN.finditer(segment):
            out.add(segment[last : match.start()])
            token = match.group()
            if match.lastgroup == "string":
                out.add(token, Style.STRING)
            elif match.lastgroup == "number":
                out.add(token, Style.NUMBER)
            elif (token.lower() if lang.case_insensitive else token) in lang.keywords:
                out.add(token, Style.KEYWORD)
            elif token in lang.types:
                out.add(token, Style.TYPE)
            else:
                out.add(token)
            last = match.end()
        out.add(segment[last:])
