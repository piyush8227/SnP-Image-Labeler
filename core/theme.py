# SnP Image Labeler
# Copyright (C) 2026 Piyush More
#
# Licensed under the GNU General Public License, version 3 or later.
# See the LICENSE file at the repository root for the full text.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.

"""
SnP Image Labeler - Theme + UI scale

One place that decides every colour and every pixel size in the app.

Why it exists
-------------
Colours used to be hard-coded twice: as hex literals in style.qss and again as
hex literals inside the painting code (annotation_viewer, review_grid, ...).
That made a second theme impossible. Here every colour is a named token with a
value per mode, so the stylesheet and the painters read the same source.

Sizes
-----
build_stylesheet() multiplies every `Npx` in the stylesheet by the UI scale, so
one setting scales text, padding, borders and radii together. Widget code that
needs a pixel number asks scaled() for it instead of hard-coding one, which is
what keeps a 200% window from clipping its own controls.
"""

import re
from typing import Dict, List

LIGHT = "light"
DARK  = "dark"
MODES = (LIGHT, DARK)

# UI scales offered in the toolbar. Any float works; these are the presets.
SCALES: List[int] = [75, 100, 125, 150, 175, 200, 250, 300]

MIN_SCALE, MAX_SCALE = 0.5, 4.0


# ------------------------------------------------------------------ #
#  Tokens                                                              #
# ------------------------------------------------------------------ #
#
# Semantic names, not colour names: "accent", not "blue". A token means the
# same thing in both modes, which is what lets one stylesheet serve both.

_TOKENS: Dict[str, Dict[str, str]] = {
    LIGHT: {
        # Surfaces, lightest to deepest
        "bg":            "#ffffff",
        "bg_alt":        "#f3f4f6",
        "bg_deep":       "#e6e7eb",
        "bg_raised":     "#ffffff",
        "bg_hover":      "#eceef2",
        "bg_active":     "#dfe2e8",
        "bg_select":     "#dce8fb",
        "bg_stripe":     "#fafafb",

        # Text
        "text":          "#14141a",
        "text_muted":    "#44454f",
        "text_faint":    "#74757f",
        "text_on_accent":"#ffffff",

        # Lines
        "border":        "#d2d4da",
        "border_strong": "#b0b2bb",
        "border_muted":  "#c4c6cd",

        # Meaning
        "accent":        "#0b5ed7",
        "accent_hover":  "#0a53be",
        "accent_text":   "#0b4bb0",
        "accent_alt":    "#0b7285",
        "accent_soft":   "#dce8fb",
        "danger":        "#c0281a",
        "danger_soft":   "#fbe3e0",
        "danger_border": "#eab3ac",
        "success":       "#0f7b33",
        "warning":       "#9a6400",

        # Image canvas behind the photo
        "canvas":        "#9b9ea6",

        # Bounding box chrome
        "box_halo":      "#000000",
        "box_handle":    "#ffffff",
        "box_handle_rim":"#000000",
    },
    DARK: {
        "bg":            "#1e1e2e",
        "bg_alt":        "#181825",
        "bg_deep":       "#11111b",
        "bg_raised":     "#252538",
        "bg_hover":      "#313244",
        "bg_active":     "#45475a",
        "bg_select":     "#2e2e4a",
        "bg_stripe":     "#21212f",

        "text":          "#cdd6f4",
        "text_muted":    "#a6adc8",
        "text_faint":    "#6c7086",
        "text_on_accent":"#11111b",

        "border":        "#313244",
        "border_strong": "#45475a",
        "border_muted":  "#585b70",

        "accent":        "#89b4fa",
        "accent_hover":  "#5ba1e0",
        "accent_text":   "#89b4fa",
        "accent_alt":    "#74c7ec",
        "accent_soft":   "#2e3a52",
        "danger":        "#f38ba8",
        "danger_soft":   "#45303c",
        "danger_border": "#5a3a48",
        "success":       "#a6e3a1",
        "warning":       "#f9e2af",

        "canvas":        "#15151f",

        "box_halo":      "#000000",
        "box_handle":    "#ffffff",
        "box_handle_rim":"#000000",
    },
}

# Label colours for bounding boxes. Strongly saturated and dark enough to carry
# white text, because a box is drawn over an arbitrary photograph - the old
# pastel set vanished against light backgrounds, which is exactly the thing
# annotators complained about. Each is paired with its own halo in the painter.
_BOX_COLORS: Dict[str, List[str]] = {
    LIGHT: [
        "#d8232a", "#0067c0", "#0f7b33", "#b8008a", "#c75000",
        "#6a2bbd", "#007b8a", "#8a6d00", "#15587a", "#a01050",
    ],
    DARK: [
        "#ff5a5f", "#4aa3ff", "#4fd27a", "#ff6ad5", "#ffa14a",
        "#b58cff", "#4fd8e8", "#ffd84a", "#6ec7ff", "#ff7aa8",
    ],
}


def _mode(mode: str) -> str:
    return mode if mode in MODES else LIGHT


def tokens(mode: str) -> Dict[str, str]:
    """Every colour token for a mode."""
    return dict(_TOKENS[_mode(mode)])


def color(mode: str, token: str, fallback: str = "#808080") -> str:
    """One colour token, by name."""
    return _TOKENS[_mode(mode)].get(token, fallback)


def box_colors(mode: str) -> List[str]:
    """The per-label bounding-box palette for a mode."""
    return list(_BOX_COLORS[_mode(mode)])


def clamp_scale(scale: float) -> float:
    try:
        value = float(scale)
    except (TypeError, ValueError):
        return 1.0
    return max(MIN_SCALE, min(MAX_SCALE, value))


def scaled(px: float, scale: float, minimum: int = 1) -> int:
    """A pixel size at the current UI scale, never rounding away to nothing."""
    return max(minimum, int(round(float(px) * clamp_scale(scale))))


# ------------------------------------------------------------------ #
#  Stylesheet                                                          #
# ------------------------------------------------------------------ #

_TOKEN_RE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
_PX_RE    = re.compile(r"(?<![\w.])(\d+)px")


def build_stylesheet(template: str, mode: str, scale: float = 1.0) -> str:
    """
    Resolve a tokenised stylesheet for one mode and UI scale.

    `{{token}}` becomes that mode's colour; every `Npx` is multiplied by the
    scale so one setting moves text, padding and borders in step. A 1px border
    is never scaled below 1px, or it would disappear entirely.
    """
    palette = _TOKENS[_mode(mode)]
    factor  = clamp_scale(scale)

    def token_sub(match: "re.Match") -> str:
        name = match.group(1)
        if name not in palette:
            raise KeyError(f"Unknown theme token: {{{{{name}}}}}")
        return palette[name]

    resolved = _TOKEN_RE.sub(token_sub, template)

    if factor != 1.0:
        resolved = _PX_RE.sub(
            lambda m: f"{max(1, int(round(int(m.group(1)) * factor)))}px",
            resolved,
        )
    return resolved


# ------------------------------------------------------------------ #
#  Applying it                                                         #
# ------------------------------------------------------------------ #

def template_path() -> str:
    """
    Locate style.qss in both source and PyInstaller-bundled runs.

    Mirrors main._resource so the stylesheet is found either way; kept here so
    widget code can re-theme itself without importing the entry point.
    """
    import os
    import sys
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    base = os.environ.get("SNP_BUNDLE_DIR", base)
    return os.path.join(base, "style.qss")


_template_cache: Dict[str, str] = {}


def load_template() -> str:
    """The raw tokenised stylesheet, read once per process."""
    if "qss" not in _template_cache:
        try:
            with open(template_path(), "r", encoding="utf-8") as fh:
                _template_cache["qss"] = fh.read()
        except OSError:
            _template_cache["qss"] = ""
    return _template_cache["qss"]


# Point size the UI is designed around; the scale multiplies it.
BASE_POINT_SIZE = 9


# What apply() last put on the application. Widgets read this when they need a
# pixel number, instead of hard-coding one that only works at 100%.
_current: Dict[str, object] = {"mode": LIGHT, "scale": 1.0}


def current_mode() -> str:
    return str(_current.get("mode", LIGHT))


def current_scale() -> float:
    return float(_current.get("scale", 1.0))


def px(size: float, minimum: int = 1) -> int:
    """
    A pixel size at the UI scale in force.

    Widget chrome (button heights, thumbnail sizes, panel widths) has to grow
    with the text or it clips it. Call this instead of writing a literal.
    """
    return scaled(size, current_scale(), minimum)


def apply(app, mode: str, scale: float = 1.0) -> str:
    """
    Put `mode` at `scale` on the application: stylesheet plus default font.

    The stylesheet carries px font sizes for everything it styles; the app font
    covers what it does not reach (native menus, tooltips, message boxes), so
    both have to move together or those read tiny at 200%.
    """
    factor = clamp_scale(scale)
    _current["mode"], _current["scale"] = _mode(mode), factor
    sheet = build_stylesheet(load_template(), mode, factor)

    font = app.font()
    font.setPointSizeF(BASE_POINT_SIZE * factor)
    app.setFont(font)
    app.setStyleSheet(sheet)
    return sheet
