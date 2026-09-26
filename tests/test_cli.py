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
printf '%s\\n' "$@" > "$FAKE_HERDR_LOG"
printf '%s\\n' -- >> "$FAKE_HERDR_LOG"
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

    def assert_opens_app_for(self, calls: list[str], expected: dict[str, object]) -> None:
        self.assertEqual(
            calls[:7],
            ["plugin", "pane", "open", "--plugin", "view-and-edit", "--entrypoint", "app"],
        )
        self.assertEqual(calls[7], "--env")
        name, _, payload = calls[8].partition("=")
        self.assertEqual(name, "HERDR_VIEW_AND_EDIT_TARGET")
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

    def test_no_selection_opens_file_browser_in_pane_cwd(self) -> None:
        code, calls = self.run_action({"focused_pane_cwd": str(self.root)})
        self.assertEqual(code, 0)
        self.assert_opens_app_for(calls, {"path": str(self.root), "line": None, "column": None})

    def test_popup_already_open_is_explained_plainly(self) -> None:
        code, calls = self.run_action(
            {"focused_pane_cwd": str(self.root), "selected_text": "src/main.rs"},
            pane_error=BUSY_ERROR,
        )
        self.assertEqual(code, 1)
        self.assertEqual(calls[:3], ["notification", "show", "View and Edit"])
        self.assertIn("すでに開いています", calls[4])
        self.assertNotIn("ui_busy", calls[4])


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
