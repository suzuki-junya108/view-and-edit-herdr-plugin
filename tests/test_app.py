"""Drive the whole app with a fake terminal and check what the user would see."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.fixtures import make_samples, tiny_png
from view_and_edit import kitty
from view_and_edit.app import App
from view_and_edit.browser import BrowserScreen
from view_and_edit.editor import EditorScreen
from view_and_edit.formats import Kind
from view_and_edit.keys import Key, Mouse, Paste
from view_and_edit.media import PREVIEW_PIXELS, Cache, preview_png
from view_and_edit.picker import PickerScreen
from view_and_edit.target import Target
from view_and_edit.viewer import ViewerScreen
from view_and_edit.width import text_width

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b_G[^\x1b]*\x1b\\")
WIDTH, HEIGHT = 120, 30


class FakeTerminal:
    def __init__(self) -> None:
        self.output: list[str] = []

    def size(self) -> tuple[int, int]:
        return WIDTH, HEIGHT

    def cell_size(self) -> kitty.CellSize:
        return kitty.CellSize(10, 20)

    def write(self, data: str) -> None:
        self.output.append(data)


class AppTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.files = make_samples(self.root)
        self.terminal = FakeTerminal()
        self.app = App(self.terminal, Cache())

    def tearDown(self) -> None:
        self.app.quit()
        self._tmp.cleanup()

    def screen_text(self) -> str:
        frame = self.app.compose()
        return "\n".join(_ANSI.sub("", line) for line in frame.lines)

    def keys(self, *names: str) -> None:
        for name in names:
            self.app.dispatch(Key(name))

    def type(self, text: str) -> None:
        for ch in text:
            self.app.dispatch(Key(ch))

    def click(self, text: str, row: int | None = None) -> None:
        """Left-click the first cell of `text`, searching only `row` when given (-1: footer)."""
        lines = self.screen_text().split("\n")
        rows = [row % len(lines)] if row is not None else range(len(lines))
        for y in rows:
            index = lines[y].find(text)
            if index >= 0:
                self.app.dispatch(Mouse("press", text_width(lines[y][:index]), y))
                self.app.dispatch(Mouse("release", text_width(lines[y][:index]), y))
                return
        self.fail(f"{text!r} is not on screen")

    def settle(self, timeout: float = 10.0) -> str:
        """Render until background work (previews) finishes."""
        deadline = time.monotonic() + timeout
        text = self.screen_text()
        while "読み込み中" in text and time.monotonic() < deadline:
            time.sleep(0.05)
            text = self.screen_text()
        return text


class BrowserTest(AppTestCase):
    def open_browser(self, select: Path | None = None) -> BrowserScreen:
        screen = BrowserScreen(self.app, self.root, select=select)
        self.app.push(screen)
        return screen

    def test_lists_folders_first_and_hides_dotfiles(self) -> None:
        self.open_browser()
        text = self.screen_text()
        self.assertIn("subdir/", text)
        self.assertLess(text.index("subdir/"), text.index("archive.zip"))
        self.assertNotIn(".hidden", text)
        self.keys(".")
        self.assertIn(".hidden", self.screen_text())

    def test_preview_follows_selection_after_short_delay(self) -> None:
        self.open_browser(select=self.files["notes.md"])
        self.assertIn("Markdown", self.screen_text())
        time.sleep(0.15)
        self.assertIn("見出し", self.screen_text())

    def test_enter_folder_and_go_back_up(self) -> None:
        screen = self.open_browser()
        self.keys("enter")  # first entry is the folder
        self.assertEqual(screen.directory, self.root / "subdir")
        self.assertIn("inside.txt", self.screen_text())
        self.keys("left")
        self.assertEqual(screen.directory, self.root)
        self.assertEqual(screen.selected.name if screen.selected else None, "subdir")

    def test_filter_narrows_and_escape_clears(self) -> None:
        screen = self.open_browser()
        self.keys("/")
        self.type("dat")
        self.assertEqual({e.name for e in screen.entries}, {"data.csv", "data.tsv", "data.json"})
        self.keys("escape")
        self.assertGreater(len(screen.entries), 3)

    def test_open_file_then_q_returns_to_browser(self) -> None:
        self.open_browser(select=self.files["data.csv"])
        self.keys("enter")
        self.assertIsInstance(self.app.top, ViewerScreen)
        self.keys("q")
        self.assertIsInstance(self.app.top, BrowserScreen)
        self.keys("q")
        self.assertFalse(self.app.running)

    def test_create_rename_and_new_folder(self) -> None:
        screen = self.open_browser()
        self.keys("m")
        self.type("newdir")
        self.keys("enter")
        self.assertTrue((self.root / "newdir").is_dir())
        self.assertEqual(screen.selected.name if screen.selected else None, "newdir")
        self.keys("r", "ctrl+u")
        self.type("renamed")
        self.keys("enter")
        self.assertTrue((self.root / "renamed").is_dir())
        self.keys("n")
        self.type("fresh.txt")
        self.keys("enter")
        self.assertIsInstance(self.app.top, EditorScreen)
        self.type("hello")
        self.keys("ctrl+s", "ctrl+q")
        self.assertEqual((self.root / "fresh.txt").read_text(), "hello\n")
        # Back in the list, the new file is the one selected (so d/r act on it).
        self.assertEqual(screen.selected.name if screen.selected else None, "fresh.txt")

    def test_invalid_names_are_rejected(self) -> None:
        self.open_browser()
        self.keys("m")
        self.type("../escape")
        self.keys("enter")
        self.assertFalse((self.root.parent / "escape").exists())
        self.assertIn("使えない名前", self.screen_text())

    def test_trash_asks_first(self) -> None:
        self.open_browser(select=self.files["data.tsv"])
        with mock.patch("view_and_edit.browser.move_to_trash", return_value=None) as trash:
            self.keys("d")
            self.assertIn("ゴミ箱に移動しますか", self.screen_text())
            self.keys("n")
            trash.assert_not_called()
            self.keys("d", "y")
            trash.assert_called_once_with(self.files["data.tsv"])

    def test_mouse_click_selects_and_double_click_opens(self) -> None:
        screen = self.open_browser()
        self.click("data.json")
        self.assertEqual(screen.selected.name if screen.selected else None, "data.json")
        self.click("data.json")
        self.assertIsInstance(self.app.top, ViewerScreen)

    def test_parent_row_click_goes_up(self) -> None:
        screen = BrowserScreen(self.app, self.root / "subdir")
        self.app.push(screen)
        self.click("..")
        self.assertEqual(screen.directory, self.root)
        self.assertEqual(screen.selected.name if screen.selected else None, "subdir")

    def test_footer_hints_are_clickable(self) -> None:
        self.open_browser(select=self.files["data.csv"])
        self.click("Enter", row=-1)
        self.assertIsInstance(self.app.top, ViewerScreen)
        self.click("q", row=-1)
        self.assertIsInstance(self.app.top, BrowserScreen)
        self.click("?", row=-1)
        self.assertIn("キー操作", self.screen_text())
        self.click("キー操作")  # any click closes the help
        self.assertNotIn("キー操作", self.screen_text())

    def test_trash_confirm_buttons_are_clickable(self) -> None:
        self.open_browser(select=self.files["data.tsv"])
        with mock.patch("view_and_edit.browser.move_to_trash", return_value=None) as trash:
            self.keys("d")
            self.click("いいえ", row=-1)
            trash.assert_not_called()
            self.assertNotIn("ゴミ箱に移動しますか", self.screen_text())
            self.keys("d")
            self.click("はい", row=-1)
            trash.assert_called_once_with(self.files["data.tsv"])

    def test_help_overlay(self) -> None:
        self.open_browser()
        self.keys("?")
        self.assertIn("ゴミ箱に移動", self.screen_text())
        self.keys("x")
        self.assertNotIn("キー操作", self.screen_text())


class PickerTest(AppTestCase):
    def open_picker(self, note: str | None = None) -> PickerScreen:
        targets = [
            Target(self.files["code.py"], 150),
            Target(self.files["data.csv"]),
            Target(self.root / "gone.txt"),  # removed after the screen was read
            Target(self.root / "subdir"),
        ]
        screen = PickerScreen(self.app, targets, self.root, "画面に出ているファイル", note)
        self.app.push(screen)
        return screen

    def test_lists_the_picks_with_their_lines_under_a_heading(self) -> None:
        screen = self.open_picker()
        text = self.screen_text()
        self.assertIn("画面に出ているファイル", text)
        self.assertIn("code.py:150", text)
        self.assertIn("subdir/", text)
        self.assertIn("フォルダを見る", text)
        self.assertNotIn("gone.txt", text)
        self.assertEqual(
            [e.path for e in screen.entries][:2], [self.files["code.py"], self.files["data.csv"]]
        )

    def test_enter_opens_at_the_line_and_q_comes_back(self) -> None:
        screen = self.open_picker()
        self.keys("enter")
        self.assertIsInstance(self.app.top, ViewerScreen)
        self.assertIn("150 x147 = 147", self.screen_text())
        self.keys("q")
        self.assertIs(self.app.top, screen)
        self.keys("q")
        self.assertFalse(self.app.running)

    def test_edit_starts_at_the_line(self) -> None:
        self.open_picker()
        self.keys("e")
        self.assertIsInstance(self.app.top, EditorScreen)
        self.assertIn("150:1", self.screen_text())

    def test_folder_pick_opens_the_browser_inside_it(self) -> None:
        self.open_picker()
        self.click("subdir/")
        self.click("subdir/")
        top = self.app.top
        self.assertIsInstance(top, BrowserScreen)
        self.assertNotIsInstance(top, PickerScreen)
        self.assertEqual(
            top.directory if isinstance(top, BrowserScreen) else None, self.root / "subdir"
        )

    def test_parent_row_and_left_open_the_file_browser(self) -> None:
        screen = self.open_picker()
        self.click("フォルダを見る")
        self.assertIsInstance(self.app.top, BrowserScreen)
        self.assertNotIsInstance(self.app.top, PickerScreen)
        self.keys("q")
        self.assertIs(self.app.top, screen)
        self.keys("left")
        self.assertIn("archive.zip", self.screen_text())

    def test_long_paths_keep_their_file_name_and_nearby_files_are_relative(self) -> None:
        deep = self.root / ("very-long-folder-name-" * 6) / "target-name.txt"
        deep.parent.mkdir()
        deep.write_text("x\n")
        base = self.root / "subdir"
        screen = PickerScreen(self.app, [Target(deep), Target(self.files["data.csv"])], base, "h")
        self.app.push(screen)
        text = self.screen_text()
        self.assertIn("target-name.txt", text)
        self.assertIn("…", text)
        self.assertIn("../data.csv", text)

    def test_folder_actions_are_not_offered(self) -> None:
        self.open_picker()
        self.keys("n")
        self.keys("m")
        self.keys("r")
        self.assertIsNone(self.app.prompt)

    def test_filter_and_note(self) -> None:
        screen = self.open_picker(note="一部だけ探しました")
        self.assertIn("一部だけ探しました", self.screen_text())
        self.keys("/")
        self.type("csv")
        self.assertEqual([e.path for e in screen.entries], [self.files["data.csv"]])


class ViewerTest(AppTestCase):
    def view(self, name: str, line: int | None = None) -> ViewerScreen:
        screen = ViewerScreen(self.app, self.files[name], line)
        self.app.push(screen)
        return screen

    def test_every_sample_opens_without_error(self) -> None:
        for name in self.files:
            with self.subTest(name=name):
                self.app.screens.clear()
                self.view(name)
                text = self.settle()
                self.assertNotIn("表示できませんでした", text)
                self.assertNotIn("Traceback", text)

    def test_code_has_line_numbers_and_starts_at_line(self) -> None:
        self.view("code.py", line=150)
        text = self.screen_text()
        self.assertIn("150 x147 = 147", text)
        self.assertNotIn("def hello", text)

    def test_markdown_rendered_and_source_tab(self) -> None:
        self.view("notes.md")
        text = self.screen_text()
        self.assertIn("☑ 完了", text)
        self.assertIn("太字", text)
        self.assertNotIn("**", text)
        self.keys("tab")
        self.assertIn("**太字**", self.screen_text())

    def test_html_text_tab_strips_markup(self) -> None:
        self.view("page.html")
        self.keys("tab")
        text = self.screen_text()
        self.assertIn("本文 & 説明", text)
        self.assertNotIn("x()", text)

    def test_csv_and_xlsx_tables(self) -> None:
        self.view("data.csv")
        self.assertRegex(self.screen_text(), r"a,b\s+│ 4")
        self.app.screens.clear()
        self.view("sheet.xlsx")
        text = self.screen_text()
        self.assertIn("品名", text)
        self.assertIn("42", text)
        self.assertIn("売上", text)
        self.keys("right")
        self.assertIn("TRUE", self.screen_text())

    def test_sqlite_scrolls_lazily(self) -> None:
        self.view("app.sqlite")
        self.assertIn("users (500 行)", self.screen_text())
        self.keys("G")
        self.assertIn("user499", self.screen_text())

    def test_sqlite_hostile_table_name_is_only_a_name(self) -> None:
        db = self.root / "hostile.sqlite"
        conn = sqlite3.connect(db)
        conn.execute('CREATE TABLE "x"" ; DROP TABLE keep; --" (v TEXT)')
        conn.execute('INSERT INTO "x"" ; DROP TABLE keep; --" VALUES (\'ok\')')
        conn.execute("CREATE TABLE keep (v TEXT)")
        conn.commit()
        conn.close()
        self.app.push(ViewerScreen(self.app, db))
        self.assertIn("keep (0 行)", self.screen_text())  # names sort, so "keep" comes first
        self.keys("right")
        text = self.screen_text()
        self.assertIn('x" ; DROP TABLE keep; -- (1 行)', text)
        self.assertIn("ok", text)

    def test_view_tabs_and_page_arrows_are_clickable(self) -> None:
        self.view("notes.md")
        self.click("ソース", row=1)
        self.assertIn("**太字**", self.screen_text())
        self.app.screens.clear()
        self.view("sheet.xlsx")
        self.click("▶", row=1)
        self.assertIn("TRUE", self.screen_text())
        self.click("◀", row=1)
        self.assertIn("品名", self.screen_text())

    def test_player_line_click_toggles_playback(self) -> None:
        audio = self.root / "tone.wav"
        audio.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt ")
        screen = ViewerScreen(self.app, audio)
        self.app.push(screen)
        with mock.patch.object(ViewerScreen, "_toggle_play") as toggle:
            self.click("停止中")
            toggle.assert_called_once_with()
        self.assertEqual(screen.path, audio)

    def test_search_moves_to_match(self) -> None:
        screen = self.view("code.py")
        self.keys("/")
        self.type("x180 ")
        self.keys("enter")
        self.assertIn("x180 = 180", self.screen_text())
        self.assertEqual(screen.pane.focus_line, 182)

    def test_image_is_placed_with_kitty_graphics(self) -> None:
        self.view("image.png")
        self.settle()
        frame = self.app.compose()
        self.assertEqual(len(frame.images), 1)
        placement = frame.images[0]
        self.assertEqual(kitty.png_size(placement.png), (40, 20))
        self.app.draw()
        self.assertIn("\x1b_Ga=t,f=100", "".join(self.terminal.output))

    @unittest.skipUnless(shutil.which("sips"), "needs macOS sips")
    def test_image_preview_is_capped_but_never_enlarged(self) -> None:
        small_jpeg = self.root / "small.jpg"
        source = str(self.files["image.png"])
        subprocess.run(
            ["sips", "-s", "format", "jpeg", source, "--out", str(small_jpeg)],
            capture_output=True,
            check=True,
        )
        huge_png = self.root / "huge.png"
        huge_png.write_bytes(tiny_png(PREVIEW_PIXELS * 2, 10))
        for path, expected in ((small_jpeg, (40, 20)), (huge_png, (PREVIEW_PIXELS, 5))):
            with self.subTest(path=path.name):
                png = preview_png(path, Kind.IMAGE, self.app.cache).read_bytes()
                self.assertEqual(kitty.png_size(png), expected)

    def test_binary_falls_back_to_hex(self) -> None:
        self.view("blob.bin")
        self.assertIn("00 01 02 03", self.screen_text())

    def test_shift_jis_text(self) -> None:
        self.view("sjis.txt")
        self.assertIn("シフトJIS", self.screen_text())

    def test_edit_key_opens_editor_and_reloads_after(self) -> None:
        self.view("data.json")
        self.keys("e")
        self.assertIsInstance(self.app.top, EditorScreen)
        self.keys("ctrl+end")
        self.app.dispatch(Paste("\n"))
        self.keys("ctrl+s", "ctrl+q")
        self.assertIsInstance(self.app.top, ViewerScreen)

    def test_non_editable_format_explains(self) -> None:
        self.view("image.png")
        self.keys("e")
        self.assertIn("編集できません", self.screen_text())


class EditorTest(AppTestCase):
    def edit(self, name: str, line: int | None = None) -> EditorScreen:
        screen = EditorScreen(self.app, self.files[name], line)
        self.app.push(screen)
        return screen

    def test_type_save_preserves_encoding(self) -> None:
        self.edit("sjis.txt")
        self.keys("end")
        self.type("です")
        self.keys("ctrl+s")
        self.assertEqual(self.files["sjis.txt"].read_bytes(), "シフトJISです\n".encode("cp932"))
        self.assertIn("保存しました", self.screen_text())

    def test_close_with_unsaved_changes_asks(self) -> None:
        self.edit("code.py")
        self.type("#")
        self.keys("ctrl+q")
        self.assertIn("変更を保存しますか", self.screen_text())
        self.keys("escape")
        self.assertIsInstance(self.app.top, EditorScreen)
        self.keys("ctrl+q", "n")
        self.assertFalse(self.app.running)
        self.assertTrue(self.files["code.py"].read_text().startswith("def hello"))

    def test_question_mark_is_typed_not_help(self) -> None:
        screen = self.edit("data.tsv")
        self.type("?")
        self.assertTrue(screen.buffer.lines[0].startswith("?"))

    def test_find_and_goto(self) -> None:
        screen = self.edit("code.py")
        self.keys("ctrl+f")
        self.type("x99 ")
        self.keys("enter")
        self.assertEqual(screen.buffer.cursor.row, 101)
        self.keys("ctrl+g")
        self.type("3")
        self.keys("enter")
        self.assertEqual(screen.buffer.cursor.row, 2)

    def test_external_change_is_not_silently_overwritten(self) -> None:
        path = self.files["data.tsv"]
        self.edit("data.tsv")
        self.type("Z")
        time.sleep(0.01)
        path.write_text("changed elsewhere\n")
        future = path.stat().st_mtime + 5
        os.utime(path, (future, future))
        self.keys("ctrl+s")
        self.assertIn("ほかで変更されています", self.screen_text())
        self.keys("n")
        self.assertEqual(path.read_text(), "changed elsewhere\n")

    def test_footer_save_is_clickable_and_help_hint_is_not_duplicated(self) -> None:
        self.edit("data.tsv")
        self.type("Z")
        footer = self.screen_text().split("\n")[-1]
        self.assertNotIn("? ヘルプ", footer)
        self.click("^S", row=-1)
        self.assertTrue(self.files["data.tsv"].read_text().startswith("Z"))

    def test_footer_buttons_stay_while_a_message_shows(self) -> None:
        self.edit("data.tsv")
        self.type("Z")
        self.keys("ctrl+s")
        footer = self.screen_text().split("\n")[-1]
        self.assertIn("保存しました", footer)
        self.click("^Q", row=-1)
        self.assertFalse(self.app.running)

    def test_close_confirm_offers_a_way_back(self) -> None:
        self.edit("code.py")
        self.type("#")
        self.keys("ctrl+q")
        self.click("編集に戻る", row=-1)
        self.assertIsInstance(self.app.top, EditorScreen)
        self.assertIsNone(self.app.confirm)

    def test_mouse_drag_selects(self) -> None:
        screen = self.edit("data.csv")
        gutter = len(str(len(screen.buffer.lines))) + 2
        self.app.dispatch(Mouse("press", gutter, 1))
        self.app.dispatch(Mouse("drag", gutter + 4, 1))
        self.assertEqual(screen.buffer.selected_text(), "name")


if __name__ == "__main__":
    unittest.main()
