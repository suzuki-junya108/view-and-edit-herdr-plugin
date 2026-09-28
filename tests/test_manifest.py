from __future__ import annotations

import re
import unittest
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent.parent / "herdr-plugin.toml"


def _pane_block(pane_id: str) -> str:
    # Python 3.9 has no tomllib, so the [[panes]] table is read as text.
    for block in MANIFEST.read_text(encoding="utf-8").split("[[panes]]")[1:]:
        table = block.split("\n[")[0]
        if re.search(rf'^id = "{pane_id}"$', table, re.MULTILINE):
            return table
    raise AssertionError(f"no pane {pane_id!r} in {MANIFEST}")


class ManifestTest(unittest.TestCase):
    def test_popup_uses_the_whole_workspace_area(self) -> None:
        # Previews, tables and the search list need the width; herdr already keeps
        # its sidebar and tab bar visible around the popup.
        table = _pane_block("app")
        self.assertIn('placement = "popup"', table)
        self.assertIn('width = "100%"', table)
        self.assertIn('height = "100%"', table)


if __name__ == "__main__":
    unittest.main()
