"""
SnP Image Labeler - Icon builder

Turns one square master PNG (1024 x 1024, transparent background) into every
asset the app and the installer need, including a real multi-resolution
icon.ico.  Qt can only write a single-size .ico, so the container is
assembled here: DIB entries for the small sizes (what the Windows shell and
the Alt+Tab switcher prefer) and PNG entries for 128/256 (what Explorer's
large-icon views use).

Usage:
    python tools/make_icons.py assets/logo_master.png
    python tools/make_icons.py master.png --out .

Produces, next to the app:
    icon.ico                 16, 24, 32, 48, 64, 128, 256 px in one file
    icon.png                 256 px - the runtime window / taskbar icon
    assets/icon_512.png      docs, store listings
    assets/icon_1024.png     large export for print or a future macOS .icns
"""

import argparse
import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Console output: Windows consoles and pipes default to cp1252, which raises
# on any non-ASCII character and can kill a run mid-report.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from PyQt5.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PyQt5.QtGui import QColor, QImage, QPainter
from PyQt5.QtWidgets import QApplication

ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
PNG_ENTRY_FROM = 128          # this size and above are stored as PNG
PNG_EXPORTS = [256, 512, 1024]


# ------------------------------------------------------------------ #
#  Scaling                                                             #
# ------------------------------------------------------------------ #

def load_master(path: str) -> QImage:
    image = QImage(path)
    if image.isNull():
        raise SystemExit(f"Cannot read image: {path}")
    image = image.convertToFormat(QImage.Format_ARGB32)
    if image.width() != image.height():
        # Letterbox onto a square canvas rather than distorting the artwork
        side = max(image.width(), image.height())
        square = QImage(side, side, QImage.Format_ARGB32)
        square.fill(QColor(0, 0, 0, 0))
        painter = QPainter(square)
        painter.drawImage((side - image.width()) // 2,
                          (side - image.height()) // 2, image)
        painter.end()
        image = square
        print(f"  note: master was not square - padded to {side}x{side}")
    if image.width() < 512:
        print(f"  warning: master is only {image.width()} px; 1024 px is "
              f"recommended for a crisp 256 px icon")
    return image


def scaled(master: QImage, size: int) -> QImage:
    return master.scaled(size, size, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)


def knockout_corners(image: QImage, tolerance: int = 42) -> int:
    """
    Make the flat background outside a rounded tile transparent.

    Generated icons come back with the corners filled in - black on a dark
    tile, white on a light one - which shows up as a square block behind the
    icon in Explorer's light theme. This flood-fills inward from each corner
    and clears only what is connected to it, so the artwork is untouched.

    Returns the number of pixels cleared.
    """
    from collections import deque

    w, h = image.width(), image.height()
    corners = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
    refs = [image.pixel(x, y) for x, y in corners]

    def rgb(px):
        return ((px >> 16) & 0xFF, (px >> 8) & 0xFF, px & 0xFF)

    # Only act on a uniform border - never on artwork that runs to the edge
    base = rgb(refs[0])
    for other in refs[1:]:
        r, g, b = rgb(other)
        if abs(r - base[0]) + abs(g - base[1]) + abs(b - base[2]) > 3 * tolerance:
            return 0

    visited = bytearray(w * h)
    queue = deque(corners)
    cleared = 0
    soft = tolerance * 2

    while queue:
        x, y = queue.popleft()
        if x < 0 or y < 0 or x >= w or y >= h:
            continue
        idx = y * w + x
        if visited[idx]:
            continue
        visited[idx] = 1

        px = image.pixel(x, y)
        r, g, b = rgb(px)
        dist = ((r - base[0]) ** 2 + (g - base[1]) ** 2 + (b - base[2]) ** 2) ** 0.5
        if dist > soft:
            continue                      # reached the artwork

        if dist <= tolerance:
            alpha = 0
        else:                             # feather the anti-aliased rim
            alpha = int(255 * (dist - tolerance) / float(soft - tolerance))
        image.setPixel(x, y, (alpha << 24) | (r << 16) | (g << 8) | b)
        cleared += 1

        queue.append((x + 1, y))
        queue.append((x - 1, y))
        queue.append((x, y + 1))
        queue.append((x, y - 1))

    return cleared


def png_bytes(image: QImage) -> bytes:
    buf = QBuffer(QByteArray())
    buf.open(QIODevice.WriteOnly)
    image.save(buf, "PNG")
    return bytes(buf.data())


# ------------------------------------------------------------------ #
#  ICO container                                                       #
# ------------------------------------------------------------------ #

def dib_bytes(image: QImage) -> bytes:
    """32-bit BGRA DIB (BITMAPINFOHEADER + bottom-up pixels + AND mask)."""
    w, h = image.width(), image.height()
    header = struct.pack(
        "<IiiHHIIiiII",
        40,          # biSize
        w,           # biWidth
        h * 2,       # biHeight - XOR image plus AND mask
        1,           # biPlanes
        32,          # biBitCount
        0,           # biCompression = BI_RGB
        0,           # biSizeImage
        0, 0,        # biXPelsPerMeter, biYPelsPerMeter
        0, 0,        # biClrUsed, biClrImportant
    )

    pixels = bytearray()
    for y in range(h - 1, -1, -1):              # bottom-up
        for x in range(w):
            argb = image.pixel(x, y)
            a = (argb >> 24) & 0xFF
            r = (argb >> 16) & 0xFF
            g = (argb >> 8) & 0xFF
            b = argb & 0xFF
            pixels += bytes((b, g, r, a))       # BGRA

    # AND mask: 1 bit per pixel, each row padded to a 4-byte boundary.
    # Zeroed - the 32-bit alpha channel carries the real transparency.
    row_bytes = ((w + 31) // 32) * 4
    mask = bytes(row_bytes * h)
    return header + bytes(pixels) + mask


def build_ico(master: QImage, dest: Path, sizes=ICO_SIZES) -> None:
    entries = []
    for size in sizes:
        image = scaled(master, size)
        payload = (png_bytes(image) if size >= PNG_ENTRY_FROM
                   else dib_bytes(image))
        entries.append((size, payload))

    offset = 6 + 16 * len(entries)
    directory = b""
    blob = b""
    for size, payload in entries:
        directory += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,     # width  (0 means 256)
            0 if size >= 256 else size,     # height (0 means 256)
            0,                              # colours in palette
            0,                              # reserved
            1,                              # colour planes
            32,                             # bits per pixel
            len(payload),
            offset,
        )
        blob += payload
        offset += len(payload)

    header = struct.pack("<HHH", 0, 1, len(entries))    # reserved, type=icon
    dest.write_bytes(header + directory + blob)


def verify_ico(path: Path) -> list:
    """Read the container back and report the sizes it actually contains."""
    from PyQt5.QtGui import QImageReader
    reader = QImageReader(str(path))
    found = []
    for i in range(reader.imageCount()):
        reader.jumpToImage(i)
        size = reader.size()
        found.append(f"{size.width()}x{size.height()}")
    return found


# ------------------------------------------------------------------ #
#  Main                                                                #
# ------------------------------------------------------------------ #

def main() -> int:
    parser = argparse.ArgumentParser(description="Build SnP Image Labeler icons")
    parser.add_argument("master", help="square master PNG, 1024x1024 preferred")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent.parent),
                        help="output folder (default: the project root)")
    parser.add_argument("--no-knockout", action="store_true",
                        help="keep the generated background corners opaque")
    args = parser.parse_args()

    app = QApplication(sys.argv)          # needed for image plugins
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print(f"\nMaster: {args.master}")
    master = load_master(args.master)
    print(f"  loaded {master.width()}x{master.height()}")

    if args.no_knockout:
        print("  corners kept opaque (--no-knockout)\n")
    else:
        cleared = knockout_corners(master)
        if cleared:
            pct = 100.0 * cleared / (master.width() * master.height())
            print(f"  corners knocked out: {cleared:,} px ({pct:.1f}%) "
                  f"now transparent\n")
        else:
            print("  corners left as they are (background is not uniform)\n")

    # icon.png sits next to the app (the runtime fallback); the bigger
    # exports are documentation assets and live in assets/.
    assets = out / "assets"
    for size in PNG_EXPORTS:
        if size == 256:
            dest = out / "icon.png"
        else:
            assets.mkdir(parents=True, exist_ok=True)
            dest = assets / f"icon_{size}.png"
        if not scaled(master, size).save(str(dest), "PNG"):
            print(f"  FAILED to write {dest}")
            return 1
        print(f"  wrote {str(dest.relative_to(out)):<22} {size}x{size}")

    ico = out / "icon.ico"
    build_ico(master, ico)
    print(f"  wrote {ico.name:<16} {', '.join(verify_ico(ico))}"
          f"   ({ico.stat().st_size / 1024:.0f} KB)")

    print("\nNext:")
    print("  1. python main.py            - check the window and taskbar icon")
    print("  2. build_windows.bat         - the .exe picks up icon.ico\n")
    del app
    return 0


if __name__ == "__main__":
    sys.exit(main())
