"""Kitty graphics protocol: show PNG images inside the terminal (herdr forwards them)."""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass

_CHUNK = 4096
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_APC_START = "\x1b_G"
_APC_END = "\x1b\\"


class NotPngError(ValueError):
    pass


def png_size(data: bytes) -> tuple[int, int]:
    """Pixel size from the PNG IHDR chunk (the fixed header at the start of every PNG)."""
    if len(data) < 24 or not data.startswith(_PNG_SIGNATURE) or data[12:16] != b"IHDR":
        raise NotPngError("not a PNG image")
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def transmit(image_id: int, png: bytes) -> str:
    """Upload PNG data under `image_id` without displaying it yet."""
    payload = base64.standard_b64encode(png).decode("ascii")
    chunks = [payload[i : i + _CHUNK] for i in range(0, len(payload), _CHUNK)] or [""]
    out = []
    for index, chunk in enumerate(chunks):
        more = 1 if index < len(chunks) - 1 else 0
        first = f"a=t,f=100,t=d,i={image_id},q=2," if index == 0 else ""
        control = f"{first}m={more}"
        out.append(f"{_APC_START}{control};{chunk}{_APC_END}")
    return "".join(out)


def place(image_id: int, row: int, col: int, cols: int, rows: int) -> str:
    """Show a transmitted image scaled into a `cols` x `rows` cell box at (row, col)."""
    move = f"\x1b[{row + 1};{col + 1}H"
    return f"{move}{_APC_START}a=p,i={image_id},p=1,c={cols},r={rows},C=1,q=2{_APC_END}"


def delete_placements(image_id: int) -> str:
    return f"{_APC_START}a=d,d=i,i={image_id},q=2{_APC_END}"


def delete_image(image_id: int) -> str:
    """Remove placements and free the uploaded data."""
    return f"{_APC_START}a=d,d=I,i={image_id},q=2{_APC_END}"


@dataclass(frozen=True)
class CellSize:
    width: float
    height: float


# Typical terminal cell aspect when the pty does not report pixel sizes.
FALLBACK_CELL = CellSize(8.0, 17.0)


def fit_cells(
    image_px: tuple[int, int],
    box_cols: int,
    box_rows: int,
    cell: CellSize,
    upscale: bool = False,
) -> tuple[int, int]:
    """Largest cell box inside (box_cols, box_rows) that keeps the image aspect ratio.

    Small images are not enlarged beyond their natural size so icons stay crisp,
    unless `upscale` is set (video frames are decoded small for speed).
    """
    img_w, img_h = image_px
    if img_w <= 0 or img_h <= 0 or box_cols <= 0 or box_rows <= 0:
        return 0, 0
    box_w = box_cols * cell.width
    box_h = box_rows * cell.height
    scale = min(box_w / img_w, box_h / img_h)
    if not upscale:
        scale = min(scale, 1.0)
    cols = max(1, min(box_cols, round(img_w * scale / cell.width)))
    rows = max(1, min(box_rows, round(img_h * scale / cell.height)))
    return cols, rows
