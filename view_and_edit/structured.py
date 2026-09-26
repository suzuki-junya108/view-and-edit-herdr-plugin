"""Turn structured files (tables, JSON, notebooks, archives, databases) into lines."""

from __future__ import annotations

import csv
import io
import json
import pyexpat
import re
import sqlite3
import tarfile
import zipfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import overload
from xml.etree import ElementTree

from view_and_edit import markdown
from view_and_edit.syntax import Highlighter
from view_and_edit.ui import Line, Style
from view_and_edit.width import text_width, truncate

MAX_CELL_WIDTH = 40
SQLITE_CELL_WIDTH = 24
HEX_BYTES_PER_ROW = 16
_SQLITE_FETCH = 200


@dataclass
class Page:
    """One tab of multi-part content: a sheet, a table, a slide."""

    title: str
    lines: Sequence[Line]


class LazyLines(Sequence[Line]):
    """Lines produced on demand so huge files scroll without rendering everything."""

    def __init__(self, count: int, make: Callable[[int], Line]) -> None:
        self._count = count
        self._make = make

    def __len__(self) -> int:
        return self._count

    @overload
    def __getitem__(self, index: int) -> Line: ...
    @overload
    def __getitem__(self, index: slice) -> Sequence[Line]: ...
    def __getitem__(self, index: int | slice) -> Line | Sequence[Line]:
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(self._count))]
        if index < 0:
            index += self._count
        if not 0 <= index < self._count:
            raise IndexError(index)
        return self._make(index)

    def __iter__(self) -> Iterator[Line]:
        return (self[i] for i in range(self._count))


# ---- tables -------------------------------------------------------------------
def table_lines(rows: list[list[str]], max_cell: int = MAX_CELL_WIDTH) -> list[Line]:
    if not rows:
        return [Line.of("（空の表）", Style.DIM)]
    count = max(len(r) for r in rows)
    widths = [0] * count
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], min(max_cell, text_width(_one_line(cell))))
    out = []
    number_width = len(str(len(rows)))
    for index, row in enumerate(rows):
        line = Line.of(f"{index + 1:>{number_width}} ", Style.DIM)
        for i in range(count):
            cell = _one_line(row[i]) if i < len(row) else ""
            cell = truncate(cell, widths[i])
            line.add("│" if i else "", Style.DIM)
            line.add(
                " " + cell + " " * (widths[i] - text_width(cell)) + " ",
                Style.BOLD if index == 0 else "",
            )
        out.append(line)
        if index == 0:
            out.append(
                Line.of(
                    " " * number_width + " " + "┼".join("─" * (w + 2) for w in widths), Style.DIM
                )
            )
    return out


def _one_line(cell: str) -> str:
    return cell.replace("\r\n", " ⏎ ").replace("\n", " ⏎ ")


def read_delimited(text: str, delimiter: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text), delimiter=delimiter))


# The stdlib parser never fetches external entities (Python >= 3.7.1). Entity-expansion
# ("billion laughs") bombs need a DTD, which Office XML never has, so any DTD is refused
# before ElementTree sees the data. This holds even on the old expat in macOS's Python 3.9.
class UnsafeXmlError(ValueError):
    pass


def _refuse_dtd(*_args: object) -> None:
    raise UnsafeXmlError("DTD を含む XML は安全のため解析しません")


def _check_no_dtd(data: bytes) -> None:
    checker = pyexpat.ParserCreate()
    checker.StartDoctypeDeclHandler = _refuse_dtd
    checker.EntityDeclHandler = _refuse_dtd
    checker.Parse(data, True)


# Refuse to inflate archive members beyond this, so a zip bomb cannot exhaust memory.
MAX_MEMBER_BYTES = 256 * 1024 * 1024


def read_member(zf: zipfile.ZipFile, name: str) -> bytes:
    if zf.getinfo(name).file_size > MAX_MEMBER_BYTES:
        raise ValueError(f"{name} が大きすぎます")
    return zf.read(name)


def parse_xml(data: bytes) -> ElementTree.Element:
    _check_no_dtd(data)
    return ElementTree.fromstring(data)  # noqa: S314 - DTDs are refused by _check_no_dtd


_XLSX_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_CELL_REF = re.compile(r"([A-Z]+)(\d+)")


def _column_index(letters: str) -> int:
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - ord("A") + 1)
    return index - 1


def read_xlsx(path: Path) -> list[tuple[str, list[list[str]]]]:
    """Sheets as (name, rows) using only the OOXML parts every .xlsx contains."""
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = parse_xml(read_member(zf, "xl/sharedStrings.xml"))
            for si in root.findall("m:si", _XLSX_NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{_XLSX_NS['m']}}}t")))
        workbook = parse_xml(read_member(zf, "xl/workbook.xml"))
        rels = parse_xml(read_member(zf, "xl/_rels/workbook.xml.rels"))
        targets = {rel.get("Id"): rel.get("Target", "") for rel in rels}
        sheets = []
        for sheet in workbook.iterfind("m:sheets/m:sheet", _XLSX_NS):
            target = targets.get(sheet.get(_REL_NS), "").lstrip("/")
            part = target if target.startswith("xl/") else f"xl/{target}"
            if part not in names:
                continue
            sheets.append((sheet.get("name", "Sheet"), _xlsx_rows(read_member(zf, part), shared)))
        return sheets


def _xlsx_rows(data: bytes, shared: list[str]) -> list[list[str]]:
    root = parse_xml(data)
    grid: dict[int, dict[int, str]] = {}
    for cell in root.iter(f"{{{_XLSX_NS['m']}}}c"):
        ref = _CELL_REF.match(cell.get("r", ""))
        if not ref:
            continue
        col, row = _column_index(ref[1]), int(ref[2]) - 1
        kind = cell.get("t")
        value = cell.find("m:v", _XLSX_NS)
        if kind == "s" and value is not None and value.text is not None:
            text = shared[int(value.text)] if int(value.text) < len(shared) else ""
        elif kind == "inlineStr":
            text = "".join(t.text or "" for t in cell.iter(f"{{{_XLSX_NS['m']}}}t"))
        elif kind == "b" and value is not None:
            text = "TRUE" if value.text == "1" else "FALSE"
        else:
            text = value.text or "" if value is not None else ""
        grid.setdefault(row, {})[col] = text
    if not grid:
        return []
    width = max(max(cols) for cols in grid.values()) + 1
    return [[grid.get(r, {}).get(c, "") for c in range(width)] for r in range(max(grid) + 1)]


_PPTX_TEXT = "{http://schemas.openxmlformats.org/drawingml/2006/main}t"
_SLIDE_NUMBER = re.compile(r"ppt/slides/slide(\d+)\.xml$")


def read_pptx_text(path: Path) -> list[tuple[str, list[str]]]:
    with zipfile.ZipFile(path) as zf:
        slides = sorted(
            (int(m[1]), name) for name in zf.namelist() if (m := _SLIDE_NUMBER.match(name))
        )
        out = []
        for number, name in slides:
            root = parse_xml(read_member(zf, name))
            paragraphs = []
            for para in root.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}p"):
                text = "".join(t.text or "" for t in para.iter(_PPTX_TEXT)).strip()
                if text:
                    paragraphs.append(text)
            out.append((f"スライド {number}", paragraphs))
        return out


_DOCX_PARA = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
_DOCX_TEXT = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"


def read_docx_text(path: Path) -> str:
    """Fallback when macOS `textutil` is unavailable."""
    with zipfile.ZipFile(path) as zf:
        root = parse_xml(read_member(zf, "word/document.xml"))
    return "\n".join(
        "".join(t.text or "" for t in p.iter(_DOCX_TEXT)) for p in root.iter(_DOCX_PARA)
    )


# ---- JSON / notebooks ---------------------------------------------------------
def json_lines(text: str) -> list[Line]:
    data = json.loads(text)
    pretty = json.dumps(data, ensure_ascii=False, indent=2)
    return highlight_lines(pretty.split("\n"), "config")


def jsonl_lines(text: str) -> list[Line]:
    out: list[Line] = []
    highlighter = Highlighter("config")
    for number, raw in enumerate(text.split("\n"), 1):
        if not raw.strip():
            continue
        out.append(Line.of(f"── {number} 行目", Style.DIM))
        try:
            pretty = json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
        except json.JSONDecodeError as error:
            out.append(Line.of(f"JSON エラー: {error}", Style.ERROR))
            out.append(Line.of(raw))
            continue
        out.extend(highlighter.line(line) for line in pretty.split("\n"))
    return out


def highlight_lines(lines: list[str], language: str | None) -> list[Line]:
    highlighter = Highlighter(language)
    return [highlighter.line(line) for line in lines]


def notebook_lines(text: str, width: int) -> list[Line]:
    data = json.loads(text)
    language = (
        data.get("metadata", {}).get("language_info", {}).get("name")
        or data.get("metadata", {}).get("kernelspec", {}).get("language")
        or "python"
    )
    out: list[Line] = []
    for cell in data.get("cells", []):
        source = cell.get("source", "")
        source = "".join(source) if isinstance(source, list) else str(source)
        if cell.get("cell_type") == "markdown":
            out.extend(markdown.render(source, width))
        else:
            count = cell.get("execution_count")
            out.append(Line.of(f"In [{count if count is not None else ' '}]:", Style.ACCENT))
            out.extend(
                Line([*Line.of("  ").spans, *code.spans])
                for code in highlight_lines(source.split("\n"), language)
            )
            for output in cell.get("outputs", []):
                out.extend(_notebook_output(output))
        out.append(Line())
    return out


def _notebook_output(output: dict[str, object]) -> list[Line]:
    text: object = output.get("text")
    data = output.get("data")
    if text is None and isinstance(data, dict):
        if "text/plain" in data:
            text = data["text/plain"]
        elif any(k.startswith("image/") for k in data):
            return [Line.of("  [画像出力]", Style.ACCENT)]
    if output.get("output_type") == "error":
        name, value = output.get("ename", "Error"), output.get("evalue", "")
        return [Line.of(f"  {name}: {value}", Style.ERROR)]
    if text is None:
        return []
    body = "".join(str(t) for t in text) if isinstance(text, list) else str(text)
    return [Line.of("  " + line, Style.DIM) for line in body.rstrip("\n").split("\n")]


# ---- archives -----------------------------------------------------------------
def _size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


human_size = _size


def archive_lines(path: Path) -> list[Line]:
    entries: list[tuple[str, int, str]] = []
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                stamp = datetime(*info.date_time).strftime("%Y-%m-%d %H:%M")
                entries.append((info.filename, info.file_size, stamp))
    elif tarfile.is_tarfile(path):
        with tarfile.open(path) as tf:
            for member in tf.getmembers():
                name = member.name + ("/" if member.isdir() else "")
                stamp = datetime.fromtimestamp(member.mtime).strftime("%Y-%m-%d %H:%M")
                entries.append((name, member.size, stamp))
    else:
        return [Line.of("このアーカイブ形式の一覧表示には対応していません", Style.DIM)]
    total = sum(size for _, size, _ in entries)
    out = [Line.of(f"{len(entries)} 項目 / 展開後 {_size(total)}", Style.BOLD), Line()]
    for name, size, stamp in entries:
        style = Style.DIRECTORY if name.endswith("/") else ""
        out.append(
            Line()
            .add(f"{stamp}  ", Style.DIM)
            .add(f"{_size(size):>9}  ", Style.DIM)
            .add(name, style)
        )
    return out


# ---- SQLite -------------------------------------------------------------------
def sqlite_pages(path: Path) -> list[Page]:
    """One page per table; rows are fetched only for the part being viewed."""
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    names = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    if not names:
        return [Page("（テーブルなし）", [Line.of("テーブルがありません", Style.DIM)])]
    return [_sqlite_page(conn, name) for name in names]


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _select_from(
    conn: sqlite3.Connection, table: str, what: str, tail: str = "", params: tuple[int, ...] = ()
) -> list[tuple[object, ...]]:
    """Run `SELECT what FROM table tail` with `what`/`tail` fixed by the callers.

    SQLite cannot bind a table name as a parameter, so it is spliced in as a quoted
    identifier. The name comes from the file's own schema and the connection is
    read-only, so a hostile name can at worst fail to match a table.
    """
    sql = f"SELECT {what} FROM {_quote_identifier(table)}{tail}"  # noqa: S608
    return conn.execute(sql, params).fetchall()  # nosemgrep


def _sqlite_page(conn: sqlite3.Connection, table: str) -> Page:
    columns = [
        str(row[0]) for row in conn.execute("SELECT name FROM pragma_table_info(?)", (table,))
    ]
    (count_value,) = _select_from(conn, table, "COUNT(*)")[0]
    count = count_value if isinstance(count_value, int) else 0
    cache: dict[int, list[tuple[object, ...]]] = {}

    def cell(value: object) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, bytes):
            return f"<{len(value)} bytes>"
        return str(value)

    def row_line(values: Sequence[str], style: str = "") -> Line:
        line = Line()
        for i, value in enumerate(values):
            text = truncate(_one_line(value), SQLITE_CELL_WIDTH)
            line.add("│" if i else "", Style.DIM)
            line.add(" " + text + " " * (SQLITE_CELL_WIDTH - text_width(text)) + " ", style)
        return line

    header = [
        row_line(columns, Style.BOLD),
        Line.of("┼".join("─" * (SQLITE_CELL_WIDTH + 2) for _ in columns), Style.DIM),
    ]

    def make(index: int) -> Line:
        if index < len(header):
            return header[index]
        row = index - len(header)
        block = row // _SQLITE_FETCH
        if block not in cache:
            cache[block] = _select_from(
                conn, table, "*", " LIMIT ? OFFSET ?", (_SQLITE_FETCH, block * _SQLITE_FETCH)
            )
        values = cache[block][row % _SQLITE_FETCH]
        return row_line([cell(v) for v in values])

    return Page(f"{table} ({count} 行)", LazyLines(count + len(header), make))


# ---- binary -------------------------------------------------------------------
def hex_lines(data: bytes) -> LazyLines:
    rows = (len(data) + HEX_BYTES_PER_ROW - 1) // HEX_BYTES_PER_ROW

    def make(index: int) -> Line:
        chunk = data[index * HEX_BYTES_PER_ROW : (index + 1) * HEX_BYTES_PER_ROW]
        hexes = " ".join(f"{b:02x}" for b in chunk)
        text = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in chunk)
        return (
            Line.of(f"{index * HEX_BYTES_PER_ROW:08x}  ", Style.DIM)
            .add(f"{hexes:<{HEX_BYTES_PER_ROW * 3}} ", Style.NUMBER)
            .add(text)
        )

    return LazyLines(max(rows, 1), make)
