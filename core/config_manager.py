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
SnP Image Labeler - Config Manager
Handles persistent application configuration with atomic writes and corruption recovery.
"""

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional


# Bumped when a saved config needs rewriting rather than just new keys merged in.
#   2 - annotation moved from typed telemetry fields to a label list, and from
#       one box per image to many, so a stored max of 1 is no longer a default.
CONFIG_VERSION = 2


class ConfigManager:
    """
    Configuration manager with:
    - Atomic writes  (write-to-temp → rename)
    - Backup always mirrors latest save (copy AFTER rename, not before)
    - Corruption recovery (falls back to backup, then defaults)
    - Auto-merge of new default keys
    """

    APP_DIR = Path.home() / ".snp_labeler"
    CONFIG_FILE = APP_DIR / "config.json"
    CONFIG_BACKUP = APP_DIR / "config.backup.json"

    DEFAULT_CONFIG: Dict[str, Any] = {
        "telemetry_fields": [],
        "output_directory": "",
        "export_format": "csv",
        "last_import_directory": "",
        "window_geometry": None,
        # ── Task switch ────────────────────────────────────────────────
        "active_task": "annotation",          # "annotation" | "segregation"
        # ── Segregation task ───────────────────────────────────────────
        "segregation_output_directory": "",   # dataset root
        "segregation_classes": [],            # user-defined class names
        "segregation_last_split": "train",
        "segregation_auto_split": False,
        "segregation_ratios": {"train": 70, "test": 20, "val": 10},
        "segregation_sticky_selection": False,
        # ── Duplicates / rejected images ───────────────────────────────
        "duplicate_sensitivity": 1,           # index into duplicate_finder.SENSITIVITY
        "rejected_directory": "",             # blank -> dataset root, else image folder
        # ── Annotation task — viewer & labelling behaviour ─────────────
        "zoom_locked": False,                 # keep zoom/pan across images
        "annotation_sticky_label": False,     # reuse last label, skip the dialog
        "annotation_recent_labels": [],       # quick-pick list (most recent first)
        "annotation_max_boxes": 0,            # boxes per image (0 = unlimited)
        "annotation_labels": [],              # the label (class) list for boxes
        "image_cache_mb": 384,                # full-res pixmap cache budget
        "resume_session": True,               # offer to restore the last queue
        # -- Appearance -------------------------------------------------
        "ui_theme": "light",                  # "light" | "dark"
        "ui_scale": 1.0,                      # 0.5 - 4.0; scales text and spacing
        # config_version is deliberately NOT defaulted here: _load_config merges
        # defaults under the saved file, so a default would hide every old config
        # from _migrate. It is stamped by _migrate instead.
    }

    def __init__(self):
        self.APP_DIR.mkdir(parents=True, exist_ok=True)
        self._config: Dict[str, Any] = self._load_config()
        self._migrate()

    def _migrate(self) -> None:
        """
        Bring a config written by an older build up to CONFIG_VERSION.

        Only values whose meaning changed are touched; everything the user
        deliberately set is left alone.
        """
        try:
            version = int(self._config.get("config_version", 1))
        except (TypeError, ValueError):
            version = 1
        if version >= CONFIG_VERSION:
            return

        if version < 2:
            # "1 box per image" was the old default, not a considered choice.
            # Annotation now takes several labelled boxes, so release the cap.
            if int(self._config.get("annotation_max_boxes", 1) or 1) <= 1:
                self._config["annotation_max_boxes"] = 0
            # Field names become the starting label list (see annotation_labels).
            if not self._config.get("annotation_labels"):
                legacy = self._config.get("telemetry_fields") or []
                names = [str(f.get("name", "")).strip() for f in legacy
                         if isinstance(f, dict) and str(f.get("name", "")).strip()]
                if names:
                    self._config["annotation_labels"] = names

        self._config["config_version"] = CONFIG_VERSION
        self.save()

    # ------------------------------------------------------------------ #
    #  Internal                                                            #
    # ------------------------------------------------------------------ #

    def _load_config(self) -> Dict[str, Any]:
        """Load config, falling back to backup then defaults on failure."""
        for candidate in (self.CONFIG_FILE, self.CONFIG_BACKUP):
            if candidate.exists():
                try:
                    with open(candidate, "r", encoding="utf-8") as fh:
                        data = json.load(fh)
                    merged = dict(self.DEFAULT_CONFIG)
                    merged.update(data)
                    return merged
                except (json.JSONDecodeError, OSError):
                    continue
        return dict(self.DEFAULT_CONFIG)

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def save(self) -> bool:
        """
        Atomically persist config to disk.

        Write order:
          1. Write new state to .tmp   (crash here -> both files untouched)
          2. Rename .tmp -> CONFIG_FILE (atomic on all OSes)
          3. Copy CONFIG_FILE -> backup (backup always = latest successful save)
        """
        tmp = self.CONFIG_FILE.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._config, fh, indent=2, ensure_ascii=False)
            tmp.replace(self.CONFIG_FILE)                         # atomic commit
            shutil.copy2(self.CONFIG_FILE, self.CONFIG_BACKUP)    # mirror to backup
            return True
        except OSError as exc:
            print(f"[ConfigManager] Save failed: {exc}")
            tmp.unlink(missing_ok=True)
            return False

    def get(self, key: str, default: Any = None) -> Any:
        return self._config.get(key, default)

    def set(self, key: str, value: Any, persist: bool = True) -> None:
        """persist=False keeps it in memory only — the caller saves later."""
        if self._config.get(key) == value and persist:
            return                      # no disk write for a no-op change
        self._config[key] = value
        if persist:
            self.save()

    # ------------------------------------------------------------------ #
    #  Annotation task — viewer & labelling behaviour                      #
    # ------------------------------------------------------------------ #

    @property
    def zoom_locked(self) -> bool:
        return bool(self._config.get("zoom_locked", False))

    @zoom_locked.setter
    def zoom_locked(self, locked: bool) -> None:
        self.set("zoom_locked", bool(locked))

    @property
    def annotation_sticky_label(self) -> bool:
        return bool(self._config.get("annotation_sticky_label", False))

    @annotation_sticky_label.setter
    def annotation_sticky_label(self, enabled: bool) -> None:
        self.set("annotation_sticky_label", bool(enabled))

    @property
    def annotation_recent_labels(self) -> List[str]:
        val = self._config.get("annotation_recent_labels", [])
        return [str(v) for v in val] if isinstance(val, list) else []

    @annotation_recent_labels.setter
    def annotation_recent_labels(self, labels: List[str]) -> None:
        self.set("annotation_recent_labels", [str(l) for l in labels][:12])

    @property
    def annotation_max_boxes(self) -> int:
        """Boxes allowed per image; 0 means no limit."""
        try:
            return max(0, int(self._config.get("annotation_max_boxes", 0)))
        except (TypeError, ValueError):
            return 0

    @property
    def annotation_labels(self) -> List[str]:
        """
        The label (class) list offered for bounding boxes.

        Migrates the old typed telemetry fields on first run: their names become
        labels, so an existing config does not come up empty.
        """
        labels = self._config.get("annotation_labels")
        if labels:
            return [str(l) for l in labels]
        legacy = self._config.get("telemetry_fields") or []
        return [str(f.get("name", "")).strip() for f in legacy
                if isinstance(f, dict) and str(f.get("name", "")).strip()]

    @annotation_labels.setter
    def annotation_labels(self, labels: List[str]) -> None:
        seen, clean = set(), []
        for label in labels or []:
            text = str(label).strip()
            if text and text.lower() not in seen:
                seen.add(text.lower())
                clean.append(text)
        self.set("annotation_labels", clean)

    @property
    def ui_theme(self) -> str:
        value = str(self._config.get("ui_theme", "light")).lower()
        return value if value in ("light", "dark") else "light"

    @ui_theme.setter
    def ui_theme(self, mode: str) -> None:
        self.set("ui_theme", "dark" if str(mode).lower() == "dark" else "light")

    @property
    def ui_scale(self) -> float:
        try:
            return max(0.5, min(4.0, float(self._config.get("ui_scale", 1.0))))
        except (TypeError, ValueError):
            return 1.0

    @ui_scale.setter
    def ui_scale(self, scale: float) -> None:
        try:
            value = max(0.5, min(4.0, float(scale)))
        except (TypeError, ValueError):
            value = 1.0
        self.set("ui_scale", round(value, 4))

    @property
    def image_cache_mb(self) -> int:
        try:
            return max(64, int(self._config.get("image_cache_mb", 384)))
        except (TypeError, ValueError):
            return 384

    @property
    def resume_session(self) -> bool:
        return bool(self._config.get("resume_session", True))

    @resume_session.setter
    def resume_session(self, enabled: bool) -> None:
        self.set("resume_session", bool(enabled))

    # ------------------------------------------------------------------ #
    #  Typed Properties                                                    #
    # ------------------------------------------------------------------ #

    @property
    def telemetry_fields(self) -> List[Dict]:
        return self._config.get("telemetry_fields", [])

    @telemetry_fields.setter
    def telemetry_fields(self, fields: List[Dict]) -> None:
        self._config["telemetry_fields"] = fields
        self.save()

    @property
    def output_directory(self) -> str:
        val = self._config.get("output_directory", "")
        return val if val else str(Path.home())

    @output_directory.setter
    def output_directory(self, path: str) -> None:
        self._config["output_directory"] = str(path)
        self.save()

    @property
    def export_format(self) -> str:
        return self._config.get("export_format", "csv")

    @export_format.setter
    def export_format(self, fmt: str) -> None:
        self._config["export_format"] = fmt.lower()
        self.save()

    @property
    def last_import_directory(self) -> str:
        val = self._config.get("last_import_directory", "")
        return val if val else str(Path.home())

    @last_import_directory.setter
    def last_import_directory(self, path: str) -> None:
        self._config["last_import_directory"] = str(path)
        self.save()

    # ------------------------------------------------------------------ #
    #  Task switch                                                         #
    # ------------------------------------------------------------------ #

    @property
    def active_task(self) -> str:
        task = self._config.get("active_task", "annotation")
        return task if task in ("annotation", "segregation") else "annotation"

    @active_task.setter
    def active_task(self, task: str) -> None:
        self._config["active_task"] = task
        self.save()

    # ------------------------------------------------------------------ #
    #  Segregation task                                                    #
    # ------------------------------------------------------------------ #

    @property
    def segregation_output_directory(self) -> str:
        val = self._config.get("segregation_output_directory", "")
        return val if val else str(Path.home())

    @segregation_output_directory.setter
    def segregation_output_directory(self, path: str) -> None:
        self._config["segregation_output_directory"] = str(path)
        self.save()

    @property
    def segregation_classes(self) -> List[str]:
        val = self._config.get("segregation_classes", [])
        return list(val) if isinstance(val, list) else []

    @segregation_classes.setter
    def segregation_classes(self, classes: List[str]) -> None:
        self._config["segregation_classes"] = list(classes)
        self.save()

    @property
    def segregation_last_split(self) -> str:
        val = self._config.get("segregation_last_split", "train")
        return val if val in ("train", "test", "val") else "train"

    @segregation_last_split.setter
    def segregation_last_split(self, split: str) -> None:
        self._config["segregation_last_split"] = split
        self.save()

    @property
    def segregation_auto_split(self) -> bool:
        return bool(self._config.get("segregation_auto_split", False))

    @segregation_auto_split.setter
    def segregation_auto_split(self, enabled: bool) -> None:
        self._config["segregation_auto_split"] = bool(enabled)
        self.save()

    @property
    def segregation_ratios(self) -> Dict[str, int]:
        val = self._config.get("segregation_ratios", {})
        base = {"train": 70, "test": 20, "val": 10}
        if isinstance(val, dict):
            for k in base:
                try:
                    base[k] = int(val.get(k, base[k]))
                except (TypeError, ValueError):
                    pass
        return base

    @segregation_ratios.setter
    def segregation_ratios(self, ratios: Dict[str, int]) -> None:
        self._config["segregation_ratios"] = dict(ratios)
        self.save()

    @property
    def segregation_sticky_selection(self) -> bool:
        return bool(self._config.get("segregation_sticky_selection", False))

    @segregation_sticky_selection.setter
    def segregation_sticky_selection(self, enabled: bool) -> None:
        self._config["segregation_sticky_selection"] = bool(enabled)
        self.save()
