"""
PyInstaller Runtime Hook — SnP Image Labeler
Patches sys._MEIPASS into the search path so bundled assets
(style.qss, icon.png) are found correctly at runtime.

This file is automatically executed by PyInstaller before main.py.
"""

import os
import sys


def _resource_path(relative: str) -> str:
    """Return absolute path to a bundled resource."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


# Patch builtins so main.py and style loader find assets
os.environ.setdefault("SNP_BUNDLE_DIR", _resource_path("."))
