"""Entry points: the herdr `open` action, the popup `app` pane, and a standalone `open`."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from view_and_edit.app import App, Screen, run_app
from view_and_edit.browser import BrowserScreen
from view_and_edit.target import Target, TargetError, resolve_file_url, resolve_selection
from view_and_edit.viewer import ViewerScreen

PLUGIN_TITLE = "View and Edit"
APP_ENTRYPOINT = "app"
TARGET_ENV = "HERDR_VIEW_AND_EDIT_TARGET"
LINK_CLICK_SOURCE = "link_click"
_POPUP_BUSY = "ui_busy"


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


def target_from_context(context: Mapping[str, object], env: Mapping[str, str]) -> Target:
    """The clicked link, else the selected path, else the pane's folder (file browser)."""
    cwd = _base_dir(context, env)
    clicked_url = context.get("clicked_url")
    if context.get("invocation_source") == LINK_CLICK_SOURCE and isinstance(clicked_url, str):
        return resolve_file_url(clicked_url, cwd)
    selected = context.get("selected_text")
    if not isinstance(selected, str) or not selected.strip():
        return Target(cwd.resolve())
    return resolve_selection(selected, cwd)


def encode_target(target: Target) -> str:
    return json.dumps({"path": str(target.path), "line": target.line, "column": target.column})


def decode_target(raw: str) -> Target:
    data = json.loads(raw)
    line, column = data.get("line"), data.get("column")
    return Target(
        Path(data["path"]),
        line if isinstance(line, int) else None,
        column if isinstance(column, int) else None,
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


def run_target(target: Target) -> int:
    def first(app: App) -> Screen:
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
    try:
        target = resolve_selection(args[0], cwd) if args else Target(cwd)
    except TargetError as error:
        print(error, file=sys.stderr)
        return 1
    return run_target(target)


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
