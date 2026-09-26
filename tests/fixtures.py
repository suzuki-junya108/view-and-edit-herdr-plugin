"""Build small sample files of every supported format for tests."""

from __future__ import annotations

import json
import sqlite3
import struct
import zipfile
import zlib
from pathlib import Path


def tiny_png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = struct.pack(">I", zlib.crc32(kind + body))
        return struct.pack(">I", len(body)) + kind + body + crc

    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


_XLSX_PARTS = {
    "[Content_Types].xml": '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
    "xl/workbook.xml": (
        '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="売上" sheetId="1" r:id="rId1"/>'
        '<sheet name="Two" sheetId="2" r:id="rId2"/></sheets></workbook>'
    ),
    "xl/_rels/workbook.xml.rels": (
        '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Target="/xl/worksheets/sheet2.xml"/></Relationships>'
    ),
    "xl/sharedStrings.xml": (
        '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<si><t>品名</t></si><si><t>りんご</t></si></sst>"
    ),
    "xl/worksheets/sheet1.xml": (
        '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
        '<row r="1"><c r="A1" t="s"><v>0</v></c>'
        '<c r="B1" t="inlineStr"><is><t>数量</t></is></c></row>'
        '<row r="2"><c r="A2" t="s"><v>1</v></c><c r="C2"><v>42</v></c></row>'
        "</sheetData></worksheet>"
    ),
    "xl/worksheets/sheet2.xml": (
        '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
        '<row r="1"><c r="A1" t="b"><v>1</v></c></row></sheetData></worksheet>'
    ),
}

_PPTX_SLIDE = (
    '<?xml version="1.0"?><p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
    ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:sp><p:txBody>'
    "<a:p><a:r><a:t>{title}</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
)


def make_samples(root: Path) -> dict[str, Path]:
    """Create one file per format under `root` and return them by short name."""
    files: dict[str, Path] = {}

    def put(name: str, data: bytes | str) -> Path:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            path.write_text(data, encoding="utf-8")
        else:
            path.write_bytes(data)
        files[name] = path
        return path

    numbered = "\n".join(f"x{i} = {i}" for i in range(200))
    put("code.py", "def hello():\n    return 'こんにちは'  # greet\n" + numbered)
    put(
        "notes.md",
        "# 見出し\n\n- [x] 完了\n- 項目 **太字**\n\n```python\nprint('hi')\n```\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n",
    )
    put(
        "page.html",
        "<html><head><style>p{}</style></head><body><h1>タイトル</h1>"
        "<p>本文 &amp; 説明</p><script>x()</script></body></html>",
    )
    put("data.csv", 'name,qty\nりんご,3\n"a,b",4\n')
    put("data.tsv", "k\tv\n1\t2\n")
    put("data.json", json.dumps({"name": "テスト", "items": [1, 2]}))
    put("log.jsonl", '{"a": 1}\n{"b": [2]}\nnot json\n')
    put(
        "book.ipynb",
        json.dumps(
            {
                "cells": [
                    {"cell_type": "markdown", "source": ["# ノート"]},
                    {
                        "cell_type": "code",
                        "execution_count": 1,
                        "source": "1 + 1",
                        "outputs": [{"output_type": "execute_result", "data": {"text/plain": "2"}}],
                    },
                ],
                "metadata": {"language_info": {"name": "python"}},
            }
        ),
    )
    put("image.png", tiny_png(40, 20))
    put("blob.bin", bytes(range(256)))
    put("sjis.txt", "シフトJIS\n".encode("cp932"))

    with zipfile.ZipFile(put("archive.zip", b""), "w") as zf:
        zf.writestr("inner/readme.txt", "hello")
    with zipfile.ZipFile(put("sheet.xlsx", b""), "w") as zf:
        for name, body in _XLSX_PARTS.items():
            zf.writestr(name, body)
    with zipfile.ZipFile(put("slides.pptx", b""), "w") as zf:
        zf.writestr("ppt/slides/slide1.xml", _PPTX_SLIDE.format(title="最初のスライド"))
        zf.writestr("ppt/slides/slide2.xml", _PPTX_SLIDE.format(title="Second"))

    db = put("app.sqlite", b"")
    db.unlink()
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE users (id INTEGER, name TEXT)")
    conn.executemany("INSERT INTO users VALUES (?, ?)", [(i, f"user{i}") for i in range(500)])
    conn.commit()
    conn.close()
    files["app.sqlite"] = db

    (root / "subdir").mkdir(exist_ok=True)
    (root / "subdir" / "inside.txt").write_text("inside\n")
    (root / ".hidden").write_text("secret\n")
    return files
