"""Entry points: the herdr `open` action, the popup `app` pane, and a standalone `open`."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Union

from view_and_edit.app import App, Screen, run_app
from view_and_edit.browser import BrowserScreen, PickerScreen
from view_and_edit.locate import Locator
from view_and_edit.screen_paths import files_on_screen
from view_and_edit.target import Target, TargetError, resolve_all, resolve_file_url
from view_and_edit.viewer import ViewerScreen

PLUGIN_TITLE = "View and Edit"
APP_ENTRYPOINT = "app"
TARGET_ENV = "HERDR_VIEW_AND_EDIT_TARGET"
LINK_CLICK_SOURCE = "link_click"
_POPUP_BUSY = "ui_busy"
SCREEN_HEADING = "画面に出ているファイル"
PARTIAL_SEARCH_NOTE = "大きいフォルダは一部だけ探しました。見つからないものがあるかもしれません"
PANE_READ_TIMEOUT_SECONDS = 5.0
_MAX_HEADING_CHARS = 40


@dataclass(frozen=True)
class Choices:
    """Several files to pick from, listed in the popup instead of opening one directly."""

    targets: list[Target]
    base: Path
    heading: str
    note: str | None = None


Launch = Union[Target, Choices]


def _herdr_bin(env: Mapping[str, str]) -> str:
    return env.get("HERDR_BIN_PATH") or "herdr"


def _notify(env: Mapping[str, str], message: str) -> None:
    print(message, file=sys.stderr)
    subprocess.run(
        [_herdr_bin(env), "notification", "show", PLUGIN_TITLE, "--body", message],
        check=False,
        stdout=subprocess.DEVNULL,
    )


def _pane_open_error(stderr: str) -> str:
    # herdr allows one popup at a time; clicking another path while one is open is common.
    if _POPUP_BUSY in stderr:
        return "View and Edit はすでに開いています。閉じてからもう一度開いてください"
    return f"ポップアップを開けませんでした: {stderr.strip()}"


def _base_dir(context: Mapping[str, object], env: Mapping[str, str]) -> Path:
    for key in ("focused_pane_cwd", "workspace_cwd"):
        value = context.get(key)
        if isinstance(value, str) and value:
            return Path(value)
    return Path(env.get("HOME") or "/")


def _search_roots(context: Mapping[str, object], cwd: Path) -> list[Path]:
    """The pane's folder, then the workspace's checkout: agents print paths relative to it."""
    roots = [cwd]
    worktree = context.get("worktree")
    if isinstance(worktree, dict):
        checkout = worktree.get("checkout_path")
        if isinstance(checkout, str) and checkout:
            roots.append(Path(checkout))
    workspace_cwd = context.get("workspace_cwd")
    if isinstance(workspace_cwd, str) and workspace_cwd:
        roots.append(Path(workspace_cwd))
    return [root for root in roots if root.is_dir()]


def _read_screen(context: Mapping[str, object], env: Mapping[str, str]) -> str:
    """The visible text of the pane the action was invoked from; empty if unavailable."""
    pane_id = context.get("focused_pane_id") or env.get("HERDR_PANE_ID")
    if not isinstance(pane_id, str) or not pane_id:
        return ""
    try:
        result = subprocess.run(
            [_herdr_bin(env), "pane", "read", pane_id, "--source", "visible"],
            capture_output=True,
            text=True,
            timeout=PANE_READ_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout if result.returncode == 0 else ""


def _partial_note(locator: Locator) -> str | None:
    return None if locator.complete else PARTIAL_SEARCH_NOTE


def _shorten_heading(text: str) -> str:
    one_line = " ".join(text.split())
    if len(one_line) <= _MAX_HEADING_CHARS:
        return one_line
    return one_line[: _MAX_HEADING_CHARS - 1] + "…"


def _match_heading(text: str) -> str:
    return f"「{_shorten_heading(text)}」に一致するファイル"


def choose(targets: list[Target], base: Path, heading: str, locator: Locator) -> Launch:
    """Open a single match directly; list several so the user picks."""
    if len(targets) == 1:
        return targets[0]
    return Choices(targets, base, heading, _partial_note(locator))


def target_from_context(context: Mapping[str, object], env: Mapping[str, str]) -> Launch:
    """The clicked link, else the selected path, else the files on the pane's screen,
    else the pane's folder (file browser)."""
    cwd = _base_dir(context, env)
    clicked_url = context.get("clicked_url")
    if context.get("invocation_source") == LINK_CLICK_SOURCE and isinstance(clicked_url, str):
        return resolve_file_url(clicked_url, cwd)
    locator = Locator(_search_roots(context, cwd))
    selected = context.get("selected_text")
    if isinstance(selected, str) and selected.strip():
        return choose(resolve_all(selected, cwd, locator), cwd, _match_heading(selected), locator)
    on_screen = files_on_screen(_read_screen(context, env), cwd, locator)
    if on_screen:
        return Choices(on_screen, cwd.resolve(), SCREEN_HEADING, _partial_note(locator))
    return Target(cwd.resolve())


def _target_json(target: Target) -> dict[str, object]:
    return {"path": str(target.path), "line": target.line, "column": target.column}


def encode_target(launch: Launch) -> str:
    if isinstance(launch, Target):
        return json.dumps(_target_json(launch))
    return json.dumps(
        {
            "choices": [_target_json(target) for target in launch.targets],
            "base": str(launch.base),
            "heading": launch.heading,
            "note": launch.note,
        }
    )


def _target_from_json(data: Mapping[str, object]) -> Target:
    line, column = data.get("line"), data.get("column")
    return Target(
        Path(str(data["path"])),
        line if isinstance(line, int) else None,
        column if isinstance(column, int) else None,
    )


def decode_target(raw: str) -> Launch:
    data = json.loads(raw)
    choices = data.get("choices")
    if not isinstance(choices, list):
        return _target_from_json(data)
    note = data.get("note")
    return Choices(
        [_target_from_json(choice) for choice in choices],
        Path(str(data["base"])),
        str(data.get("heading") or SCREEN_HEADING),
        note if isinstance(note, str) else None,
    )


def run_action(env: Mapping[str, str]) -> int:
    try:
        context = json.loads(env.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
        target = target_from_context(context, env)
    except (TargetError, json.JSONDecodeError) as error:
        _notify(env, str(error))
        return 1
    plugin_id = env.get("HERDR_PLUGIN_ID")
    if not plugin_id:
        print("HERDR_PLUGIN_ID がありません。herdr から起動してください", file=sys.stderr)
        return 1
    result = subprocess.run(
        [
            _herdr_bin(env),
            "plugin",
            "pane",
            "open",
            "--plugin",
            plugin_id,
            "--entrypoint",
            APP_ENTRYPOINT,
            "--env",
            f"{TARGET_ENV}={encode_target(target)}",
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        _notify(env, _pane_open_error(result.stderr))
    return result.returncode


def run_target(launch: Launch) -> int:
    def first(app: App) -> Screen:
        if isinstance(launch, Choices):
            return PickerScreen(app, launch.targets, launch.base, launch.heading, launch.note)
        target = launch
        if target.path.is_dir():
            return BrowserScreen(app, target.path)
        # Open the file on top of its folder, so `q` lands in the browser.
        browser = BrowserScreen(app, target.path.parent, select=target.path)
        app.push(browser)
        return ViewerScreen(app, target.path, target.line)

    return run_app(first)


def run_pane(env: Mapping[str, str]) -> int:
    raw = env.get(TARGET_ENV)
    if not raw:
        print(f"{TARGET_ENV} がありません", file=sys.stderr)
        return 1
    return run_target(decode_target(raw))


def run_standalone(args: Sequence[str]) -> int:
    """`view-and-edit open [PATH[:LINE]]` for use outside herdr or for testing."""
    cwd = Path.cwd()
    if not args:
        return run_target(Target(cwd))
    locator = Locator([cwd])
    try:
        targets = resolve_all(args[0], cwd, locator)
    except TargetError as error:
        print(error, file=sys.stderr)
        return 1
    return run_target(choose(targets, cwd, _match_heading(args[0]), locator))


def main(argv: Sequence[str]) -> int:
    command = argv[1] if len(argv) > 1 else ""
    if command == "action":
        return run_action(os.environ)
    if command == "app":
        return run_pane(os.environ)
    if command == "open":
        return run_standalone(argv[2:])
    print(f"usage: {argv[0]} {{action|app|open [PATH]}}", file=sys.stderr)
    return 2
