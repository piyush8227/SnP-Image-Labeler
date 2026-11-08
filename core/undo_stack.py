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
SnP Image Labeler - Undo Stack

Snapshot undo for the annotation state of one image.  A snapshot is just the
list of box dicts, which is small, JSON-safe and trivially correct - there
are no per-operation inverses to get wrong when a box is drawn, moved,
resized, relabelled, deleted or cleared.

The stack is reset every time a different image is loaded: Ctrl+Z undoes
work on the picture in front of you, never a navigation.
"""

import copy
from typing import Any, Dict, List, Optional

DEFAULT_LIMIT = 60

State = List[Dict[str, Any]]


class UndoStack:
    def __init__(self, limit: int = DEFAULT_LIMIT):
        self._limit = max(2, int(limit))
        self._states: List[State] = [[]]
        self._pos = 0

    # ---- Lifecycle ------------------------------------------------------

    def reset(self, state: Optional[State] = None) -> None:
        """Start fresh with `state` as the baseline (no undo available)."""
        self._states = [copy.deepcopy(state) if state else []]
        self._pos = 0

    def push(self, state: State) -> None:
        """Record a committed change. Drops any redo branch."""
        snapshot = copy.deepcopy(state) if state else []
        if snapshot == self._states[self._pos]:
            return                                  # nothing actually changed
        del self._states[self._pos + 1:]
        self._states.append(snapshot)
        if len(self._states) > self._limit:
            self._states.pop(0)
        self._pos = len(self._states) - 1

    # ---- Undo / redo ----------------------------------------------------

    def undo(self) -> Optional[State]:
        if self._pos <= 0:
            return None
        self._pos -= 1
        return copy.deepcopy(self._states[self._pos])

    def redo(self) -> Optional[State]:
        if self._pos >= len(self._states) - 1:
            return None
        self._pos += 1
        return copy.deepcopy(self._states[self._pos])

    def current(self) -> State:
        return copy.deepcopy(self._states[self._pos])

    @property
    def can_undo(self) -> bool:
        return self._pos > 0

    @property
    def can_redo(self) -> bool:
        return self._pos < len(self._states) - 1

    @property
    def depth(self) -> int:
        return len(self._states)
