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
SnP Image Labeler - Session Store

Remembers the working queue and the row you were on so closing the app in
the middle of a 5,000 image batch does not mean re-importing the folder and
hunting for your place.

Kept out of config.json on purpose: a 10,000 path queue is ~1 MB of JSON and
config.json is rewritten on every small setting change.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

SESSION_FILE = Path.home() / ".snp_labeler" / "session.json"
MAX_PATHS = 200_000


class SessionStore:
    """Atomic save/load of the last working session."""

    def __init__(self, path: Path = SESSION_FILE):
        self._path = Path(path)

    # ---- Save -----------------------------------------------------------

    def save(self, paths: List[str], index: int, task: str) -> bool:
        payload: Dict[str, Any] = {
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "task":     task,
            "index":    int(index),
            "queue":    [str(p) for p in paths[:MAX_PATHS]],
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            tmp.replace(self._path)
            return True
        except OSError:
            return False

    # ---- Load -----------------------------------------------------------

    def load(self) -> Dict[str, Any]:
        """Returns {} when there is nothing usable to resume."""
        if not self._path.exists():
            return {}
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        queue = data.get("queue")
        if not isinstance(queue, list) or not queue:
            return {}
        return {
            "queue":    [str(p) for p in queue],
            "index":    int(data.get("index", 0) or 0),
            "task":     str(data.get("task", "annotation")),
            "saved_at": str(data.get("saved_at", "")),
        }

    def existing(self, paths: List[str]) -> List[str]:
        """Filter a restored queue down to files that are still on disk."""
        return [p for p in paths if p and os.path.isfile(p)]

    def clear(self) -> None:
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass
