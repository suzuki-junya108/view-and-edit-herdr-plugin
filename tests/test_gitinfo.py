from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path

from view_and_edit.gitinfo import (
    ADDED,
    CHANGED_INSIDE,
    CONFLICT,
    DELETED,
    MODIFIED,
    RENAMED,
    UNTRACKED,
    diff_lines,
    format_diff,
    git_root,
    mark_for_code,
    read_status,
)
from view_and_edit.ui import Line


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def assert_index_untouched(case: RepoTestCase, path: Path, run: Callable[[Path], object]) -> None:
    """Make `path`'s stat stale (same content, new mtime), run, and check the index."""
    index = case.root / ".git" / "index"
    stamp = path.stat().st_mtime + 100
    os.utime(path, (stamp, stamp))
    before = (index.read_bytes(), index.stat().st_mtime_ns)
    run(case.root)
    case.assertEqual((index.read_bytes(), index.stat().st_mtime_ns), before)
    # The same step without the guard does write it, so the check above can fail.
    subprocess.run(["git", "status"], cwd=case.root, check=True, capture_output=True)
    case.assertNotEqual(index.stat().st_mtime_ns, before[1])


class RepoTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        git(self.root, "init", "-q")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def commit(self) -> None:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "c")

    def text(self, lines: list[Line]) -> list[str]:
        return [line.text for line in lines]


class StatusTest(RepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write("src/app.py", "a\nb\n")
        self.write("src/old name.py", "x\n")
        self.write("gone.txt", "bye\n")
        self.write("keep/same.txt", "same\n")
        self.write(".gitignore", "ignored/\n")
        self.commit()

    def test_marks_each_kind_of_change(self) -> None:
        self.write("src/app.py", "a\nB\n")
        self.write("src/staged.py", "s\n")
        git(self.root, "add", "src/staged.py")
        git(self.root, "mv", "src/old name.py", "src/新しい名前.py")
        (self.root / "gone.txt").unlink()
        self.write("notes.md", "n\n")
        self.write("fresh/deep/one.txt", "1\n")
        self.write("ignored/x.log", "x\n")
        status = read_status(self.root / "src")
        assert status is not None
        self.assertEqual(status.root, self.root)
        self.assertIs(status.mark_for(self.root / "src/app.py", False), MODIFIED)
        self.assertIs(status.mark_for(self.root / "src/staged.py", False), ADDED)
        self.assertIs(status.mark_for(self.root / "src/新しい名前.py", False), RENAMED)
        self.assertIs(status.mark_for(self.root / "gone.txt", False), DELETED)
        self.assertIs(status.mark_for(self.root / "notes.md", False), UNTRACKED)
        # Everything under a new folder is new, however deep.
        self.assertIs(status.mark_for(self.root / "fresh", True), UNTRACKED)
        self.assertIs(status.mark_for(self.root / "fresh/deep/one.txt", False), UNTRACKED)
        self.assertIs(status.mark_for(self.root / "src", True), CHANGED_INSIDE)
        self.assertIsNone(status.mark_for(self.root / "keep", True))
        self.assertIsNone(status.mark_for(self.root / "keep/same.txt", False))
        self.assertIsNone(status.mark_for(self.root / "ignored/x.log", False))
        self.assertIsNone(status.mark_for(self.root, True))
        self.assertEqual(status.count, 6)

    def test_paths_reached_through_a_symlink_still_match(self) -> None:
        self.write("src/app.py", "changed\n")
        with tempfile.TemporaryDirectory() as other:
            link = Path(other) / "link"
            link.symlink_to(self.root)
            status = read_status(link)
            assert status is not None
            self.assertIs(status.mark_for(link / "src/app.py", False), MODIFIED)
            self.assertIs(status.mark_for(self.root / "src/app.py", False), MODIFIED)

    def test_outside_git_there_is_no_status(self) -> None:
        with tempfile.TemporaryDirectory() as plain:
            self.assertIsNone(read_status(Path(plain)))
            self.assertIsNone(git_root(Path(plain)))

    def test_status_leaves_the_index_alone(self) -> None:
        # Plain `git status` rewrites .git/index (under index.lock) when file stats are
        # stale; an agent committing at that moment would fail on the lock.
        assert_index_untouched(self, self.root / "src/app.py", read_status)

    def test_repository_fsmonitor_hook_is_not_run(self) -> None:
        marker = self.root / "hook-ran"
        hook = self.root / "hook.sh"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        hook.chmod(0o755)
        git(self.root, "config", "core.fsmonitor", str(hook))
        self.write("src/app.py", "changed\n")
        self.assertIsNotNone(read_status(self.root))
        diff_lines(self.root / "src/app.py")
        self.assertFalse(marker.exists())

    def test_repository_filter_commands_are_not_run(self) -> None:
        marker = self.root / "filter-ran"
        hook = self.root / "filter.sh"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat\n")
        hook.chmod(0o755)
        self.write(".gitattributes", "*.py filter=evil\n*.md filter=proc\n")
        self.write("notes.md", "n\n")
        self.commit()
        git(self.root, "config", "filter.evil.clean", str(hook))
        git(self.root, "config", "filter.evil.required", "true")
        git(self.root, "config", "filter.proc.process", str(hook))
        self.write("src/app.py", "changed\n")
        self.write("notes.md", "changed\n")
        status = read_status(self.root)
        assert status is not None
        self.assertIs(status.mark_for(self.root / "src/app.py", False), MODIFIED)
        self.assertIs(status.mark_for(self.root / "notes.md", False), MODIFIED)
        self.assertIn("+ changed", self.text(diff_lines(self.root / "src/app.py"))[-1])
        diff_lines(self.root / "notes.md")
        self.assertFalse(marker.exists())

    def test_status_codes(self) -> None:
        self.assertIs(mark_for_code(" M"), MODIFIED)
        self.assertIs(mark_for_code("MM"), MODIFIED)
        self.assertIs(mark_for_code("AM"), ADDED)
        self.assertIs(mark_for_code("UU"), CONFLICT)
        self.assertIs(mark_for_code("AA"), CONFLICT)
        self.assertIs(mark_for_code(" D"), DELETED)
        self.assertIs(mark_for_code("RM"), RENAMED)


class DiffTest(RepoTestCase):
    def test_changed_lines_are_numbered_and_coloured(self) -> None:
        path = self.write("app.py", "".join(f"line{i}\n" for i in range(1, 21)))
        self.commit()
        path.write_text(
            "".join(f"line{i}\n" for i in range(1, 21)).replace("line10\n", "TEN\n"),
            encoding="utf-8",
        )
        lines = diff_lines(path)
        text = self.text(lines)
        self.assertEqual(text[0], "+1 -1  最後のコミットからの変更")
        self.assertIn("──── 7 行目から ────", text)
        self.assertIn("   10       - line10", text)
        self.assertIn("         10 + TEN", text)
        self.assertIn("    9     9   line9", text)
        added = next(line for line in lines if line.text.endswith("+ TEN"))
        self.assertEqual(added.spans[-1].style, "32")

    def test_unchanged_file_says_so(self) -> None:
        path = self.write("a.txt", "a\n")
        self.commit()
        self.assertEqual(self.text(diff_lines(path)), ["最後のコミットから変更はありません"])

    def test_new_file_is_all_added(self) -> None:
        self.write("a.txt", "a\n")
        self.commit()
        path = self.write("new dir/新規.txt", "one\ntwo\n")
        text = self.text(diff_lines(path))
        self.assertEqual(text[0], "+2 -0  新しいファイル（まだ Git に登録されていません）")
        self.assertIn("          1 + one", text)

    def test_repository_without_commits(self) -> None:
        path = self.write("a.txt", "a\n")
        git(self.root, "add", "a.txt")
        self.assertEqual(self.text(diff_lines(path))[0], "+1 -0  最後のコミットからの変更")

    def test_ignored_and_outside_git(self) -> None:
        self.write(".gitignore", "*.log\n")
        self.commit()
        self.assertIn("無視", self.text(diff_lines(self.write("x.log", "x\n")))[0])
        with tempfile.TemporaryDirectory() as plain:
            loose = Path(plain) / "a.txt"
            loose.write_text("a\n")
            self.assertEqual(self.text(diff_lines(loose)), ["Git の管理外のファイルです"])

    def test_staged_and_unstaged_changes_both_count(self) -> None:
        path = self.write("a.txt", "1\n2\n")
        self.commit()
        path.write_text("1 staged\n2\n")
        git(self.root, "add", "a.txt")
        path.write_text("1 staged\n2 unstaged\n")
        self.assertEqual(self.text(diff_lines(path))[0], "+2 -2  最後のコミットからの変更")

    def test_shift_jis_file_is_readable(self) -> None:
        path = self.root / "sjis.txt"
        path.write_bytes("最初\n".encode("cp932"))
        self.commit()
        path.write_bytes("最初\n追加\n".encode("cp932"))
        self.assertIn("          2 + 追加", self.text(diff_lines(path)))

    def test_binary_and_missing_final_newline(self) -> None:
        text = self.text(
            format_diff(
                "diff --git a/x b/x\n@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b\n",
                "w",
            )
        )
        self.assertIn("（最後に改行なし）", text[4])
        binary = self.text(format_diff("Binary files a/x and b/x differ\n", "w"))
        self.assertIn("バイナリファイル", binary[-1])

    def test_diff_leaves_the_index_alone(self) -> None:
        path = self.write("a.txt", "a\n")
        self.commit()
        assert_index_untouched(self, path, lambda _root: diff_lines(path))


if __name__ == "__main__":
    unittest.main()
