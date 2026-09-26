from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from view_and_edit.target import Target, TargetError, resolve_file_url, resolve_selection

LOCAL_HOSTS = {"", "localhost", "my-mac"}


class ResolveSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "src").mkdir()
        (self.root / "src" / "app.ts").write_text("x\n")
        (self.root / "with space.md").write_text("x\n")
        (self.root / "weird:1.txt").write_text("x\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def resolve(self, text: str) -> Target:
        return resolve_selection(text, self.root)

    def test_relative_path_resolves_against_pane_cwd(self) -> None:
        self.assertEqual(self.resolve("src/app.ts"), Target(self.root / "src/app.ts"))

    def test_line_and_column_suffixes(self) -> None:
        self.assertEqual(self.resolve("src/app.ts:12"), Target(self.root / "src/app.ts", 12))
        self.assertEqual(self.resolve("src/app.ts:12:5"), Target(self.root / "src/app.ts", 12, 5))
        self.assertEqual(self.resolve("src/app.ts:12:5:"), Target(self.root / "src/app.ts", 12, 5))
        self.assertEqual(self.resolve("src/app.ts(7,2)"), Target(self.root / "src/app.ts", 7, 2))

    def test_wrapping_quotes_and_punctuation_are_ignored(self) -> None:
        expected = Target(self.root / "src/app.ts", 3)
        for text in ("`src/app.ts:3`", '"src/app.ts:3".', "(src/app.ts:3),", "[src/app.ts:3]"):
            with self.subTest(text=text):
                self.assertEqual(self.resolve(text), expected)

    def test_existing_name_with_colon_wins_over_line_suffix(self) -> None:
        self.assertEqual(self.resolve("weird:1.txt"), Target(self.root / "weird:1.txt"))

    def test_path_with_spaces_when_whole_selection_matches(self) -> None:
        self.assertEqual(self.resolve("with space.md"), Target(self.root / "with space.md"))

    def test_token_inside_a_line_is_found(self) -> None:
        self.assertEqual(self.resolve("\tmodified:   src/app.ts"), Target(self.root / "src/app.ts"))

    def test_git_diff_prefix_is_dropped_when_needed(self) -> None:
        self.assertEqual(self.resolve("b/src/app.ts"), Target(self.root / "src/app.ts"))

    def test_absolute_path_and_directory(self) -> None:
        target = self.resolve(str(self.root / "src"))
        self.assertEqual(target.path, self.root / "src")
        self.assertTrue(target.is_dir)

    def test_file_url_in_selection(self) -> None:
        url = (self.root / "with space.md").as_uri()
        self.assertEqual(self.resolve(url), Target(self.root / "with space.md"))

    def test_missing_file_reports_base_directory(self) -> None:
        with self.assertRaises(TargetError) as caught:
            self.resolve("nope/missing.ts:3")
        self.assertIn(str(self.root), str(caught.exception))

    def test_empty_selection(self) -> None:
        with self.assertRaises(TargetError):
            self.resolve("  \n ")


class ResolveFileUrlTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.file = self.root / "日本語 file.txt"
        self.file.write_text("x\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_percent_encoded_path(self) -> None:
        target = resolve_file_url(self.file.as_uri(), self.root, LOCAL_HOSTS)
        self.assertEqual(target, Target(self.file))

    def test_local_hostname_and_line_fragment(self) -> None:
        url = f"file://my-mac{self.file.as_uri()[len('file://') :]}#L42"
        self.assertEqual(resolve_file_url(url, self.root, LOCAL_HOSTS), Target(self.file, 42))

    def test_remote_host_is_rejected(self) -> None:
        with self.assertRaises(TargetError):
            resolve_file_url(f"file://other-host{self.file}", self.root, LOCAL_HOSTS)

    def test_non_file_scheme_is_rejected(self) -> None:
        with self.assertRaises(TargetError):
            resolve_file_url("https://example.com/a.txt", self.root, LOCAL_HOSTS)


if __name__ == "__main__":
    unittest.main()
