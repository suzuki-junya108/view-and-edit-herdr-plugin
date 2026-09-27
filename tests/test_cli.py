"""Run the real launcher with a fake `herdr` binary to check what the plugin asks herdr to do."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = PLUGIN_ROOT / "bin" / "view-and-edit"

FAKE_HERDR = """#!/bin/sh
printf '%s\\n' "$@" >> "$FAKE_HERDR_LOG"
printf '%s\\n' -- >> "$FAKE_HERDR_LOG"
if [ "$1" = pane ] && [ "$2" = read ]; then
  cat "$FAKE_PANE_TEXT"
  exit 0
fi
if [ "$1" = plugin ] && [ -n "$FAKE_HERDR_PANE_ERROR" ]; then
  printf '%s\\n' "$FAKE_HERDR_PANE_ERROR" >&2
  exit 1
fi
"""
BUSY_ERROR = '{"error":{"code":"ui_busy","message":"a popup pane is already open"}}'


class ActionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.file = self.root / "src" / "main.rs"
        self.file.parent.mkdir()
        self.file.write_text("fn main() {}\n")
        self.fake = self.root / "fake-herdr"
        self.fake.write_text(FAKE_HERDR)
        self.fake.chmod(0o755)
        self.log = self.root / "herdr.log"
        self.pane_text = self.root / "pane.txt"
        self.pane_text.write_text("")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_action(self, context: dict[str, object], pane_error: str = "") -> tuple[int, list[str]]:
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.root),
            "HERDR_BIN_PATH": str(self.fake),
            "HERDR_PLUGIN_ID": "view-and-edit",
            "HERDR_PLUGIN_CONTEXT_JSON": json.dumps(context),
            "FAKE_HERDR_LOG": str(self.log),
            "FAKE_HERDR_PANE_ERROR": pane_error,
            "FAKE_PANE_TEXT": str(self.pane_text),
        }
        result = subprocess.run(
            [sys.executable, str(LAUNCHER), "action"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        return result.returncode, calls

    @staticmethod
    def invocations(calls: list[str]) -> list[list[str]]:
        """The fake herdr's log split into one argv per call."""
        runs: list[list[str]] = [[]]
        for arg in calls:
            if arg == "--":
                runs.append([])
            else:
                runs[-1].append(arg)
        return [run for run in runs if run]

    def payload(self, calls: list[str]) -> dict[str, object]:
        opens = [run for run in self.invocations(calls) if run[:3] == ["plugin", "pane", "open"]]
        self.assertEqual(len(opens), 1)
        self.assert_opens_app_for(opens[0], None)
        data = json.loads(opens[0][8].partition("=")[2])
        self.assertIsInstance(data, dict)
        return dict(data)

    def assert_opens_app_for(self, calls: list[str], expected: dict[str, object] | None) -> None:
        self.assertEqual(
            calls[:7],
            ["plugin", "pane", "open", "--plugin", "view-and-edit", "--entrypoint", "app"],
        )
        self.assertEqual(calls[7], "--env")
        name, _, payload = calls[8].partition("=")
        self.assertEqual(name, "HERDR_VIEW_AND_EDIT_TARGET")
        if expected is not None:
            self.assertEqual(json.loads(payload), expected)

    def test_selected_text_opens_menu_with_resolved_path(self) -> None:
        code, calls = self.run_action(
            {"focused_pane_cwd": str(self.root), "selected_text": "src/main.rs:1:4"}
        )
        self.assertEqual(code, 0)
        self.assert_opens_app_for(calls, {"path": str(self.file), "line": 1, "column": 4})

    def test_link_click_uses_clicked_url(self) -> None:
        code, calls = self.run_action(
            {
                "invocation_source": "link_click",
                "clicked_url": self.file.as_uri(),
                "selected_text": "ignored",
            }
        )
        self.assertEqual(code, 0)
        self.assert_opens_app_for(calls, {"path": str(self.file), "line": None, "column": None})

    def test_missing_path_shows_notification_instead_of_menu(self) -> None:
        code, calls = self.run_action(
            {"focused_pane_cwd": str(self.root), "selected_text": "nope.txt"}
        )
        self.assertEqual(code, 1)
        self.assertEqual(calls[:3], ["notification", "show", "View and Edit"])

    def test_no_selection_and_no_paths_on_screen_opens_file_browser(self) -> None:
        self.pane_text.write_text("$ echo hello\nhello\n")
        code, calls = self.run_action(
            {"focused_pane_cwd": str(self.root), "focused_pane_id": "w1:p1"}
        )
        self.assertEqual(code, 0)
        runs = self.invocations(calls)
        self.assertEqual(runs[0], ["pane", "read", "w1:p1", "--source", "visible"])
        self.assertEqual(
            self.payload(calls), {"path": str(self.root), "line": None, "column": None}
        )

    def test_no_selection_lists_the_files_on_the_screen(self) -> None:
        (self.root / "README.md").write_text("x\n")
        self.pane_text.write_text("edited src/main.rs:3\nsee main.rs and README.md\n")
        code, calls = self.run_action(
            {"focused_pane_cwd": str(self.root), "focused_pane_id": "w1:p1"}
        )
        self.assertEqual(code, 0)
        # Lowest on the screen first; `main.rs` is found under src/ by name.
        self.assertEqual(
            self.payload(calls),
            {
                "choices": [
                    {"path": str(self.root / "README.md"), "line": None, "column": None},
                    {"path": str(self.file), "line": None, "column": None},
                ],
                "base": str(self.root),
                "heading": "画面に出ているファイル",
                "note": None,
            },
        )

    def test_selected_bare_name_is_found_elsewhere_in_the_project(self) -> None:
        code, calls = self.run_action(
            {"focused_pane_cwd": str(self.root), "selected_text": "main.rs:7"}
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.payload(calls), {"path": str(self.file), "line": 7, "column": None})

    def test_ambiguous_selection_lists_every_match(self) -> None:
        other = self.root / "lib" / "main.rs"
        other.parent.mkdir()
        other.write_text("x\n")
        code, calls = self.run_action(
            {"focused_pane_cwd": str(self.root), "selected_text": "main.rs"}
        )
        self.assertEqual(code, 0)
        data = self.payload(calls)
        choices = data["choices"]
        assert isinstance(choices, list)
        self.assertEqual([choice["path"] for choice in choices], [str(other), str(self.file)])
        self.assertEqual(data["heading"], "「main.rs」に一致するファイル")

    def test_popup_already_open_is_explained_plainly(self) -> None:
        code, calls = self.run_action(
            {"focused_pane_cwd": str(self.root), "selected_text": "src/main.rs"},
            pane_error=BUSY_ERROR,
        )
        self.assertEqual(code, 1)
        notification = self.invocations(calls)[-1]
        self.assertEqual(notification[:3], ["notification", "show", "View and Edit"])
        self.assertIn("すでに開いています", notification[4])
        self.assertNotIn("ui_busy", notification[4])


class PaneTest(unittest.TestCase):
    def test_app_without_target_fails_cleanly(self) -> None:
        result = subprocess.run(
            [sys.executable, str(LAUNCHER), "app"],
            env={"PATH": os.environ["PATH"]},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("HERDR_VIEW_AND_EDIT_TARGET", result.stderr)

    def test_unknown_subcommand_prints_usage(self) -> None:
        result = subprocess.run(
            [sys.executable, str(LAUNCHER), "bogus"], capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage", result.stderr)


if __name__ == "__main__":
    unittest.main()
