"""
SnP Image Labeler - Annotation Manager
Saves and loads bounding box annotations per image.
File format: {stem}_boxes.json  in output directory.
"""

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional


class AnnotationManager:
    """Atomic save/load of bounding-box annotation files."""

    def __init__(self, output_dir: str):
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def set_output_directory(self, path: str) -> None:
        self._output_dir = Path(path)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    @property
    def output_directory(self) -> str:
        return str(self._output_dir)

    # ------------------------------------------------------------------ #
    #  Path helpers                                                        #
    # ------------------------------------------------------------------ #

    def get_annotation_path(self, image_path: str) -> Path:
        stem = Path(image_path).stem
        return self._output_dir / f"{stem}_boxes.json"

    def has_annotations(self, image_path: str) -> bool:
        p = self.get_annotation_path(image_path)
        return p.exists() and p.stat().st_size > 10

    # ------------------------------------------------------------------ #
    #  Load                                                                #
    # ------------------------------------------------------------------ #

    def load(self, image_path: str) -> List[Dict[str, Any]]:
        """Return list of box dicts, or [] if none saved."""
        path = self.get_annotation_path(image_path)
        if not path.exists():
            return []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, list) else []
        except Exception as exc:
            print(f"[AnnotationManager] Load error: {exc}")
            return []

    # ------------------------------------------------------------------ #
    #  Save                                                                #
    # ------------------------------------------------------------------ #

    def save(self, image_path: str, boxes: List[Dict[str, Any]]) -> bool:
        """Atomically save box list. Deletes file if boxes is empty."""
        dest = self.get_annotation_path(image_path)

        # If no boxes, remove file (keep output dir clean)
        if not boxes:
            dest.unlink(missing_ok=True)
            return True

        self._output_dir.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(boxes, fh, indent=2, ensure_ascii=False)
            tmp.replace(dest)
            return True
        except Exception as exc:
            print(f"[AnnotationManager] Save error: {exc}")
            tmp.unlink(missing_ok=True)
            return False

    # ------------------------------------------------------------------ #
    #  Delete                                                              #
    # ------------------------------------------------------------------ #

    def delete(self, image_path: str) -> bool:
        p = self.get_annotation_path(image_path)
        if p.exists():
            try:
                p.unlink()
                return True
            except OSError:
                pass
        return False
