"""Decode raw terminal input bytes into key, mouse, and paste events."""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass
from typing import Union

ESC = "\x1b"
_PASTE_START = "\x1b[200~"
_PASTE_END = "\x1b[201~"

# CSI parameters and final byte, e.g. `ESC [ 1 ; 5 A` or `ESC [ < 0 ; 10 ; 4 M`.
_CSI = re.compile(r"\x1b\[([<?]?)([0-9;:]*)([\x40-\x7e])")
_SS3 = re.compile(r"\x1bO([A-Za-z])")

_CSI_FINAL_KEYS = {
    "A": "up",
    "B": "down",
    "C": "right",
    "D": "left",
    "H": "home",
    "F": "end",
    "P": "f1",
    "Q": "f2",
    "R": "f3",
    "S": "f4",
    "Z": "backtab",
}
_TILDE_KEYS = {
    "1": "home",
    "2": "insert",
    "3": "delete",
    "4": "end",
    "5": "pageup",
    "6": "pagedown",
    "7": "home",
    "8": "end",
}
_SINGLE = {
    "\r": "enter",
    "\n": "enter",
    "\t": "tab",
    "\x7f": "backspace",
    "\x08": "backspace",
    "\x00": "ctrl+space",
}

# xterm modifier parameter is 1 + bitmask(shift=1, alt=2, ctrl=4).
_SHIFT_BIT = 1
_ALT_BIT = 2
_CTRL_BIT = 4


@dataclass(frozen=True)
class Key:
    """`name` is a chord like `ctrl+s`, `shift+up`, `enter`, or a printable char."""

    name: str

    @property
    def char(self) -> str | None:
        return self.name if len(self.name) == 1 else None


@dataclass(frozen=True)
class Mouse:
    kind: str  # press, release, drag, wheel_up, wheel_down
    x: int  # 0-based column
    y: int  # 0-based row
    button: int = 0
    shift: bool = False


@dataclass(frozen=True)
class Paste:
    text: str


Event = Union[Key, Mouse, Paste]  # typing.Union: `|` between classes needs Python 3.10


def _with_modifiers(base: str, param: str) -> str:
    try:
        bits = int(param) - 1
    except ValueError:
        return base
    prefix = ""
    if bits & _CTRL_BIT:
        prefix += "ctrl+"
    if bits & _ALT_BIT:
        prefix += "alt+"
    if bits & _SHIFT_BIT:
        prefix += "shift+"
    return prefix + base


def _mouse(params: str, final: str) -> Mouse | None:
    parts = params.split(";")
    if len(parts) != 3:
        return None
    try:
        code, x, y = (int(p) for p in parts)
    except ValueError:
        return None
    shift = bool(code & 4)
    x, y = x - 1, y - 1
    if code & 64:
        return Mouse("wheel_up" if code & 1 == 0 else "wheel_down", x, y, shift=shift)
    button = code & 3
    if code & 32:
        return Mouse("drag", x, y, button, shift)
    return Mouse("press" if final == "M" else "release", x, y, button, shift)


def _csi_event(prefix: str, params: str, final: str) -> Event | None:
    if prefix == "<" and final in "Mm":
        return _mouse(params, final)
    fields = params.split(";") if params else []
    if final == "~":
        base = _TILDE_KEYS.get(fields[0] if fields else "")
        if base is None:
            return None
        return Key(_with_modifiers(base, fields[1]) if len(fields) > 1 else base)
    base = _CSI_FINAL_KEYS.get(final)
    if base is None:
        return None
    if len(fields) > 1:
        return Key(_with_modifiers(base, fields[1]))
    return Key(base)


class InputParser:
    """Incremental parser; feed bytes as they arrive and collect events."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending = ""
        self._paste: list[str] | None = None

    @property
    def has_pending_escape(self) -> bool:
        return self._pending.startswith(ESC)

    def feed(self, data: bytes) -> list[Event]:
        self._pending += self._decoder.decode(data)
        return self._drain(final=False)

    def flush(self) -> list[Event]:
        """Call after an input lull: a lone ESC is the Escape key, not a prefix."""
        return self._drain(final=True)

    def _drain(self, final: bool) -> list[Event]:
        events: list[Event] = []
        while self._pending:
            if self._paste is not None:
                end = self._pending.find(_PASTE_END)
                if end < 0:
                    self._paste.append(self._pending)
                    self._pending = ""
                    break
                self._paste.append(self._pending[:end])
                events.append(Paste("".join(self._paste)))
                self._paste = None
                self._pending = self._pending[end + len(_PASTE_END) :]
                continue
            if self._pending.startswith(_PASTE_START):
                self._paste = []
                self._pending = self._pending[len(_PASTE_START) :]
                continue
            consumed, event = self._next(final)
            if consumed == 0:
                break
            self._pending = self._pending[consumed:]
            if event is not None:
                events.append(event)
        return events

    def _next(self, final: bool) -> tuple[int, Event | None]:
        text = self._pending
        ch = text[0]
        if ch != ESC:
            if ch in _SINGLE:
                return 1, Key(_SINGLE[ch])
            if ord(ch) < 0x20:
                return 1, Key("ctrl+" + chr(ord(ch) + 0x60))
            return 1, Key(ch)
        if len(text) == 1:
            return (1, Key("escape")) if final else (0, None)
        return self._escape(text, final)

    @staticmethod
    def _escape(text: str, final: bool) -> tuple[int, Event | None]:
        csi = _CSI.match(text)
        if csi:
            return csi.end(), _csi_event(csi[1], csi[2], csi[3])
        ss3 = _SS3.match(text)
        if ss3:
            return ss3.end(), Key(_CSI_FINAL_KEYS.get(ss3[1], ss3[1]))
        if text[1] == "[" or text[1] == "O":
            # Incomplete escape sequence: wait for more bytes unless input went quiet.
            return (len(text), None) if final else (0, None)
        if text[1] == ESC:
            return 1, Key("escape")
        second = text[1]
        if second in _SINGLE:
            return 2, Key("alt+" + _SINGLE[second])
        return 2, Key("alt+" + second)
