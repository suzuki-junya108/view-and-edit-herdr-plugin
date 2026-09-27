from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from view_and_edit.locate import FileIndex, Locator, build_index


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


class RepositoryIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        for name in (
            "REQUIREMENTS.md",
            "docs/REQUIREMENTS.md",
            "view_and_edit/browser.py",
            "view_and_edit/deep/browser.py",
            "notes/new-untracked.txt",
            "out/generated.log",
        ):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x\n")
        (self.root / ".gitignore").write_text("out/\n")
        git(self.root, "init", "-q")
        git(self.root, "add", "REQUIREMENTS.md", "docs", "view_and_edit", ".gitignore")
        self.index = build_index(self.root / "view_and_edit")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_indexes_the_whole_repository_from_a_subfolder(self) -> None:
        self.assertEqual(self.index.root, self.root)
        self.assertTrue(self.index.complete)

    def test_bare_name_matches_nearest_to_the_root_first(self) -> None:
        self.assertEqual(
            self.index.find("REQUIREMENTS.md"),
            [self.root / "REQUIREMENTS.md", self.root / "docs/REQUIREMENTS.md"],
        )

    def test_partial_path_matches_only_whole_path_segments(self) -> None:
        self.assertEqual(
            self.index.find("deep/browser.py"), [self.root / "view_and_edit/deep/browser.py"]
        )
        self.assertEqual(self.index.find("ep/browser.py"), [])

    def test_untracked_files_are_found_but_ignored_ones_are_not(self) -> None:
        self.assertEqual(
            self.index.find("new-untracked.txt"), [self.root / "notes/new-untracked.txt"]
        )
        self.assertEqual(self.index.find("generated.log"), [])

    def test_folders_and_dot_slash_prefix(self) -> None:
        self.assertEqual(self.index.find("view_and_edit/deep/"), [self.root / "view_and_edit/deep"])
        self.assertEqual(
            self.index.find("./docs/REQUIREMENTS.md"), [self.root / "docs/REQUIREMENTS.md"]
        )

    def test_absolute_home_and_parent_paths_are_not_suffixes(self) -> None:
        for mention in ("/REQUIREMENTS.md", "~/REQUIREMENTS.md", "../REQUIREMENTS.md", ".."):
            with self.subTest(mention=mention):
                self.assertEqual(self.index.find(mention), [])

    def test_deleted_file_is_not_offered(self) -> None:
        (self.root / "docs/REQUIREMENTS.md").unlink()
        self.assertEqual(self.index.find("REQUIREMENTS.md"), [self.root / "REQUIREMENTS.md"])


class FolderIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        for name in ("memo/@info.md", "node_modules/pkg/@info.md", "a/b/c/deep.txt"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_walks_a_folder_outside_git_skipping_vendored_folders(self) -> None:
        index = build_index(self.root)
        self.assertTrue(index.complete)
        self.assertEqual(index.find("@info.md"), [self.root / "memo/@info.md"])
        self.assertEqual(index.find("deep.txt"), [self.root / "a/b/c/deep.txt"])

    def test_walk_stops_at_the_time_budget_and_says_so(self) -> None:
        ticks = iter(range(100))
        index = build_index(self.root, budget=1.5, clock=lambda: float(next(ticks)))
        self.assertFalse(index.complete)
        # Shallow folders come first, so the top-level entries made it in.
        self.assertIn("memo", index.paths)
        self.assertEqual(index.find("deep.txt"), [])


class LocatorTest(unittest.TestCase):
    def test_indexes_are_built_once_and_only_when_needed(self) -> None:
        built: list[Path] = []

        def builder(root: Path) -> FileIndex:
            built.append(root)
            return FileIndex(root, [], complete=False)

        locator = Locator([Path("/nowhere-a"), Path("/nowhere-b")], builder)
        self.assertTrue(locator.complete)
        self.assertEqual(built, [])
        locator.find("x.txt")
        locator.find("y.txt")
        self.assertEqual(built, [Path("/nowhere-a"), Path("/nowhere-b")])
        self.assertFalse(locator.complete)


if __name__ == "__main__":
    unittest.main()
