"""What git knows about the files being looked at: change marks for lists, and diffs.

Only reads. Agents run git in the same repositories at the same time, so every call
skips the optional index refresh (which takes `index.lock`). A folder received with
its `.git` (say, from an archive) may carry config that makes git run commands while
comparing files; fsmonitor hooks, clean/process filters, external diff drivers and
textconv are all switched off.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from view_and_edit.textbuf import DecodeError, decode_text
from view_and_edit.ui import Line, Style
from view_and_edit.width import expand_tabs

# `git status` refreshes file stats first; right after a checkout or a build touched
# many files that alone took ~5 s on a 50k-file repository, so allow for it.
STATUS_TIMEOUT_SECONDS = 20.0
DIFF_TIMEOUT_SECONDS = 10.0
# Shared with the file index (locate.py), which lists files the same way.
GIT_COMMAND = (
    "git",
    "--no-optional-locks",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.quotePath=false",
)
# git's well-known id of the empty tree: what a repository without commits is compared to.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
_BINARY_NOTE = "バイナリファイルの変更です（中身の差分は表示できません）"
_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass(frozen=True)
class Mark:
    letter: str
    style: str
    label: str


MODIFIED = Mark("M", "33", "変更")
ADDED = Mark("A", "32", "追加")
RENAMED = Mark("R", "36", "名前変更")
DELETED = Mark("D", "31", "削除")
UNTRACKED = Mark("?", "32", "新規（Git 未登録）")
CONFLICT = Mark("U", "1;31", "衝突")
CHANGED_INSIDE = Mark("•", "33", "中に変更あり")

_CONFLICT_CODES = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})


def _run(command: list[str], timeout: float) -> subprocess.CompletedProcess[bytes] | None:
    try:
        return subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _filter_overrides(cwd: Path) -> list[str]:
    """`-c` options that blank every filter command the repository's config defines.

    status and diff pass changed files through the clean filter named in
    .gitattributes, running whatever command the config gives it. There is no switch
    to turn filters off, so each configured one is overridden by name. Listing config
    runs nothing.
    """
    listed = _run(
        [*GIT_COMMAND, "-C", str(cwd), "config", "--name-only", "--get-regexp", r"^filter\."],
        DIFF_TIMEOUT_SECONDS,
    )
    if listed is None:
        return []
    overrides: list[str] = []
    for name in listed.stdout.decode("utf-8", "replace").split():
        if name.endswith((".clean", ".smudge", ".process")):
            overrides += ["-c", f"{name}="]
        elif name.endswith(".required"):
            overrides += ["-c", f"{name}=false"]  # a blanked required filter would fail
    return overrides


def _git(args: list[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess[bytes] | None:
    return _run([*GIT_COMMAND, *_filter_overrides(cwd), "-C", str(cwd), *args], timeout)


def git_root(path: Path) -> Path | None:
    """The top of the work tree holding `path` (a file or folder), found without running git.

    Checked on every file the viewer opens, so it only looks for a `.git` entry upwards.
    """
    current = path if path.is_dir() else path.parent
    for folder in (current, *current.parents):
        if (folder / ".git").exists():
            return folder
    return None


def mark_for_code(code: str) -> Mark:
    """The mark for a porcelain v1 status code such as ` M`, `A `, `R ` or `??`."""
    if code == "??":
        return UNTRACKED
    if code in _CONFLICT_CODES:
        return CONFLICT
    if "R" in code:
        return RENAMED
    if code[0] == "A":
        return ADDED
    if "D" in code:
        return DELETED
    return MODIFIED


@dataclass
class GitStatus:
    root: Path
    files: dict[str, Mark]  # root-relative paths with "/"
    untracked_dirs: tuple[str, ...] = ()  # "dir/" entries: everything inside is new
    _changed_dirs: set[str] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        for path in [*self.files, *self.untracked_dirs]:
            parts = path.rstrip("/").split("/")
            for depth in range(1, len(parts)):
                self._changed_dirs.add("/".join(parts[:depth]))

    @property
    def count(self) -> int:
        return len(self.files) + len(self.untracked_dirs)

    def _relative(self, path: Path) -> str | None:
        # Lists hold paths as the user reached them (say /tmp/...), which may differ
        # from the root's spelling (/private/tmp/...) only through symlinks.
        for candidate, root in ((path, self.root), (path.resolve(), self.root.resolve())):
            relative = os.path.relpath(candidate, root)
            if relative != ".." and not relative.startswith(".." + os.sep):
                return relative.replace(os.sep, "/")
        return None

    def mark_for(self, path: Path, is_dir: bool) -> Mark | None:
        relative = self._relative(path)
        if relative is None or relative == ".":
            return None
        if relative in self.files:
            return self.files[relative]
        if any((relative + "/").startswith(folder) for folder in self.untracked_dirs):
            return UNTRACKED
        if is_dir and relative in self._changed_dirs:
            return CHANGED_INSIDE
        return None


def parse_status(root: Path, output: bytes) -> GitStatus:
    """Parse `git status --porcelain=v1 -z` output."""
    fields = output.decode("utf-8", "surrogateescape").split("\0")
    files: dict[str, Mark] = {}
    untracked_dirs: list[str] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if "R" in code or "C" in code:
            index += 1  # the next field is the name it was renamed or copied from
        if code == "??" and path.endswith("/"):
            untracked_dirs.append(path)
        elif code != "!!":
            files[path] = mark_for_code(code)
    return GitStatus(root, files, tuple(untracked_dirs))


def read_status(directory: Path) -> GitStatus | None:
    """Changes in the repository holding `directory`, or None outside git (or on failure)."""
    root = git_root(directory)
    if root is None:
        return None
    result = _git(
        ["status", "--porcelain=v1", "-z", "--untracked-files=normal"],
        root,
        STATUS_TIMEOUT_SECONDS,
    )
    if result is None or result.returncode != 0:
        return None
    return parse_status(root, result.stdout)


# ---- diffs ------------------------------------------------------------------


def _decode(data: bytes) -> str:
    try:
        text, _ = decode_text(data)
    except DecodeError:
        return data.decode("utf-8", "replace")
    return text


def _has_commits(root: Path) -> bool:
    result = _git(["rev-parse", "--verify", "--quiet", "HEAD"], root, DIFF_TIMEOUT_SECONDS)
    return result is not None and result.returncode == 0


def _diff_output(path: Path, root: Path) -> tuple[bytes, str] | str:
    """The raw diff against the last commit and how it was taken, or a message instead."""
    relative = os.path.relpath(path.resolve(), root.resolve())
    status = _git(
        ["status", "--porcelain=v1", "-z", "--ignored", "--", relative],
        root,
        DIFF_TIMEOUT_SECONDS,
    )
    if status is None or status.returncode != 0:
        return "Git の情報を読み取れませんでした"
    code = status.stdout[:2].decode("ascii", "replace")
    if code == "!!":
        return "Git で無視されているファイルです（.gitignore）"
    options = ["--no-color", "--no-ext-diff", "--no-textconv"]
    if code == "??":
        # Not in git yet: the whole file is new. --no-index exits 1 when files differ.
        result = _git(
            ["diff", *options, "--no-index", "--", os.devnull, relative], root, DIFF_TIMEOUT_SECONDS
        )
        if result is None or result.returncode not in (0, 1):
            return "差分を作れませんでした"
        return result.stdout, "新しいファイル（まだ Git に登録されていません）"
    base = "HEAD" if _has_commits(root) else _EMPTY_TREE
    # The plumbing diff-index, not `git diff`: the porcelain rewrites .git/index when
    # file stats are stale, even with --no-optional-locks.
    result = _git(
        ["diff-index", "-p", "--no-ext-diff", "--no-textconv", base, "--", relative],
        root,
        DIFF_TIMEOUT_SECONDS,
    )
    if result is None or result.returncode != 0:
        return "差分を作れませんでした"
    return result.stdout, "最後のコミットからの変更"


def diff_lines(path: Path) -> list[Line]:
    """The file's changes since the last commit, as numbered, coloured lines."""
    root = git_root(path)
    if root is None:
        return [Line.of("Git の管理外のファイルです", Style.DIM)]
    taken = _diff_output(path, root)
    if isinstance(taken, str):
        return [Line.of(taken, Style.DIM)]
    output, what = taken
    return format_diff(_decode(output), what)


def format_diff(diff: str, what: str) -> list[Line]:
    """Render a unified diff of one file: hunk separators and old/new line numbers."""
    body: list[Line] = []
    added = removed = 0
    old = new = 0
    in_hunk = False
    for raw in diff.split("\n"):
        hunk = _HUNK.match(raw)
        if hunk:
            old, new = int(hunk.group(1)), int(hunk.group(2))
            in_hunk = True
            body.append(Line.of(f"──── {new} 行目から ────", Style.ACCENT))
            continue
        if raw.startswith("Binary files"):
            body.append(
                Line.of("バイナリファイルの変更です（中身の差分は表示できません）", Style.DIM)
            )
            continue
        if not in_hunk or not raw:
            continue  # the diff --git / index / --- / +++ header lines
        sign, text = raw[0], expand_tabs(raw[1:])
        if sign == "+":
            body.append(_numbered("", str(new), "+ " + text, "32"))
            new += 1
            added += 1
        elif sign == "-":
            body.append(_numbered(str(old), "", "- " + text, "31"))
            old += 1
            removed += 1
        elif sign == "\\":
            body.append(Line.of("            （最後に改行なし）", Style.DIM))
        else:
            body.append(_numbered(str(old), str(new), "  " + text, ""))
            old += 1
            new += 1
    if not body:
        return [Line.of("最後のコミットから変更はありません", Style.DIM)]
    summary = Line().add(f"+{added}", "32").add(" ").add(f"-{removed}", "31")
    summary.add(f"  {what}", Style.DIM)
    return [summary, Line(), *body]


def _numbered(old: str, new: str, text: str, style: str) -> Line:
    return Line().add(f"{old:>5} {new:>5} ", Style.DIM).add(text, style)
