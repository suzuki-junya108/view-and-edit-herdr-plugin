from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from view_and_edit.locate import Locator, build_index
from view_and_edit.screen_paths import files_on_screen, looks_like_path
from view_and_edit.target import Target

# What an agent's pane looks like: prompt glyphs, tool-call bullets, a table, a path the
# agent wrapped onto the next line with indentation, and plenty of non-path words.
AGENT_SCREEN = """\
❯ fix the bug in browser.py please
⏺ Read(view_and_edit/browser.py)
  ⎿  Read 468 lines
⏺ The cause is in `view_and_edit/browser.py:120`. See REQUIREMENTS.md and
  docs/guide.md. Logged in as suzuki.junya, version 0.9.1, https://example.com/a.py
│ file            │ note │
│ notes/todo.txt  │ ok   │
  ⎿  $ uv run --script tools/very/long/path/that/wraps/in
     side.py && echo done
"""


class FilesOnScreenTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        for name in (
            "view_and_edit/browser.py",
            "REQUIREMENTS.md",
            "docs/guide.md",
            "notes/todo.txt",
            "tools/very/long/path/that/wraps/inside.py",
            "unrelated/side.py",
        ):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x\n")
        self.cwd = self.root / "docs"  # the agent runs somewhere other than the repo root
        self.locator = Locator([self.cwd], builder=lambda _root: build_index(self.root))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def found(self, text: str) -> list[Target]:
        return files_on_screen(text, self.cwd, self.locator)

    def test_lists_existing_files_lowest_on_screen_first(self) -> None:
        self.assertEqual(
            self.found(AGENT_SCREEN),
            [
                Target(self.root / "tools/very/long/path/that/wraps/inside.py"),
                Target(self.root / "notes/todo.txt"),
                Target(self.root / "docs/guide.md"),
                Target(self.root / "REQUIREMENTS.md"),
                Target(self.root / "view_and_edit/browser.py", 120),
            ],
        )

    def test_wrapped_path_is_joined_and_its_halves_are_not_listed(self) -> None:
        paths = [target.path for target in self.found(AGENT_SCREEN)]
        self.assertNotIn(self.root / "unrelated/side.py", paths)

    def test_same_file_is_listed_once_at_its_lowest_mention(self) -> None:
        text = "see browser.py:3\nlater view_and_edit/browser.py:9\n"
        self.assertEqual(self.found(text), [Target(self.root / "view_and_edit/browser.py", 9)])

    def test_tool_call_argument_is_a_path(self) -> None:
        self.assertEqual(
            self.found("⏺ Read(view_and_edit/browser.py)\n"),
            [Target(self.root / "view_and_edit/browser.py")],
        )

    def test_nothing_on_screen(self) -> None:
        self.assertEqual(self.found("$ ls\nno paths here, just words.\n"), [])
        self.assertEqual(self.found(""), [])

    def test_without_a_locator_only_exact_paths_count(self) -> None:
        text = "todo.txt ../notes/todo.txt"
        self.assertEqual(files_on_screen(text, self.cwd), [Target(self.root / "notes/todo.txt")])


class LooksLikePathTest(unittest.TestCase):
    def test_paths(self) -> None:
        for token in ("a.py", "src/app", "Makefile", "x.tsx:12:3", "`README.md`", "~/x"):
            with self.subTest(token=token):
                self.assertTrue(looks_like_path(token))

    def test_not_paths(self) -> None:
        for token in (
            "word",
            "0.9.1",
            "12:30",
            "https://x.com/a.py",
            "--flag",
            "a",
            "//",
            "~/",
            "../",
        ):
            with self.subTest(token=token):
                self.assertFalse(looks_like_path(token))


if __name__ == "__main__":
    unittest.main()
