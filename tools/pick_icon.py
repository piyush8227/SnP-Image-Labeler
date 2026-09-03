"""
SnP Image Labeler - Icon candidate comparison

Shrinking is the whole test. A mark that looks great at 1024 px often turns to
mud at 32 px, and you cannot see that by looking at the generated file.

This renders every candidate at the sizes Windows actually draws, side by
side on the app's own background, plus a greyscale copy (if colour is doing
the identifying, the mark is too weak). It also runs a few objective checks:
squareness, flat-vector-ness, transparency, and how close the artwork sits to
the app palette.

Usage:
    python tools/pick_icon.py logo_candidates/           # a folder of PNGs
    python tools/pick_icon.py a.png b.png c.png          # or individual files
    python tools/pick_icon.py logo_candidates/ --out sheet.png

Then open the sheet it writes and judge at 32 px, not at 1024.
"""

import argparse
import os
import sys
from pathlib import Path

# Deliberately NOT forcing the offscreen platform: it ships with an empty font
# database on Windows, so every label on the sheet would silently draw as
# nothing. No window is ever shown either way.

# Console output: Windows consoles and pipes default to cp1252, which raises
# on any non-ASCII character and can kill a run mid-report.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from PyQt5.QtCore import QRect, Qt
from PyQt5.QtGui import QColor, QFont, QImage, QPainter, QPen
from PyQt5.QtWidgets import QApplication

# The app's palette (style.qss)
BG       = QColor("#1e1e2e")
PANEL    = QColor("#181825")
LINE     = QColor("#313244")
TEXT     = QColor("#cdd6f4")
DIM      = QColor("#6c7086")
BLUE     = QColor("#89b4fa")
GREEN    = QColor("#a6e3a1")
YELLOW   = QColor("#f9e2af")
RED      = QColor("#f38ba8")

BRAND = ["#89b4fa", "#74c7ec", "#1e1e2e", "#181825", "#313244",
         "#45475a", "#cdd6f4", "#a6e3a1", "#f9e2af", "#f38ba8"]

PREVIEW_SIZES = [256, 64, 32, 16]
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

ROW_H   = 268
SHEET_W = 1120
LEFT    = 28


# ------------------------------------------------------------------ #
#  Checks                                                              #
# ------------------------------------------------------------------ #

def unique_colours(image: QImage, side: int = 64) -> int:
    """Flat vector art has few colours; gradients and AI mush have many."""
    small = image.scaled(side, side, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    seen = set()
    for y in range(small.height()):
        for x in range(small.width()):
            argb = small.pixel(x, y)
            # Quantise to 5 bits per channel so anti-aliasing is not counted
            seen.add(((argb >> 19) & 0x1F, (argb >> 11) & 0x1F, (argb >> 3) & 0x1F))
    return len(seen)


def has_alpha(image: QImage) -> bool:
    if not image.hasAlphaChannel():
        return False
    step = max(1, image.width() // 64)
    for y in range(0, image.height(), step):
        for x in range(0, image.width(), step):
            if ((image.pixel(x, y) >> 24) & 0xFF) < 250:
                return True
    return False


def palette_distance(image: QImage) -> int:
    """
    Average distance from each dominant colour to the nearest brand colour,
    0-441. Under ~40 means the artwork is on-palette.
    """
    brand = []
    for hexval in BRAND:
        c = QColor(hexval)
        brand.append((c.red(), c.green(), c.blue()))

    small = image.scaled(48, 48, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    counts = {}
    for y in range(small.height()):
        for x in range(small.width()):
            argb = small.pixel(x, y)
            if ((argb >> 24) & 0xFF) < 128:
                continue
            key = (((argb >> 16) & 0xFF) // 16, ((argb >> 8) & 0xFF) // 16,
                   (argb & 0xFF) // 16)
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return 999

    top = sorted(counts.items(), key=lambda kv: -kv[1])[:6]
    total = 0.0
    weight = 0
    for (r, g, b), n in top:
        rgb = (r * 16 + 8, g * 16 + 8, b * 16 + 8)
        best = min(((rgb[0] - br) ** 2 + (rgb[1] - bg) ** 2 + (rgb[2] - bb) ** 2) ** 0.5
                   for br, bg, bb in brand)
        total += best * n
        weight += n
    return int(total / weight) if weight else 999


def to_greyscale(image: QImage) -> QImage:
    grey = image.convertToFormat(QImage.Format_ARGB32)
    for y in range(grey.height()):
        for x in range(grey.width()):
            argb = grey.pixel(x, y)
            a = (argb >> 24) & 0xFF
            r = (argb >> 16) & 0xFF
            g = (argb >> 8) & 0xFF
            b = argb & 0xFF
            v = int(0.299 * r + 0.587 * g + 0.114 * b)
            grey.setPixel(x, y, (a << 24) | (v << 16) | (v << 8) | v)
    return grey


def verdicts(image: QImage, path: Path):
    """Returns a list of (colour, text) findings."""
    out = []
    w, h = image.width(), image.height()

    if w != h:
        out.append((YELLOW, f"not square ({w}x{h}) - will be padded"))
    else:
        out.append((GREEN, f"square {w}x{h}"))

    if min(w, h) < 512:
        out.append((RED, f"only {min(w, h)} px - regenerate at 1024"))

    n = unique_colours(image)
    if n <= 40:
        out.append((GREEN, f"flat artwork ({n} colours)"))
    elif n <= 120:
        out.append((YELLOW, f"some shading ({n} colours)"))
    else:
        out.append((RED, f"gradient-heavy ({n} colours) - will mush at 32 px"))

    d = palette_distance(image)
    if d <= 40:
        out.append((GREEN, f"on palette (distance {d})"))
    elif d <= 90:
        out.append((YELLOW, f"near palette (distance {d})"))
    else:
        out.append((RED, f"off palette (distance {d}) - won't match the UI"))

    out.append((DIM, "transparent background" if has_alpha(image)
                else "solid background"))
    return out


# ------------------------------------------------------------------ #
#  Sheet                                                              #
# ------------------------------------------------------------------ #

def draw_row(painter: QPainter, image: QImage, path: Path, index: int, top: int):
    painter.setPen(QPen(LINE, 1))
    painter.drawLine(LEFT, top, SHEET_W - LEFT, top)

    # Candidate label
    painter.setFont(QFont("Consolas", 11, QFont.Bold))
    painter.setPen(QPen(BLUE))
    painter.drawText(LEFT, top + 30, f"[{index}]")
    painter.setFont(QFont("Segoe UI", 10))
    painter.setPen(QPen(TEXT))
    name = path.name if len(path.name) <= 52 else path.name[:49] + "..."
    painter.drawText(LEFT + 36, top + 30, name)

    y = top + 48

    def cell(x, box, img, caption):
        """Draw one preview in its own box and caption it."""
        painter.fillRect(x, y, box, box, PANEL)
        painter.setPen(QPen(LINE, 1))
        painter.drawRect(x, y, box, box)
        painter.drawImage(x + (box - img.width()) // 2,
                          y + (box - img.height()) // 2, img)
        painter.setFont(QFont("Consolas", 9))
        painter.setPen(QPen(DIM))
        painter.drawText(QRect(x, y + box + 3, box, 16), Qt.AlignHCenter, caption)

    # The hero preview, then the sizes Windows actually draws
    x = LEFT
    cell(x, 200, image.scaled(200, 200, Qt.KeepAspectRatio, Qt.SmoothTransformation),
         "as generated")
    x += 200 + 14

    for size in PREVIEW_SIZES[1:]:          # 64, 32, 16 in 96 px boxes
        cell(x, 96, image.scaled(size, size, Qt.KeepAspectRatio,
                                 Qt.SmoothTransformation), f"{size} px")
        x += 96 + 12

    # Greyscale check at 64 - if colour is doing the identifying, it fails here
    grey = to_greyscale(image).scaled(64, 64, Qt.KeepAspectRatio,
                                      Qt.SmoothTransformation)
    cell(x, 96, grey, "grey 64")
    x += 96 + 22

    # Findings
    tx = x
    ty = y + 18
    painter.setFont(QFont("Segoe UI", 11))
    for colour, text in verdicts(image, path):
        painter.setPen(QPen(colour))
        painter.drawText(tx, ty, text)
        ty += 24


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare icon candidates at real sizes")
    parser.add_argument("inputs", nargs="+", help="image files or a folder")
    parser.add_argument("--out", default="icon_candidates.png",
                        help="sheet to write (default: icon_candidates.png)")
    args = parser.parse_args()

    app = QApplication(sys.argv)

    paths = []
    for item in args.inputs:
        p = Path(item)
        if p.is_dir():
            paths += sorted(q for q in p.iterdir()
                            if q.suffix.lower() in IMAGE_EXTS)
        elif p.is_file():
            paths.append(p)
        else:
            print(f"  skipped (not found): {item}")
    if not paths:
        print("No images found.")
        return 1

    loaded = []
    for p in paths:
        img = QImage(str(p))
        if img.isNull():
            print(f"  skipped (unreadable): {p.name}")
            continue
        loaded.append((p, img.convertToFormat(QImage.Format_ARGB32)))
    if not loaded:
        print("Nothing readable.")
        return 1

    height = 92 + ROW_H * len(loaded)
    sheet = QImage(SHEET_W, height, QImage.Format_ARGB32)
    sheet.fill(BG)

    painter = QPainter(sheet)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

    painter.setFont(QFont("Segoe UI", 15, QFont.Bold))
    painter.setPen(QPen(TEXT))
    painter.drawText(LEFT, 42, "SnP Image Labeler - icon candidates")
    painter.setFont(QFont("Segoe UI", 9))
    painter.setPen(QPen(DIM))
    painter.drawText(LEFT, 64,
                     "Judge at 32 px and in grey. 16 px is the title bar and "
                     "Alt+Tab; 32 px is the taskbar at 100% scaling.")

    top = 82
    for i, (path, image) in enumerate(loaded, start=1):
        draw_row(painter, image, path, i, top)
        top += ROW_H
    painter.end()

    out = Path(args.out)
    if not sheet.save(str(out), "PNG"):
        print(f"Failed to write {out}")
        return 1

    print(f"\n  {len(loaded)} candidate(s) -> {out.resolve()}")
    print("  Open it, pick the one that still reads at 32 px, then:")
    print("      python tools/make_icons.py <winner>.png\n")
    del app
    return 0


if __name__ == "__main__":
    sys.exit(main())
