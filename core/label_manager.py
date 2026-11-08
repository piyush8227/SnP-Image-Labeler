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
SnP Image Labeler - Label Manager
Handles reading and writing of label files (CSV / JSON) with:
  - Atomic writes  (write-to-temp → rename)
  - Upsert logic   (edit existing file; never create duplicates)
  - Format-agnostic load (tries preferred format first, then the other)
  - Safe serialisation of list/dict values inside CSV
"""

import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional


class LabelManager:
    """Manages label file I/O for SnP Image Labeler."""

    SUPPORTED_FORMATS = ("csv", "json")

    def __init__(self, output_dir: str):
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    #  Directory                                                           #
    # ------------------------------------------------------------------ #

    def set_output_directory(self, path: str) -> None:
        self._output_dir = Path(path)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    @property
    def output_directory(self) -> str:
        return str(self._output_dir)

    # ------------------------------------------------------------------ #
    #  Path helpers                                                        #
    # ------------------------------------------------------------------ #

    def get_label_path(self, image_path: str, fmt: str) -> Path:
        stem = Path(image_path).stem
        return self._output_dir / f"{stem}.{fmt}"

    def has_label(self, image_path: str) -> bool:
        """Return True if any label file exists for this image."""
        return any(
            self.get_label_path(image_path, fmt).exists()
            for fmt in self.SUPPORTED_FORMATS
        )

    # ------------------------------------------------------------------ #
    #  Load                                                                #
    # ------------------------------------------------------------------ #

    def load_label(
        self, image_path: str, preferred_fmt: str = "csv"
    ) -> Optional[Dict[str, Any]]:
        """Load label data, trying preferred format first."""
        order = [preferred_fmt] + [f for f in self.SUPPORTED_FORMATS if f != preferred_fmt]
        for fmt in order:
            path = self.get_label_path(image_path, fmt)
            if path.exists():
                try:
                    return self._read(path, fmt)
                except Exception as exc:
                    print(f"[LabelManager] Load error ({path.name}): {exc}")
        return None

    def _read(self, path: Path, fmt: str) -> Dict[str, Any]:
        if fmt == "csv":
            with open(path, "r", newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            if not rows:
                return {}
            result: Dict[str, Any] = {}
            for k, v in rows[0].items():
                # Lists / dicts were serialised as JSON strings
                try:
                    parsed = json.loads(v)
                    result[k] = parsed
                except (json.JSONDecodeError, TypeError):
                    result[k] = v
            return result

        if fmt == "json":
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)

        raise ValueError(f"Unsupported format: {fmt}")

    # ------------------------------------------------------------------ #
    #  Save (upsert — create or overwrite)                                 #
    # ------------------------------------------------------------------ #

    def save_label(
        self, image_path: str, labels: Dict[str, Any], fmt: str
    ) -> bool:
        """Atomically save (create or update) a label file."""
        self._output_dir.mkdir(parents=True, exist_ok=True)
        dest = self.get_label_path(image_path, fmt)
        tmp = dest.with_suffix(".tmp")
        try:
            if fmt == "csv":
                self._write_csv(tmp, labels)
            elif fmt == "json":
                self._write_json(tmp, labels)
            else:
                raise ValueError(f"Unsupported format: {fmt}")
            tmp.replace(dest)
            return True
        except Exception as exc:
            print(f"[LabelManager] Save error: {exc}")
            tmp.unlink(missing_ok=True)
            return False

    def save_label_to_path(
        self, dest_path: str, labels: Dict[str, Any], fmt: str
    ) -> bool:
        """Save label to an arbitrary path (used by Export dialog)."""
        dest = Path(dest_path)
        tmp = dest.with_suffix(".tmp")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if fmt == "csv":
                self._write_csv(tmp, labels)
            elif fmt == "json":
                self._write_json(tmp, labels)
            else:
                raise ValueError(f"Unsupported format: {fmt}")
            tmp.replace(dest)
            return True
        except Exception as exc:
            print(f"[LabelManager] Export error: {exc}")
            tmp.unlink(missing_ok=True)
            return False

    # ------------------------------------------------------------------ #
    #  Internal writers                                                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _write_csv(path: Path, labels: Dict[str, Any]) -> None:
        serialised: Dict[str, str] = {}
        for k, v in labels.items():
            if isinstance(v, (list, dict)):
                serialised[k] = json.dumps(v, ensure_ascii=False)
            else:
                serialised[k] = str(v) if v is not None else ""
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=serialised.keys())
            writer.writeheader()
            writer.writerow(serialised)

    @staticmethod
    def _write_json(path: Path, labels: Dict[str, Any]) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(labels, fh, indent=2, ensure_ascii=False)

    # ------------------------------------------------------------------ #
    #  Delete                                                              #
    # ------------------------------------------------------------------ #

    def delete_label(self, image_path: str) -> bool:
        deleted = False
        for fmt in self.SUPPORTED_FORMATS:
            p = self.get_label_path(image_path, fmt)
            if p.exists():
                try:
                    p.unlink()
                    deleted = True
                except OSError as exc:
                    print(f"[LabelManager] Delete error: {exc}")
        return deleted
