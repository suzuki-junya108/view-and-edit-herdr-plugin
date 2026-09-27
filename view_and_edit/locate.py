"""Find files that a terminal mentions by bare name or partial path.

Agents and tools print `REQUIREMENTS.md` or `browser.py:120` relative to wherever they
happen to be, which is rarely the pane's cwd. An index of the project lets those
mentions resolve by matching the end of each known path.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

# Folders that hold generated or vendored files: walking them is slow and a match
# inside them is almost never the file the user meant.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".tox",
        ".next",
        ".nuxt",
        ".cache",
        ".Trash",
        "DerivedData",
        "SourcePackages",
        "Pods",
        ".build",
        "build",
        "dist",
        "target",
    }
)
# Outside git there is no file list to ask for, so the folder is walked breadth-first
# (shallow files first) and the walk stops here to keep the popup responsive.
WALK_BUDGET_SECONDS = 1.0
GIT_TIMEOUT_SECONDS = 5.0


def git_toplevel(directory: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    top = result.stdout.strip()
    return Path(top) if result.returncode == 0 and top else None


def _git_files(top: Path) -> list[str] | None:
    """Tracked plus untracked-but-not-ignored files, relative to the repository root."""
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(top),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    names = result.stdout.decode("utf-8", "surrogateescape").split("\0")
    return list(dict.fromkeys(name for name in names if name))


def _walk_files(root: Path, budget: float, clock: Callable[[], float]) -> tuple[list[str], bool]:
    """Files and folders under `root`, shallow first; False when the time budget ran out."""
    deadline = clock() + budget
    files: list[str] = []
    pending: deque[tuple[Path, str]] = deque([(root, "")])
    while pending:
        if clock() > deadline:
            return files, False
        directory, prefix = pending.popleft()
        try:
            with os.scandir(directory) as scan:
                items = sorted(scan, key=lambda item: item.name)
        except OSError:
            continue
        for item in items:
            relative = prefix + item.name
            try:
                is_dir = item.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if item.name not in SKIP_DIRS:
                    files.append(relative)
                    pending.append((Path(item.path), relative + "/"))
            else:
                files.append(relative)
    return files, True


def _with_parent_dirs(files: Iterable[str]) -> list[str]:
    """Add each file's folders, so `src/components` can be matched too."""
    seen: dict[str, None] = {}
    for name in files:
        parts = name.split("/")
        for depth in range(1, len(parts)):
            seen.setdefault("/".join(parts[:depth]), None)
        seen.setdefault(name, None)
    return list(seen)


def _clean_mention(mention: str) -> str | None:
    """The mention as a relative suffix, or None when it cannot name a project file."""
    text = mention.strip()
    while text.startswith("./"):
        text = text[2:]
    text = text.rstrip("/")
    if not text or text.startswith(("/", "~", "../")) or text == "..":
        return None
    return text


@dataclass
class FileIndex:
    root: Path
    paths: list[str]
    complete: bool
    _by_name: dict[str, list[str]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for path in self.paths:
            self._by_name.setdefault(path.rsplit("/", 1)[-1], []).append(path)

    def find(self, mention: str) -> list[Path]:
        """Existing paths whose end matches `mention`, nearest to the root first."""
        suffix = _clean_mention(mention)
        if suffix is None:
            return []
        name = suffix.rsplit("/", 1)[-1]
        matches = [
            path
            for path in self._by_name.get(name, [])
            if path == suffix or path.endswith("/" + suffix)
        ]
        matches.sort(key=lambda path: (path.count("/"), path))
        return [self.root / path for path in matches if (self.root / path).exists()]


def build_index(
    directory: Path,
    budget: float = WALK_BUDGET_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> FileIndex:
    """Index the git repository containing `directory`, else the folder itself."""
    top = git_toplevel(directory)
    if top is not None:
        files = _git_files(top)
        if files is not None:
            return FileIndex(top, _with_parent_dirs(files), complete=True)
    files, complete = _walk_files(directory, budget, clock)
    return FileIndex(directory, _with_parent_dirs(files), complete)


class Locator:
    """Resolves mentions against the indexes of a few roots, built on first use."""

    def __init__(
        self,
        roots: Iterable[Path],
        builder: Callable[[Path], FileIndex] = build_index,
    ) -> None:
        self._roots = list(dict.fromkeys(roots))
        self._builder = builder
        self._indexes: list[FileIndex] | None = None

    def _all_indexes(self) -> list[FileIndex]:
        if self._indexes is None:
            # Roots inside the same repository share one index of the whole repository.
            tops = dict.fromkeys(git_toplevel(root) or root for root in self._roots)
            self._indexes = [self._builder(top) for top in tops]
        return self._indexes

    @property
    def complete(self) -> bool:
        """False when some folder was too large to search fully."""
        return self._indexes is None or all(index.complete for index in self._indexes)

    def find(self, mention: str) -> list[Path]:
        found: dict[Path, None] = {}
        for index in self._all_indexes():
            for path in index.find(mention):
                found.setdefault(path.resolve(), None)
        return list(found)
