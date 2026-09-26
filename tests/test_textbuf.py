from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from view_and_edit.textbuf import (
    DecodeError,
    FileFormat,
    Pos,
    TextBuffer,
    atomic_write,
    decode_text,
    encode_text,
)


def buf(text: str, row: int = 0, col: int = 0) -> TextBuffer:
    b = TextBuffer.from_text(text)
    b.goto(row, col)
    return b


class EditingTest(unittest.TestCase):
    def test_typing_and_newline_keeps_indent(self) -> None:
        b = buf("    x", 0, 5)
        b.newline()
        b.insert("y")
        self.assertEqual(b.text, "    x\n    y")
        self.assertEqual(b.cursor, Pos(1, 5))
        self.assertTrue(b.dirty)

    def test_typed_characters_undo_as_one_step(self) -> None:
        b = buf("")
        for ch in "hello":
            b.insert(ch)
        b.insert(" ")
        for ch in "world":
            b.insert(ch)
        b.undo()
        self.assertEqual(b.text, "hello ")
        b.undo()
        b.undo()
        self.assertEqual(b.text, "")
        self.assertFalse(b.dirty)
        b.redo()
        self.assertEqual(b.text, "hello")

    def test_backspace_joins_lines_and_removes_indent_units(self) -> None:
        b = buf("ab\ncd", 1, 0)
        b.backspace()
        self.assertEqual(b.text, "abcd")
        self.assertEqual(b.cursor, Pos(0, 2))
        b = buf("        x", 0, 8)
        b.backspace()
        self.assertEqual(b.text, "    x")

    def test_delete_at_line_end_joins_next_line(self) -> None:
        b = buf("ab\ncd", 0, 2)
        b.delete()
        self.assertEqual(b.text, "abcd")

    def test_selection_is_replaced_by_typing(self) -> None:
        b = buf("hello world", 0, 0)
        for _ in range(5):
            b.right(select=True)
        self.assertEqual(b.selected_text(), "hello")
        b.insert("bye")
        self.assertEqual(b.text, "bye world")

    def test_multiline_selection_cut_and_paste(self) -> None:
        b = buf("one\ntwo\nthree", 0, 1)
        b.vertical(1, select=True)
        self.assertEqual(b.cut(), "ne\nt")
        self.assertEqual(b.text, "owo\nthree")
        b.doc_end()
        b.insert("ne\nt")
        self.assertEqual(b.text, "owo\nthreene\nt")

    def test_cut_without_selection_takes_whole_line(self) -> None:
        b = buf("a\nb\nc", 1, 0)
        self.assertEqual(b.cut(), "b\n")
        self.assertEqual(b.text, "a\nc")

    def test_indent_and_dedent_selected_lines(self) -> None:
        b = buf("a\nb\nc", 0, 0)
        b.vertical(1, select=True)
        b.end(select=True)
        b.indent()
        self.assertEqual(b.text, "    a\n    b\nc")
        b.dedent()
        self.assertEqual(b.text, "a\nb\nc")
        b.undo()
        self.assertEqual(b.text, "    a\n    b\nc")

    def test_dedent_without_selection_keeps_cursor_on_text(self) -> None:
        b = buf("    abc", 0, 6)
        b.dedent()
        self.assertEqual(b.text, "abc")
        self.assertEqual(b.cursor, Pos(0, 2))
        self.assertIsNone(b.selection())

    def test_tab_inserts_spaces_to_next_stop(self) -> None:
        b = buf("ab", 0, 2)
        b.indent()
        self.assertEqual(b.text, "ab  ")


class MovementTest(unittest.TestCase):
    def test_vertical_move_keeps_display_column_across_wide_chars(self) -> None:
        b = buf("日本語テキスト\nabcdefghij", 0, 2)  # column 4
        b.vertical(1)
        self.assertEqual(b.cursor, Pos(1, 4))
        b.vertical(-1)
        self.assertEqual(b.cursor, Pos(0, 2))

    def test_vertical_move_remembers_goal_over_short_lines(self) -> None:
        b = buf("abcdef\nx\nabcdef", 0, 5)
        b.vertical(1)
        self.assertEqual(b.cursor, Pos(1, 1))
        b.vertical(1)
        self.assertEqual(b.cursor, Pos(2, 5))

    def test_smart_home_toggles(self) -> None:
        b = buf("    code", 0, 8)
        b.home()
        self.assertEqual(b.cursor.col, 4)
        b.home()
        self.assertEqual(b.cursor.col, 0)

    def test_word_moves(self) -> None:
        b = buf("foo.bar baz", 0, 0)
        b.word_right()
        self.assertEqual(b.cursor.col, 3)
        b.word_right()
        self.assertEqual(b.cursor.col, 7)
        b.word_left()
        self.assertEqual(b.cursor.col, 4)

    def test_arrow_collapses_selection(self) -> None:
        b = buf("abcdef", 0, 1)
        b.right(select=True)
        b.right(select=True)
        b.left()
        self.assertEqual(b.cursor, Pos(0, 1))
        self.assertIsNone(b.selection())

    def test_click_maps_display_column(self) -> None:
        b = buf("あいう", 0, 0)
        b.click(0, 3)
        self.assertEqual(b.cursor, Pos(0, 1))


class FindTest(unittest.TestCase):
    def test_find_forward_wraps_and_is_case_insensitive(self) -> None:
        b = buf("Foo\nbar foo\nbaz", 0, 0)
        self.assertTrue(b.find("foo"))
        self.assertEqual(b.selection(), (Pos(0, 0), Pos(0, 3)))
        self.assertTrue(b.find("foo"))
        self.assertEqual(b.selection(), (Pos(1, 4), Pos(1, 7)))
        self.assertTrue(b.find("foo"))
        self.assertEqual(b.selection(), (Pos(0, 0), Pos(0, 3)))

    def test_find_backward(self) -> None:
        b = buf("foo foo\nfoo", 1, 3)
        b.find("foo", forward=False)
        self.assertEqual(b.selection(), (Pos(1, 0), Pos(1, 3)))
        b.find("foo", forward=False)
        self.assertEqual(b.selection(), (Pos(0, 4), Pos(0, 7)))
        b.find("foo", forward=False)
        self.assertEqual(b.selection(), (Pos(0, 0), Pos(0, 3)))

    def test_find_missing(self) -> None:
        self.assertFalse(buf("abc").find("zzz"))


class FileFormatTest(unittest.TestCase):
    def test_round_trip_preserves_crlf_bom_and_missing_final_newline(self) -> None:
        for data in (
            b"a\r\nb\r\n",
            b"\xef\xbb\xbfa\nb",
            b"no newline",
            b"",
            "日本語\n".encode("cp932"),
        ):
            with self.subTest(data=data):
                text, fmt = decode_text(data)
                self.assertEqual(encode_text(text, fmt), data)

    def test_shift_jis_is_detected(self) -> None:
        text, fmt = decode_text("こんにちは\n".encode("cp932"))
        self.assertEqual((text, fmt.encoding), ("こんにちは", "cp932"))

    def test_binary_is_rejected(self) -> None:
        with self.assertRaises(DecodeError):
            decode_text(b"\x00\x01\x02")

    def test_atomic_write_keeps_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.sh"
            path.write_text("old")
            path.chmod(0o755)
            atomic_write(path, encode_text("new", FileFormat()))
            self.assertEqual(path.read_text(), "new\n")
            self.assertEqual(path.stat().st_mode & 0o777, 0o755)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["run.sh"])


if __name__ == "__main__":
    unittest.main()
