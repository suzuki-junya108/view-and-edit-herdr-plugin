from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "herdr-plugin.toml"
READMES = (ROOT / "README.md", ROOT / "README.en.md")


def _manifest_value(key: str) -> str:
    found = re.search(rf'^{key} = "([^"]+)"$', MANIFEST.read_text(encoding="utf-8"), re.MULTILINE)
    if found is None:
        raise AssertionError(f"no {key} in {MANIFEST}")
    return found.group(1)


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

    def test_readmes_state_the_same_versions_as_the_manifest(self) -> None:
        # Users read the requirement and the pinning example from the README, so a
        # stale number there sends them to a herdr or tag that does not match.
        min_herdr = _manifest_value("min_herdr_version")
        version = _manifest_value("version")
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(f'version = "{version}"', project)
        for readme in READMES:
            text = readme.read_text(encoding="utf-8")
            with self.subTest(readme=readme.name):
                self.assertIn(f"herdr](https://herdr.dev) {min_herdr} ", text)
                self.assertIn(f"--ref v{version}", text)


if __name__ == "__main__":
    unittest.main()
