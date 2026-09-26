"""Width, key parsing, kitty graphics, and syntax colouring."""

from __future__ import annotations

import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from view_and_edit.formats import Kind, detect
from view_and_edit.keys import InputParser, Key, Mouse, Paste
from view_and_edit.kitty import CellSize, fit_cells, place, png_size, transmit
from view_and_edit.structured import UnsafeXmlError, parse_xml
from view_and_edit.syntax import Highlighter, language_for
from view_and_edit.ui import Line, Style, render_line
from view_and_edit.width import (
    column_to_index,
    fit,
    slice_columns,
    text_width,
    truncate,
    truncate_left,
    wrap,
)


def tiny_png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class WidthTest(unittest.TestCase):
    def test_wide_characters_count_double(self) -> None:
        self.assertEqual(text_width("aあ"), 3)
        self.assertEqual(text_width("é"), 1)

    def test_slicing_never_splits_wide_characters(self) -> None:
        self.assertEqual(slice_columns("あいう", 1, 4), " い ")
        self.assertEqual(fit("あいう", 5), "あい ")
        self.assertEqual(text_width(fit("あいう", 5)), 5)

    def test_truncate_both_ends(self) -> None:
        self.assertEqual(truncate("abcdef", 4), "abc…")
        self.assertEqual(truncate_left("/very/long/path", 6), "…/path")

    def test_wrap_prefers_spaces(self) -> None:
        self.assertEqual(wrap("hello big world", 9), ["hello big", "world"])
        self.assertEqual(wrap("あいうえお", 4), ["あい", "うえ", "お"])

    def test_column_to_index(self) -> None:
        self.assertEqual(column_to_index("あいう", 2), 1)
        self.assertEqual(column_to_index("ab", 10), 2)


class KeyParserTest(unittest.TestCase):
    def parse(self, data: bytes) -> list[object]:
        parser = InputParser()
        return [*parser.feed(data), *parser.flush()]

    def test_plain_and_control_keys(self) -> None:
        self.assertEqual(
            self.parse(b"a\r\x13\x7f"), [Key("a"), Key("enter"), Key("ctrl+s"), Key("backspace")]
        )

    def test_arrows_with_modifiers(self) -> None:
        self.assertEqual(
            self.parse(b"\x1b[A\x1b[1;2B\x1b[1;5C\x1bOD\x1b[5~\x1b[3;2~"),
            [
                Key("up"),
                Key("shift+down"),
                Key("ctrl+right"),
                Key("left"),
                Key("pageup"),
                Key("shift+delete"),
            ],
        )

    def test_utf8_split_across_reads(self) -> None:
        parser = InputParser()
        data = "あ".encode()
        self.assertEqual(parser.feed(data[:1]), [])
        self.assertEqual(parser.feed(data[1:]), [Key("あ")])

    def test_lone_escape_needs_flush(self) -> None:
        parser = InputParser()
        self.assertEqual(parser.feed(b"\x1b"), [])
        self.assertTrue(parser.has_pending_escape)
        self.assertEqual(parser.flush(), [Key("escape")])

    def test_alt_key(self) -> None:
        self.assertEqual(self.parse(b"\x1bb"), [Key("alt+b")])

    def test_sgr_mouse(self) -> None:
        self.assertEqual(
            self.parse(b"\x1b[<0;5;3M\x1b[<64;1;1M\x1b[<65;1;1M\x1b[<32;6;3M"),
            [
                Mouse("press", 4, 2),
                Mouse("wheel_up", 0, 0),
                Mouse("wheel_down", 0, 0),
                Mouse("drag", 5, 2),
            ],
        )

    def test_bracketed_paste_keeps_newlines(self) -> None:
        parser = InputParser()
        events = parser.feed(b"\x1b[200~line1\nli")
        events += parser.feed(b"ne2\x1b[201~x")
        self.assertEqual(events, [Paste("line1\nline2"), Key("x")])


class KittyTest(unittest.TestCase):
    def test_png_size(self) -> None:
        self.assertEqual(png_size(tiny_png(3, 2)), (3, 2))

    def test_transmit_chunks_payload(self) -> None:
        data = transmit(7, b"x" * 10000)
        self.assertTrue(data.startswith("\x1b_Ga=t,f=100,t=d,i=7,q=2,m=1;"))
        self.assertIn("\x1b_Gm=0;", data)
        self.assertEqual(data.count("\x1b\\"), 4)

    def test_place_moves_cursor_first(self) -> None:
        self.assertEqual(place(3, 1, 2, 10, 5), "\x1b[2;3H\x1b_Ga=p,i=3,p=1,c=10,r=5,C=1,q=2\x1b\\")

    def test_fit_keeps_aspect_and_does_not_upscale(self) -> None:
        cell = CellSize(10, 20)
        self.assertEqual(fit_cells((1000, 1000), 80, 20, cell), (40, 20))
        self.assertEqual(fit_cells((1000, 200), 50, 40, cell), (50, 5))
        self.assertEqual(fit_cells((20, 20), 80, 40, cell), (2, 1))

    def test_fit_upscale_fills_box(self) -> None:
        cell = CellSize(10, 20)
        self.assertEqual(fit_cells((320, 180), 80, 40, cell, upscale=True), (80, 22))


class SyntaxTest(unittest.TestCase):
    def styles(self, line: Line) -> list[tuple[str, str]]:
        return [(s.text, s.style) for s in line.spans if s.style]

    def test_python_keywords_strings_comments(self) -> None:
        h = Highlighter(language_for(Path("a.py")))
        self.assertEqual(
            self.styles(h.line('def f(): return "x#y"  # note')),
            [
                ("def", Style.KEYWORD),
                ("return", Style.KEYWORD),
                ('"x#y"', Style.STRING),
                ("# note", Style.COMMENT),
            ],
        )

    def test_block_comment_spans_lines(self) -> None:
        h = Highlighter("js")
        h.line("a /* start")
        self.assertEqual(
            self.styles(h.line("still */ const")),
            [("still */", Style.COMMENT), ("const", Style.KEYWORD)],
        )

    def test_unknown_language_is_plain(self) -> None:
        self.assertEqual(
            Highlighter(language_for(Path("notes.txt"))).line("if x").spans[0].style, ""
        )


class RenderLineTest(unittest.TestCase):
    def test_pads_and_resets(self) -> None:
        out = render_line(Line().add("ab", Style.BOLD), 4)
        self.assertEqual(out, "\x1b[0;1mab\x1b[0m  \x1b[0m")

    def test_control_characters_are_neutralised(self) -> None:
        out = render_line(Line.of("a\x1b[2Jb"), 10)
        self.assertNotIn("\x1b[2J", out)


if __name__ == "__main__":
    unittest.main()


class XmlSafetyTest(unittest.TestCase):
    BOMB = (
        '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaaaaaaaa">'
        '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]><r>&b;</r>'
    )

    def test_plain_office_xml_parses(self) -> None:
        self.assertEqual(parse_xml(b"<r><t>hi</t></r>").findtext("t"), "hi")

    def test_entity_bomb_is_refused_before_expansion(self) -> None:
        with self.assertRaises(UnsafeXmlError):
            parse_xml(self.BOMB.encode())

    def test_dtd_hidden_in_utf16_is_still_refused(self) -> None:
        data = self.BOMB.replace('version="1.0"', 'version="1.0" encoding="UTF-16"')
        with self.assertRaises(UnsafeXmlError):
            parse_xml(data.encode("utf-16"))


class DetectTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_typescript_is_text_not_video(self) -> None:
        for name in ("app.ts", "mod.mts"):
            path = self.root / name
            path.write_text("export const x: number = 1;\n")
            self.assertEqual(detect(path), Kind.TEXT, name)

    def test_mpeg_transport_stream_is_video_by_content(self) -> None:
        packet = bytes([0x47]) + bytes(187)
        path = self.root / "clip.ts"
        path.write_bytes(packet * 4)
        self.assertEqual(detect(path), Kind.VIDEO)

    def test_unknown_extension_uses_magic_bytes(self) -> None:
        path = self.root / "noext"
        path.write_bytes(b"%PDF-1.7\n")
        self.assertEqual(detect(path), Kind.PDF)
