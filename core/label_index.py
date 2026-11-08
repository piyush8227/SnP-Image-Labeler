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
SnP Image Labeler - Label Index & Review Flags

LabelIndex answers "is this image done?" from one directory scan instead of
two filesystem stats per image.  The old path cost 2 x os.stat per image
every time the queue refreshed its status - 20,000 stats for a 10,000 image
queue, on the GUI thread, and far worse on a network share.

ReviewFlags stores the "flagged for review" state, which is deliberately not
the same thing as "labelled": an image the labeller is unsure about needs to
come back, not to be counted as finished.
"""

import json
import os
from pathlib import Path
from typing import Iterable, List, Optional, Set

LABEL_SUFFIXES = (".csv", ".json")
FLAGS_FILENAME = "_review_flags.json"
_CASE_INSENSITIVE = os.name == "nt"


def _key(stem: str) -> str:
    return stem.casefold() if _CASE_INSENSITIVE else stem


# ------------------------------------------------------------------ #
#  Label index                                                         #
# ------------------------------------------------------------------ #

class LabelIndex:
    """In-memory set of label-file stems for one output directory."""

    def __init__(self, output_dir: str = ""):
        self._dir: Optional[Path] = Path(output_dir) if output_dir else None
        self._stems: Set[str] = set()
        self._loaded = False

    # ---- Directory ------------------------------------------------------

    def set_directory(self, output_dir: str) -> None:
        new = Path(output_dir) if output_dir else None
        if new != self._dir:
            self._dir = new
            self._stems.clear()
            self._loaded = False

    @property
    def directory(self) -> str:
        return str(self._dir) if self._dir else ""

    # ---- Scan -----------------------------------------------------------

    def rebuild(self) -> int:
        """One os.scandir pass. Returns how many label files were found."""
        self._stems.clear()
        self._loaded = True
        if not self._dir:
            return 0
        try:
            with os.scandir(self._dir) as entries:
                for entry in entries:
                    if not entry.is_file():
                        continue
                    stem, ext = os.path.splitext(entry.name)
                    if ext.lower() in LABEL_SUFFIXES and stem != Path(FLAGS_FILENAME).stem:
                        self._stems.add(_key(stem))
        except OSError:
            pass
        return len(self._stems)

    def ensure_loaded(self) -> None:
        if not self._loaded:
            self.rebuild()

    # ---- Queries / updates ----------------------------------------------

    def has(self, image_path: str) -> bool:
        self.ensure_loaded()
        return _key(Path(image_path).stem) in self._stems

    def mark(self, image_path: str) -> None:
        self.ensure_loaded()
        self._stems.add(_key(Path(image_path).stem))

    def unmark(self, image_path: str) -> None:
        self.ensure_loaded()
        self._stems.discard(_key(Path(image_path).stem))

    def count_done(self, image_paths: Iterable[str]) -> int:
        self.ensure_loaded()
        return sum(1 for p in image_paths if _key(Path(p).stem) in self._stems)

    @property
    def size(self) -> int:
        return len(self._stems)


# ------------------------------------------------------------------ #
#  Review flags                                                        #
# ------------------------------------------------------------------ #

class ReviewFlags:
    """
    "Come back to this one" markers, stored beside the label files so they
    survive a restart and travel with the output folder.
    """

    def __init__(self, output_dir: str = ""):
        self._dir: Optional[Path] = Path(output_dir) if output_dir else None
        self._names: Set[str] = set()
        self._loaded = False

    # ---- Directory ------------------------------------------------------

    def set_directory(self, output_dir: str) -> None:
        new = Path(output_dir) if output_dir else None
        if new != self._dir:
            self._dir = new
            self._names.clear()
            self._loaded = False

    def _path(self) -> Optional[Path]:
        return (self._dir / FLAGS_FILENAME) if self._dir else None

    # ---- Load / save ----------------------------------------------------

    def load(self) -> None:
        self._loaded = True
        self._names.clear()
        path = self._path()
        if not path or not path.exists():
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                self._names = {str(n) for n in data}
        except (OSError, json.JSONDecodeError):
            pass

    def ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def save(self) -> bool:
        path = self._path()
        if not path:
            return False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(sorted(self._names), fh, indent=2, ensure_ascii=False)
            tmp.replace(path)
            return True
        except OSError:
            return False

    # ---- Queries / updates ----------------------------------------------

    def is_flagged(self, image_path: str) -> bool:
        self.ensure_loaded()
        return Path(image_path).name in self._names

    def set_flagged(self, image_path: str, flagged: bool) -> bool:
        """Returns True when the state actually changed."""
        self.ensure_loaded()
        name = Path(image_path).name
        if flagged and name not in self._names:
            self._names.add(name)
        elif not flagged and name in self._names:
            self._names.discard(name)
        else:
            return False
        self.save()
        return True

    def toggle(self, image_path: str) -> bool:
        """Flip the flag. Returns the new state."""
        new_state = not self.is_flagged(image_path)
        self.set_flagged(image_path, new_state)
        return new_state

    def flagged_names(self) -> List[str]:
        self.ensure_loaded()
        return sorted(self._names)

    @property
    def count(self) -> int:
        self.ensure_loaded()
        return len(self._names)
