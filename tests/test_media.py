"""Converter plumbing: stopping stuck tools and keeping previews offline."""

from __future__ import annotations

import http.server
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import ClassVar

from view_and_edit import media
from view_and_edit.formats import Kind

# Prints the marker, then lingers with a child process, like headless Chrome can.
_LINGERING = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print(child.pid, flush=True)
sys.stderr.write("12 bytes written to file x.png\\n"); sys.stderr.flush()
time.sleep(60)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class RunUntilMarkerTest(unittest.TestCase):
    def test_stops_tool_and_children_once_marker_appears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "child.pid"
            script = _LINGERING.replace(
                "print(child.pid, flush=True)",
                f"open({str(pid_file)!r}, 'w').write(str(child.pid))",
            )
            started = time.monotonic()
            done = media._run_until_marker([sys.executable, "-c", script], b"bytes written", 20)
            self.assertTrue(done)
            self.assertLess(time.monotonic() - started, 10)
            child = int(pid_file.read_text())
            deadline = time.monotonic() + 5
            while _alive(child) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(_alive(child))

    def test_gives_up_after_timeout_without_marker(self) -> None:
        started = time.monotonic()
        done = media._run_until_marker(
            [sys.executable, "-c", "import time; time.sleep(60)"], b"x", 1
        )
        self.assertFalse(done)
        self.assertLess(time.monotonic() - started, 8)

    def test_missing_program_is_a_plain_failure(self) -> None:
        self.assertFalse(media._run_until_marker(["/nonexistent/tool"], b"x", 1))


class _CountingHandler(http.server.BaseHTTPRequestHandler):
    hits: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        _CountingHandler.hits.append(self.path)
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


@unittest.skipUnless(media.chrome_binary(), "needs Google Chrome")
class HtmlPreviewOfflineTest(unittest.TestCase):
    def test_page_cannot_reach_the_network(self) -> None:
        server = http.server.HTTPServer(("127.0.0.1", 0), _CountingHandler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)  # cleanups run last-in first-out
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / "page.html"
            page.write_text(
                f'<h1>offline</h1><img src="http://127.0.0.1:{port}/img.png">'
                f'<img src="http://localhost:{port}/host.png">'
                f'<script>fetch("http://127.0.0.1:{port}/js")</script>'
            )
            png = media.preview_png(page, Kind.HTML, media.Cache())
            self.assertTrue(png.read_bytes().startswith(b"\x89PNG"))
        self.assertEqual(_CountingHandler.hits, [])
