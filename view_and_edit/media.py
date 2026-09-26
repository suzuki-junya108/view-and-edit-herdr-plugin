"""Convert files to PNG previews and play audio/video, using whatever tools exist.

Everything prefers tools that ship with macOS (sips, qlmanage, afplay, afinfo,
textutil) and uses Chrome, ffmpeg, and poppler only when they are installed.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import queue
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from view_and_edit import kitty
from view_and_edit.formats import Kind
from view_and_edit.structured import read_docx_text

PREVIEW_PIXELS = 1600
_PNG_HEADER_BYTES = 24
VIDEO_FRAME_WIDTH = 640
VIDEO_FPS = 10
CONVERT_TIMEOUT = 30
# Quick Look answers valid files in well under a second but can hang on broken ones.
QUICKLOOK_TIMEOUT = 15
CHROME_TIMEOUT = 45
_CHROME_SCREENSHOT_DONE = b"bytes written to file"
_STOP_GRACE_SECONDS = 2
_TIMED_OUT = 124  # same exit status coreutils `timeout` uses
_READ_CHUNK = 65536
_PNG_END = b"IEND\xaeB`\x82"
_CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
)


class PreviewError(Exception):
    """No available tool could produce a preview."""


def which(name: str) -> str | None:
    return shutil.which(name)


def is_macos() -> bool:
    return sys.platform == "darwin"


class Cache:
    """Per-process scratch directory, removed on exit so previews never pile up."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="view-and-edit-"))
        atexit.register(shutil.rmtree, self.root, True)

    def path_for(self, source: Path, variant: str, suffix: str = ".png") -> Path:
        stat = source.stat()
        key = f"{source}|{stat.st_mtime_ns}|{stat.st_size}|{variant}"
        return self.root / (hashlib.sha256(key.encode()).hexdigest()[:24] + suffix)


def _run(argv: list[str], timeout: int = CONVERT_TIMEOUT) -> subprocess.CompletedProcess[bytes]:
    """Run a converter; a hung tool counts as a failure so the next fallback gets a turn."""
    try:
        return subprocess.run(
            argv, capture_output=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, _TIMED_OUT, b"", b"")


def _quicklook(source: Path, out: Path, size: int = PREVIEW_PIXELS) -> Path | None:
    if not which("qlmanage"):
        return None
    workdir = out.parent / (out.stem + ".ql")
    workdir.mkdir(exist_ok=True)
    _run(["qlmanage", "-t", "-s", str(size), "-o", str(workdir), str(source)], QUICKLOOK_TIMEOUT)
    produced = workdir / (source.name + ".png")
    if produced.exists():
        produced.replace(out)
        return out
    return None


def _sips_size(source: Path) -> tuple[int, int] | None:
    result = _run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(source)])
    text = result.stdout.decode(errors="replace")
    sizes = dict(re.findall(r"pixel(Width|Height):\s*(\d+)", text))
    if result.returncode != 0 or len(sizes) != 2:
        return None
    return int(sizes["Width"]), int(sizes["Height"])


def _sips(source: Path, out: Path) -> Path | None:
    if not which("sips"):
        return None
    argv = ["sips", "-s", "format", "png"]
    # --resampleHeightWidthMax also enlarges small images, which only bloats the PNG.
    size = _sips_size(source)
    if size is None or max(size) > PREVIEW_PIXELS:
        argv += ["--resampleHeightWidthMax", str(PREVIEW_PIXELS)]
    result = _run([*argv, str(source), "--out", str(out)])
    return out if result.returncode == 0 and out.exists() else None


def _ffmpeg_frame(source: Path, out: Path, seek: float = 0.0) -> Path | None:
    if not which("ffmpeg"):
        return None
    argv = ["ffmpeg", "-v", "error", "-y"]
    if seek:
        argv += ["-ss", f"{seek:.2f}"]
    argv += [
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-vf",
        f"scale='min({PREVIEW_PIXELS},iw)':-2",
        str(out),
    ]
    result = _run(argv)
    return out if result.returncode == 0 and out.exists() else None


def chrome_binary() -> str | None:
    for candidate in _CHROME_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
        found = which(name)
        if found:
            return found
    return None


# A preview must not phone home: pages may reference trackers or remote scripts.
# Every request goes to a proxy that does not exist (loopback included, which Chrome
# normally exempts) and no hostname resolves, so only the local file renders.
_CHROME_OFFLINE_FLAGS = (
    "--proxy-server=http://127.0.0.1:9",
    "--proxy-bypass-list=<-loopback>",
    "--host-resolver-rules=MAP * ~NOTFOUND",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--no-pings",
)


def _chrome_screenshot(
    source: Path, out: Path, width: int, height: int, cache: Cache
) -> Path | None:
    chrome = chrome_binary()
    if not chrome:
        return None
    profile = cache.root / "chrome-profile"
    argv = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--disable-extensions",
        f"--user-data-dir={profile}",
        *_CHROME_OFFLINE_FLAGS,
        f"--window-size={width},{height}",
        "--virtual-time-budget=3000",
        f"--screenshot={out}",
        source.resolve().as_uri(),
    ]
    written = _run_until_marker(argv, _CHROME_SCREENSHOT_DONE, CHROME_TIMEOUT)
    return out if written and out.exists() else None


def _run_until_marker(argv: list[str], marker: bytes, timeout: float) -> bool:
    """Run `argv` until it prints `marker` on stderr, then stop it and its children.

    Headless Chrome sometimes keeps running after it has written the screenshot, so
    its exit cannot be the signal that the work is done.
    """
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,  # own process group, so helpers are stopped too
        )
    except OSError:
        return False
    stderr = proc.stderr
    if stderr is None:
        _stop_group(proc)
        return False
    seen = b""
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(stderr, selectors.EVENT_READ)
            while marker not in seen:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    return False
                chunk = os.read(stderr.fileno(), 65536)
                if not chunk:  # exited without printing the marker
                    return False
                # Keep only a tail long enough to catch a marker split across reads.
                seen = (seen + chunk)[-(len(marker) + 65536) :]
        return True
    finally:
        _stop_group(proc)
        stderr.close()


def _stop_group(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=_STOP_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            continue


def _pdftoppm(source: Path, out: Path, page: int) -> Path | None:
    if not which("pdftoppm"):
        return None
    prefix = out.with_suffix("")
    result = _run(
        [
            "pdftoppm",
            "-f",
            str(page + 1),
            "-l",
            str(page + 1),
            "-png",
            "-singlefile",
            "-scale-to",
            str(PREVIEW_PIXELS),
            str(source),
            str(prefix),
        ]
    )
    produced = prefix.with_suffix(".png")
    return produced if result.returncode == 0 and produced.exists() else None


@dataclass(frozen=True)
class ImageRequest:
    """What preview image to build: `page` for PDFs, `aspect` (h/w) for HTML."""

    page: int = 0
    aspect: float = 0.6


def _small_png(source: Path) -> bool:
    """A PNG that can be shown as-is; big ones are shrunk so the terminal gets less data."""
    try:
        with source.open("rb") as fh:
            header = fh.read(_PNG_HEADER_BYTES)  # IHDR sits at a fixed offset
        return max(kitty.png_size(header)) <= PREVIEW_PIXELS
    except (OSError, kitty.NotPngError):
        return False


def preview_png(
    source: Path, kind: Kind, cache: Cache, request: ImageRequest | None = None
) -> Path:
    """Return a PNG path representing `source`, or raise PreviewError."""
    request = request or ImageRequest()
    variant = f"{kind.name}:{request.page}:{request.aspect:.2f}"
    out = cache.path_for(source, variant)
    if out.exists():
        return out
    result: Path | None = None
    if kind is Kind.IMAGE:
        if source.suffix.lower() == ".png" and _small_png(source):
            return source
        result = _sips(source, out) or _ffmpeg_frame(source, out) or _quicklook(source, out)
    elif kind is Kind.HTML:
        width = 1280
        height = max(400, min(4000, int(width * request.aspect)))
        result = _chrome_screenshot(source, out, width, height, cache) or _quicklook(source, out)
    elif kind is Kind.PDF:
        result = _pdftoppm(source, out, request.page)
        if result is None and request.page == 0:
            result = _quicklook(source, out)
    elif kind is Kind.VIDEO:
        result = (
            _ffmpeg_frame(source, out, seek=1.0)
            or _ffmpeg_frame(source, out)
            or _quicklook(source, out)
        )
    else:
        result = _quicklook(source, out)
    if result is None:
        raise PreviewError(_missing_tool_hint(kind))
    return result


def _missing_tool_hint(kind: Kind) -> str:
    if kind is Kind.PDF:
        return "PDF を画像にできませんでした（poppler の pdftoppm か macOS の Quick Look が必要）"
    if kind is Kind.VIDEO:
        return "動画のサムネイルを作れませんでした（ffmpeg か macOS の Quick Look が必要）"
    if kind is Kind.HTML:
        return "HTML を描画できませんでした（Chrome か macOS の Quick Look が必要）"
    return "プレビュー画像を作れませんでした（macOS の Quick Look が必要）"


def pdf_page_count(source: Path) -> int:
    if which("pdfinfo"):
        result = _run(["pdfinfo", str(source)])
        match = re.search(rb"^Pages:\s+(\d+)", result.stdout, re.MULTILINE)
        if match:
            return int(match[1])
    if which("mdls"):
        result = _run(["mdls", "-raw", "-name", "kMDItemNumberOfPages", str(source)])
        if result.stdout.strip().isdigit():
            return int(result.stdout)
    return 1


def document_text(source: Path, kind: Kind) -> str:
    """Plain text of a document, or raise PreviewError."""
    if kind is Kind.PDF:
        if not which("pdftotext"):
            raise PreviewError("PDF のテキスト抽出には poppler の pdftotext が必要です")
        result = _run(["pdftotext", "-layout", "-enc", "UTF-8", str(source), "-"])
        return result.stdout.decode("utf-8", "replace")
    if which("textutil") and source.suffix.lower() in {
        ".docx",
        ".doc",
        ".rtf",
        ".rtfd",
        ".odt",
        ".html",
        ".htm",
    }:
        result = _run(["textutil", "-convert", "txt", "-stdout", str(source)])
        if result.returncode == 0:
            return result.stdout.decode("utf-8", "replace")
    if source.suffix.lower() == ".docx":
        return read_docx_text(source)
    raise PreviewError("この形式のテキスト抽出には対応していません")


def media_info(source: Path) -> list[tuple[str, str]]:
    """Human-readable facts about an audio/video file."""
    if which("ffprobe"):
        result = _run(
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(source),
            ]
        )
        if result.returncode == 0:
            return _describe_probe(json.loads(result.stdout or b"{}"))
    if which("afinfo"):
        result = _run(["afinfo", str(source)])
        facts = []
        for raw in result.stdout.decode("utf-8", "replace").splitlines():
            numbers = re.findall(r"\d+(?:\.\d+)?", raw)
            if "duration" in raw and numbers:
                facts.append(("長さ", format_duration(float(numbers[0]))))
            elif raw.startswith("Data format"):
                facts.append(("形式", raw.split(":", 1)[1].strip()))
        return facts
    return []


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def media_duration(source: Path) -> float | None:
    for label, value in media_info(source):
        if label == "長さ":
            parts = [int(p) for p in value.split(":")]
            total = 0
            for part in parts:
                total = total * 60 + part
            return float(total)
    return None


def _describe_probe(data: dict[str, object]) -> list[tuple[str, str]]:
    facts: list[tuple[str, str]] = []
    fmt = data.get("format")
    if isinstance(fmt, dict):
        if fmt.get("duration"):
            facts.append(("長さ", format_duration(float(fmt["duration"]))))
        if fmt.get("bit_rate"):
            facts.append(("ビットレート", f"{int(fmt['bit_rate']) // 1000} kbps"))
        tags = fmt.get("tags")
        if isinstance(tags, dict):
            for key, label in (
                ("title", "タイトル"),
                ("artist", "アーティスト"),
                ("album", "アルバム"),
            ):
                value = tags.get(key) or tags.get(key.upper())
                if value:
                    facts.append((label, str(value)))
    streams = data.get("streams")
    if isinstance(streams, list):
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            if stream.get("codec_type") == "video" and stream.get("width"):
                rate = str(stream.get("avg_frame_rate", "0/1"))
                num, _, den = rate.partition("/")
                fps = f" {float(num) / float(den):.0f}fps" if den and float(den) else ""
                facts.append(
                    (
                        "映像",
                        f"{stream.get('codec_name')} {stream['width']}x{stream.get('height')}{fps}",
                    )
                )
            elif stream.get("codec_type") == "audio":
                facts.append(
                    (
                        "音声",
                        f"{stream.get('codec_name')} {stream.get('sample_rate', '')}Hz"
                        f" {stream.get('channels', '')}ch",
                    )
                )
    return facts


# ---- OS integration -----------------------------------------------------------
def open_external(path: Path) -> str | None:
    """Open with the default app. Returns an error message on failure."""
    opener = "open" if is_macos() else which("xdg-open")
    if not opener:
        return "既定のアプリで開くコマンドが見つかりません"
    subprocess.Popen(
        [opener, str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return None


def _trash_result(result: subprocess.CompletedProcess[bytes]) -> str | None:
    if result.returncode == 0:
        return None
    return result.stderr.decode("utf-8", "replace").strip() or "ゴミ箱に移動できませんでした"


def move_to_trash(path: Path) -> str | None:
    """Move to the OS trash so deletions can be undone. Returns an error message."""
    if is_macos():
        # The path travels as an argv item, never spliced into the script source.
        script = (
            "on run argv\n"
            'tell application "Finder" to delete (POSIX file (item 1 of argv) as alias)\n'
            "end run"
        )
        result = _run(["osascript", "-e", script, str(path)])
        return _trash_result(result)
    gio = which("gio")
    if gio:
        result = _run([gio, "trash", str(path)])
        return _trash_result(result)
    return "ゴミ箱に移動する手段がありません"


def copy_to_clipboard(text: str) -> bool:
    for argv in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
        if which(argv[0]):
            subprocess.run(argv, input=text.encode(), check=False, timeout=5)
            return True
    return False


def paste_from_clipboard() -> str | None:
    for argv in (["pbpaste"], ["wl-paste", "-n"], ["xclip", "-selection", "clipboard", "-o"]):
        if which(argv[0]):
            result = subprocess.run(argv, capture_output=True, check=False, timeout=5)
            return result.stdout.decode("utf-8", "replace")
    return None


# ---- playback -----------------------------------------------------------------
class AudioPlayer:
    """Plays a file's audio track; pause/resume by stopping the player process."""

    def __init__(self, source: Path) -> None:
        self.source = source
        self.process: subprocess.Popen[bytes] | None = None
        self.offset = 0.0
        self._started_at = 0.0
        self._paused_at: float | None = None

    @staticmethod
    def available() -> bool:
        return bool(which("ffplay") or which("afplay"))

    def start(self, offset: float = 0.0) -> None:
        self.stop()
        self.offset = max(0.0, offset)
        if which("ffplay"):
            argv = [
                "ffplay",
                "-nodisp",
                "-autoexit",
                "-vn",
                "-loglevel",
                "quiet",
                "-ss",
                f"{self.offset:.2f}",
                str(self.source),
            ]
        elif which("afplay") and self.offset == 0:
            argv = ["afplay", str(self.source)]
        else:
            return
        self.process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self._started_at = time.monotonic()
        self._paused_at = None

    @property
    def can_seek(self) -> bool:
        return bool(which("ffplay"))

    @property
    def playing(self) -> bool:
        return self.process is not None and self.process.poll() is None and self._paused_at is None

    @property
    def paused(self) -> bool:
        return self._paused_at is not None

    @property
    def finished(self) -> bool:
        return self.process is not None and self.process.poll() is not None

    def position(self) -> float:
        if self.process is None:
            return self.offset
        now = self._paused_at if self._paused_at is not None else time.monotonic()
        return self.offset + now - self._started_at

    def toggle_pause(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        if self._paused_at is None:
            self.process.send_signal(signal.SIGSTOP)
            self._paused_at = time.monotonic()
        else:
            self.process.send_signal(signal.SIGCONT)
            self._started_at += time.monotonic() - self._paused_at
            self._paused_at = None

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            if self._paused_at is not None:
                self.process.send_signal(signal.SIGCONT)
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        self._paused_at = None


def split_png_stream(buffer: bytearray) -> list[bytes]:
    """Pop complete PNG images from a byte stream produced by `-f image2pipe`."""
    frames: list[bytes] = []
    while True:
        end = buffer.find(_PNG_END)
        if end < 0:
            return frames
        end += len(_PNG_END)
        frames.append(bytes(buffer[:end]))
        del buffer[:end]


class VideoPlayer:
    """Decodes frames with ffmpeg into a small queue; the UI shows them on schedule."""

    def __init__(self, source: Path, on_frame: Callable[[], None]) -> None:
        self.source = source
        self.audio = AudioPlayer(source)
        self._on_frame = on_frame
        self._process: subprocess.Popen[bytes] | None = None
        self._frames: queue.Queue[tuple[int, bytes]] = queue.Queue(maxsize=VIDEO_FPS * 2)
        self._thread: threading.Thread | None = None
        self._offset = 0.0
        self._clock_start = 0.0
        self._paused_at: float | None = None
        self._generation = 0
        self.ended = False

    @staticmethod
    def available() -> bool:
        return bool(which("ffmpeg"))

    def start(self, offset: float = 0.0) -> None:
        self.stop()
        self._generation += 1
        self._offset = max(0.0, offset)
        self.ended = False
        self._frames = queue.Queue(maxsize=VIDEO_FPS * 2)
        argv = [
            "ffmpeg", "-v", "error", "-ss", f"{self._offset:.2f}", "-i", str(self.source), "-an",
            "-vf", f"fps={VIDEO_FPS},scale='min({VIDEO_FRAME_WIDTH},iw)':-2",
            "-f", "image2pipe", "-c:v", "png", "-",
        ]  # fmt: skip
        self._process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        self._thread = threading.Thread(
            target=self._read, args=(self._process, self._generation), daemon=True
        )
        self._thread.start()
        self.audio.start(self._offset)
        self._clock_start = time.monotonic()
        self._paused_at = None

    def _read(self, process: subprocess.Popen[bytes], generation: int) -> None:
        stdout = process.stdout
        if stdout is None:
            return
        buffer = bytearray()
        index = 0
        while True:
            chunk = os.read(stdout.fileno(), _READ_CHUNK)
            if not chunk:
                break
            buffer.extend(chunk)
            for frame in split_png_stream(buffer):
                while generation == self._generation:
                    try:
                        self._frames.put((index, frame), timeout=0.2)
                        break
                    except queue.Full:
                        continue
                if generation != self._generation:
                    return
                index += 1
                self._on_frame()
        if generation == self._generation:
            self._frames.put((-1, b""))
            self._on_frame()

    def position(self) -> float:
        now = self._paused_at if self._paused_at is not None else time.monotonic()
        return self._offset + now - self._clock_start

    @property
    def paused(self) -> bool:
        return self._paused_at is not None

    def due_frame(self) -> bytes | None:
        """Latest frame whose time has come, dropping late ones to stay in sync."""
        if self._paused_at is not None:
            return None
        elapsed = time.monotonic() - self._clock_start
        latest = None
        while True:
            with self._frames.mutex:
                head = self._frames.queue[0] if self._frames.queue else None
            if head is None:
                return latest
            index, frame = head
            if index < 0:
                self._frames.get_nowait()
                self.ended = True
                return latest
            if index / VIDEO_FPS > elapsed:
                return latest
            self._frames.get_nowait()
            latest = frame

    def toggle_pause(self) -> None:
        if self._process is None:
            return
        if self._paused_at is None:
            if self._process.poll() is None:
                self._process.send_signal(signal.SIGSTOP)
            self._paused_at = time.monotonic()
        else:
            if self._process.poll() is None:
                self._process.send_signal(signal.SIGCONT)
            self._clock_start += time.monotonic() - self._paused_at
            self._paused_at = None
        self.audio.toggle_pause()

    def stop(self) -> None:
        self._generation += 1
        if self._process is not None and self._process.poll() is None:
            if self._paused_at is not None:
                self._process.send_signal(signal.SIGCONT)
            self._process.kill()
            self._process.wait()
        self._process = None
        self._paused_at = None
        self.audio.stop()
