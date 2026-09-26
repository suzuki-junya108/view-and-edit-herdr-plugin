"""Decide how to show a file: by extension first, then by its magic number."""

from __future__ import annotations

from enum import Enum
from pathlib import Path


class Kind(Enum):
    DIRECTORY = "フォルダ"
    TEXT = "テキスト"
    MARKDOWN = "Markdown"
    HTML = "HTML"
    IMAGE = "画像"
    SVG = "SVG 画像"
    VIDEO = "動画"
    AUDIO = "音声"
    PDF = "PDF"
    DOCUMENT = "文書"
    SPREADSHEET = "表計算"
    PRESENTATION = "スライド"
    CSV = "表 (CSV)"
    TSV = "表 (TSV)"
    JSON = "JSON"
    JSONL = "JSON Lines"
    NOTEBOOK = "Jupyter Notebook"
    SQLITE = "SQLite"
    ARCHIVE = "アーカイブ"
    FONT = "フォント"
    BINARY = "バイナリ"


_BY_EXTENSION: dict[str, Kind] = {}
for _kind, _exts in {
    Kind.MARKDOWN: "md markdown mdx mkd",
    Kind.HTML: "html htm xhtml",
    Kind.IMAGE: "png jpg jpeg gif webp heic heif bmp tif tiff ico icns avif jp2 psd tga",
    Kind.SVG: "svg",
    # .ts / .mts are left out: far more often TypeScript than MPEG-TS; content decides.
    Kind.VIDEO: "mp4 m4v mov webm mkv avi wmv flv mpg mpeg 3gp m2ts",
    Kind.AUDIO: "mp3 m4a wav aac flac aiff aif ogg opus caf wma alac",
    Kind.PDF: "pdf",
    Kind.DOCUMENT: "docx doc rtf rtfd odt pages epub",
    Kind.SPREADSHEET: "xlsx xlsm numbers ods xls",
    Kind.PRESENTATION: "pptx ppt key odp",
    Kind.CSV: "csv",
    Kind.TSV: "tsv tab",
    Kind.JSON: "json geojson webmanifest",
    Kind.JSONL: "jsonl ndjson",
    Kind.NOTEBOOK: "ipynb",
    Kind.SQLITE: "sqlite sqlite3 db",
    Kind.ARCHIVE: "zip jar war whl tar tgz gz bz2 xz tbz2 txz apk ipa",
    Kind.FONT: "ttf otf ttc woff woff2",
    Kind.BINARY: "exe dll so dylib o a class pyc wasm bin dat dmg iso",
}.items():
    for _ext in _exts.split():
        _BY_EXTENSION[_ext] = _kind

# Leading bytes that identify common formats regardless of the file name.
_MAGIC: tuple[tuple[bytes, Kind], ...] = (
    (b"\x89PNG\r\n\x1a\n", Kind.IMAGE),
    (b"\xff\xd8\xff", Kind.IMAGE),
    (b"GIF8", Kind.IMAGE),
    (b"%PDF-", Kind.PDF),
    (b"SQLite format 3\x00", Kind.SQLITE),
    (b"PK\x03\x04", Kind.ARCHIVE),
    (b"\x1f\x8b", Kind.ARCHIVE),
)
# MPEG transport streams have no header; every 188-byte packet starts with 0x47.
_TS_PACKET = 188
_TS_SYNC = 0x47
_HEAD_BYTES = _TS_PACKET + 1


def detect(path: Path) -> Kind:
    if path.is_dir():
        # macOS bundles that are really documents.
        return _BY_EXTENSION.get(path.suffix.lower().lstrip("."), Kind.DIRECTORY)
    kind = _BY_EXTENSION.get(path.suffix.lower().lstrip("."))
    if kind is not None:
        return kind
    try:
        with path.open("rb") as handle:
            head = handle.read(_HEAD_BYTES)
    except OSError:
        return Kind.TEXT
    return _sniff(head)


def _sniff(head: bytes) -> Kind:
    """Kind from a file's leading bytes; text when nothing matches."""
    for magic, magic_kind in _MAGIC:
        if head.startswith(magic):
            return magic_kind
    if head[4:12] in (b"ftypisom", b"ftypmp42", b"ftypqt  ", b"ftypM4V "):
        return Kind.VIDEO
    if len(head) == _HEAD_BYTES and head[0] == head[_TS_PACKET] == _TS_SYNC:
        return Kind.VIDEO
    return Kind.TEXT  # the viewer falls back to hex if it does not decode as text


def is_editable(kind: Kind) -> bool:
    return kind in {
        Kind.TEXT,
        Kind.MARKDOWN,
        Kind.HTML,
        Kind.SVG,
        Kind.CSV,
        Kind.TSV,
        Kind.JSON,
        Kind.JSONL,
    }
