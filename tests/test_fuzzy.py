from __future__ import annotations

import unittest

from view_and_edit.fuzzy import fuzzy_score, rank

PATHS = [
    "README.md",
    "src/agent.py",
    "src/app/agent_test.py",
    "src/app.py",
    "docs/guide.md",
    "tests/test_app.py",
    "vendor/a/g/e/n/t/other.py",
]


class FuzzyScoreTest(unittest.TestCase):
    def test_characters_must_appear_in_order(self) -> None:
        self.assertIsNotNone(fuzzy_score("agt", "src/agent.py"))
        self.assertIsNone(fuzzy_score("tga", "src/agent.py"))
        self.assertIsNone(fuzzy_score("xyz", "src/agent.py"))

    def test_case_and_spaces_are_ignored(self) -> None:
        self.assertIsNotNone(fuzzy_score("READ me", "README.md"))
        self.assertIsNotNone(fuzzy_score("app test", "src/app/agent_test.py"))

    def test_empty_query_matches_nothing(self) -> None:
        self.assertIsNone(fuzzy_score("  ", "README.md"))

    def test_query_may_span_folders(self) -> None:
        self.assertIsNotNone(fuzzy_score("srcag", "src/agent.py"))
        self.assertIsNotNone(fuzzy_score("src/ag", "src/agent.py"))
        self.assertIsNone(fuzzy_score("docs/ag", "src/agent.py"))


class RankTest(unittest.TestCase):
    def test_file_name_matches_beat_matches_spread_over_folders(self) -> None:
        ranked, total = rank("agent", PATHS)
        self.assertEqual(ranked[:2], ["src/agent.py", "src/app/agent_test.py"])
        self.assertEqual(ranked[-1], "vendor/a/g/e/n/t/other.py")
        self.assertEqual(total, 3)

    def test_prefix_and_consecutive_letters_come_first(self) -> None:
        ranked, _ = rank("app", PATHS)
        self.assertEqual(ranked[0], "src/app.py")
        self.assertLess(ranked.index("src/app.py"), ranked.index("tests/test_app.py"))

    def test_shorter_path_breaks_ties(self) -> None:
        ranked, _ = rank("x", ["deep/folder/x.txt", "x.txt"])
        self.assertEqual(ranked, ["x.txt", "deep/folder/x.txt"])

    def test_limit_keeps_the_total(self) -> None:
        paths = [f"file{i}.txt" for i in range(50)]
        ranked, total = rank("file", paths, limit=10)
        self.assertEqual(len(ranked), 10)
        self.assertEqual(total, 50)

    def test_empty_query_lists_the_top_of_the_project_first(self) -> None:
        ranked, total = rank("", PATHS)
        self.assertEqual(ranked[:2], ["README.md", "docs/guide.md"])
        self.assertEqual(total, len(PATHS))


if __name__ == "__main__":
    unittest.main()
