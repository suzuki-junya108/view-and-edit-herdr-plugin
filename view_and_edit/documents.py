"""A Document is a file prepared for display: one or more views (tabs) of it."""

from __future__ import annotations

import html
import html.parser
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from view_and_edit import markdown, structured
from view_and_edit.formats import Kind, detect, is_editable
from view_and_edit.gitinfo import diff_lines, git_root
from view_and_edit.media import (
    Cache,
    ImageRequest,
    PreviewError,
    document_text,
    media_info,
    pdf_page_count,
    preview_png,
)
from view_and_edit.structured import Page, human_size
from view_and_edit.syntax import Highlighter, language_for
from view_and_edit.textbuf import DecodeError, decode_text
from view_and_edit.ui import Line, Style
from view_and_edit.width import expand_tabs, wrap

# Beyond this, colouring every line up front would make opening noticeably slow.
HIGHLIGHT_LIMIT_BYTES = 2 * 1024 * 1024
# The browser's side preview stops here and asks the user to press Enter instead.
QUICK_PREVIEW_LIMIT_BYTES = 5 * 1024 * 1024
CHANGES_VIEW = "変更点"

PagesFn = Callable[[int], list[Page]]
ImageFn = Callable[[ImageRequest], Path]


@dataclass
class View:
    name: str
    pages: PagesFn | None = None  # width -> pages of lines
    image: ImageFn | None = None
    page_count: Callable[[], int] | None = None  # image pages (PDF)
    numbered: bool = False  # show line numbers (source code)


@dataclass
class Document:
    path: Path
    kind: Kind
    views: list[View]
    facts: list[tuple[str, str]] = field(default_factory=list)
    editable: bool = False
    playable: str | None = None  # "video" / "audio"
    error: str | None = None


def _cached(fn: PagesFn) -> PagesFn:
    """Memoise per width; most views are rebuilt only when the popup is resized."""
    cache: dict[int, list[Page]] = {}

    def wrapper(width: int) -> list[Page]:
        if width not in cache:
            cache.clear()
            cache[width] = fn(width)
        return cache[width]

    return wrapper


def _static(pages: list[Page]) -> PagesFn:
    return lambda _width: pages


def _message(text: str, style: str = Style.DIM) -> list[Page]:
    return [Page("", [Line.of(text, style)])]


def _safe(fn: PagesFn) -> PagesFn:
    def wrapper(width: int) -> list[Page]:
        try:
            return fn(width)
        except (OSError, ValueError, KeyError, PreviewError, DecodeError, RuntimeError) as error:
            return _message(f"表示できませんでした: {error}", Style.ERROR)

    return wrapper


def read_text(path: Path) -> str:
    text, _ = decode_text(path.read_bytes())
    return text


def source_lines(path: Path, text: str) -> list[Line]:
    if len(text) > HIGHLIGHT_LIMIT_BYTES:
        return [Line.of(line) for line in text.split("\n")]
    highlighter = Highlighter(language_for(path))
    return [highlighter.line(line) for line in text.split("\n")]


def _source_view(path: Path, name: str = "ソース") -> View:
    return View(
        name,
        _safe(_cached(lambda _w: [Page("", source_lines(path, read_text(path)))])),
        numbered=True,
    )


class _HtmlText(html.parser.HTMLParser):
    """Readable text of an HTML page: block elements become line breaks."""

    _BLOCKS = frozenset(
        [
            "p",
            "div",
            "br",
            "li",
            "tr",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "section",
            "article",
            "header",
            "footer",
            "ul",
            "ol",
            "table",
            "pre",
            "blockquote",
        ]
    )
    _SKIP = frozenset({"script", "style", "noscript", "template", "head"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCKS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("• ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_text(source: str) -> str:
    parser = _HtmlText()
    parser.feed(source)
    lines = [" ".join(line.split()) for line in html.unescape("".join(parser.parts)).split("\n")]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def _wrapped_text_pages(text: str) -> PagesFn:
    def build(width: int) -> list[Page]:
        lines = [
            Line.of(piece) for raw in text.split("\n") for piece in wrap(expand_tabs(raw), width)
        ]
        return [Page("", lines)]

    return _cached(build)


def file_facts(path: Path, kind: Kind) -> list[tuple[str, str]]:
    try:
        stat = path.stat()
    except OSError:
        return [("種類", kind.value)]
    facts = [("種類", kind.value)]
    if kind is not Kind.DIRECTORY:
        facts.append(("サイズ", human_size(stat.st_size)))
    facts.append(("更新", datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")))
    return facts


def open_document(path: Path, cache: Cache, kind: Kind | None = None) -> Document:  # noqa: PLR0911, PLR0912
    kind = kind or detect(path)
    facts = file_facts(path, kind)
    editable = is_editable(kind)

    detected: Kind = kind  # a plain Kind (not Optional) for the default below

    def image(k: Kind = detected) -> ImageFn:
        return lambda request: preview_png(path, k, cache, request)

    if kind is Kind.DIRECTORY:
        return Document(
            path, kind, [View("一覧", _safe(lambda _w: [Page("", directory_lines(path))]))], facts
        )
    if kind is Kind.MARKDOWN:
        return Document(
            path,
            kind,
            [
                View(
                    "表示",
                    _safe(_cached(lambda w: [Page("", markdown.render(read_text(path), w))])),
                ),
                _source_view(path),
            ],
            facts,
            editable,
        )
    if kind is Kind.HTML:
        return Document(
            path,
            kind,
            [
                View("画面", image=image()),
                View(
                    "テキスト", _safe(_wrapped_text_pages_from(lambda: html_text(read_text(path))))
                ),
                _source_view(path),
            ],
            facts,
            editable,
        )
    if kind in (Kind.IMAGE, Kind.FONT):
        return Document(path, kind, [View("画像", image=image())], facts)
    if kind is Kind.SVG:
        return Document(
            path, kind, [View("画像", image=image()), _source_view(path)], facts, editable
        )
    if kind is Kind.PDF:
        count = _memo(lambda: pdf_page_count(path))
        return Document(
            path,
            kind,
            [
                View("ページ", image=image(), page_count=count),
                View(
                    "テキスト", _safe(_wrapped_text_pages_from(lambda: document_text(path, kind)))
                ),
            ],
            facts,
        )
    if kind is Kind.DOCUMENT:
        return Document(
            path,
            kind,
            [
                View("プレビュー", image=image()),
                View(
                    "テキスト", _safe(_wrapped_text_pages_from(lambda: document_text(path, kind)))
                ),
            ],
            facts,
        )
    if kind is Kind.SPREADSHEET:
        views = [View("プレビュー", image=image())]
        if path.suffix.lower() in (".xlsx", ".xlsm"):
            views.insert(0, View("表", _safe(_cached(lambda _w: _xlsx_pages(path)))))
        return Document(path, kind, views, facts)
    if kind is Kind.PRESENTATION:
        views = [View("プレビュー", image=image())]
        if path.suffix.lower() == ".pptx":
            views.append(View("テキスト", _safe(_cached(lambda _w: _pptx_pages(path)))))
        return Document(path, kind, views, facts)
    if kind in (Kind.CSV, Kind.TSV):
        delimiter = "," if kind is Kind.CSV else "\t"
        return Document(
            path,
            kind,
            [
                View(
                    "表",
                    _safe(
                        _cached(
                            lambda _w: [
                                Page(
                                    "",
                                    structured.table_lines(
                                        structured.read_delimited(read_text(path), delimiter)
                                    ),
                                )
                            ]
                        )
                    ),
                ),
                _source_view(path),
            ],
            facts,
            editable,
        )
    if kind is Kind.JSON:
        return Document(
            path,
            kind,
            [
                View(
                    "整形",
                    _safe(_cached(lambda _w: [Page("", structured.json_lines(read_text(path)))])),
                ),
                _source_view(path),
            ],
            facts,
            editable,
        )
    if kind is Kind.JSONL:
        return Document(
            path,
            kind,
            [
                View(
                    "整形",
                    _safe(_cached(lambda _w: [Page("", structured.jsonl_lines(read_text(path)))])),
                ),
                _source_view(path),
            ],
            facts,
            editable,
        )
    if kind is Kind.NOTEBOOK:
        return Document(
            path,
            kind,
            [
                View(
                    "表示",
                    _safe(
                        _cached(lambda w: [Page("", structured.notebook_lines(read_text(path), w))])
                    ),
                ),
                _source_view(path),
            ],
            facts,
        )
    if kind is Kind.SQLITE:
        return Document(
            path,
            kind,
            [View("テーブル", _safe(_cached(lambda _w: structured.sqlite_pages(path))))],
            facts,
        )
    if kind is Kind.ARCHIVE:
        return Document(
            path,
            kind,
            [View("中身", _safe(_cached(lambda _w: [Page("", structured.archive_lines(path))])))],
            facts,
        )
    if kind is Kind.VIDEO:
        return Document(
            path, kind, [View("動画", image=image())], facts + media_facts(path), playable="video"
        )
    if kind is Kind.AUDIO:
        return Document(
            path,
            kind,
            [View("音声", _static(_message("Space で再生 / 一時停止")))],
            facts + media_facts(path),
            playable="audio",
        )
    if kind is Kind.BINARY:
        return _hex_document(path, facts)
    return _text_document(path, facts)


def with_changes_view(doc: Document) -> Document:
    """Add a tab with the file's git diff, for text files inside a git work tree.

    The diff itself is only taken when the tab is first shown.
    """
    if doc.editable and git_root(doc.path) is not None:
        path = doc.path
        doc.views.append(
            View(CHANGES_VIEW, _safe(_cached(lambda _w: [Page("", diff_lines(path))])))
        )
    return doc


def media_facts(path: Path) -> list[tuple[str, str]]:
    try:
        return media_info(path)
    except (OSError, ValueError):
        return []


def _memo(fn: Callable[[], int]) -> Callable[[], int]:
    value: list[int] = []

    def wrapper() -> int:
        if not value:
            value.append(fn())
        return value[0]

    return wrapper


def _wrapped_text_pages_from(get_text: Callable[[], str]) -> PagesFn:
    text: list[str] = []

    def build(width: int) -> list[Page]:
        if not text:
            text.append(get_text())
        return _wrapped_text_pages(text[0])(width)

    return build


def _xlsx_pages(path: Path) -> list[Page]:
    sheets = structured.read_xlsx(path)
    if not sheets:
        return _message("シートがありません")
    return [Page(name, structured.table_lines(rows)) for name, rows in sheets]


def _pptx_pages(path: Path) -> list[Page]:
    slides = structured.read_pptx_text(path)
    if not slides:
        return _message("スライドがありません")
    return [
        Page(title, [Line.of(p) for p in paragraphs] or [Line.of("（テキストなし）", Style.DIM)])
        for title, paragraphs in slides
    ]


def _text_document(path: Path, facts: list[tuple[str, str]]) -> Document:
    try:
        data = path.read_bytes()
        text, fmt = decode_text(data)
    except DecodeError:
        return _hex_document(path, [("種類", Kind.BINARY.value), *facts[1:]])
    except OSError as error:
        return Document(
            path,
            Kind.TEXT,
            [View("テキスト", _static(_message(str(error), Style.ERROR)))],
            facts,
            error=str(error),
        )
    facts = [
        *facts,
        ("文字コード", fmt.encoding + (" (BOM)" if fmt.bom else "")),
        ("改行", "CRLF" if fmt.newline == "\r\n" else "LF"),
    ]
    lines = source_lines(path, text)
    return Document(
        path,
        Kind.TEXT,
        [View("テキスト", _static([Page("", lines)]), numbered=True)],
        facts,
        editable=True,
    )


def _hex_document(path: Path, facts: list[tuple[str, str]]) -> Document:
    def build(_width: int) -> list[Page]:
        return [Page("", structured.hex_lines(path.read_bytes()))]

    return Document(path, Kind.BINARY, [View("16進", _safe(_cached(build)))], facts)


def directory_lines(path: Path, show_hidden: bool = False) -> list[Line]:
    try:
        entries = list_directory(path, show_hidden)
    except OSError as error:
        return [Line.of(str(error), Style.ERROR)]
    if not entries:
        return [Line.of("（空のフォルダ）", Style.DIM)]
    return [
        Line.of(e.name + "/", Style.DIRECTORY) if e.is_dir else Line.of(e.name) for e in entries
    ]


@dataclass(frozen=True)
class Entry:
    name: str
    path: Path
    is_dir: bool
    size: int
    mtime: float


def _natural_key(name: str) -> list[object]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


def list_directory(path: Path, show_hidden: bool = False) -> list[Entry]:
    entries = []
    with os.scandir(path) as scan:
        for item in scan:
            if not show_hidden and item.name.startswith("."):
                continue
            try:
                is_dir = item.is_dir()
                stat = item.stat()
                size, mtime = stat.st_size, stat.st_mtime
            except OSError:
                is_dir, size, mtime = False, 0, 0.0
            entries.append(Entry(item.name, Path(item.path), is_dir, size, mtime))
    entries.sort(key=lambda e: (not e.is_dir, _natural_key(e.name)))
    return entries


def too_large_for_quick_preview(path: Path, kind: Kind) -> bool:
    if kind in (
        Kind.IMAGE,
        Kind.VIDEO,
        Kind.AUDIO,
        Kind.PDF,
        Kind.DOCUMENT,
        Kind.PRESENTATION,
        Kind.FONT,
        Kind.DIRECTORY,
    ):
        return False  # previews for these never read the whole file into Python
    try:
        return path.stat().st_size > QUICK_PREVIEW_LIMIT_BYTES
    except OSError:
        return False


def page_lines(pages: Sequence[Page], index: int) -> Sequence[Line]:
    if not pages:
        return []
    return pages[max(0, min(index, len(pages) - 1))].lines
