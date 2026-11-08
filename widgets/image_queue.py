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
SnP Image Labeler - Image Queue Panel

Built for long queues:
  - thumbnails are requested only for the rows in (and just around) the
    viewport, decoded scaled on a small thread pool and cached on disk
  - row lookup is a dict, not a scan of every item, so delivering a
    thumbnail or marking an image done is O(1) instead of O(n)
  - the done/pending/flagged counters are kept incrementally
  - three states, not two: pending, done, and "flagged for review"
"""

import os
from typing import Callable, Dict, List, Optional

from PyQt5.QtCore import QPoint, QSize, Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QColor, QIcon, QImage, QPixmap
from PyQt5.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QListView, QListWidget,
    QListWidgetItem, QMenu, QPushButton, QVBoxLayout, QWidget,
)

from core import theme

from core.thumbnail_cache import ThumbnailService

# Row states
PENDING = 0
DONE    = 1
FLAGGED = 2

FILTER_ALL     = "All"
FILTER_PENDING = "Pending"
FILTER_DONE    = "Done"
FILTER_FLAGGED = "Flagged"

THUMB_SIZE   = 56
VIEW_MARGIN  = 12          # rows fetched above/below the viewport


# ------------------------------------------------------------------ #
#  Single queue item                                                   #
# ------------------------------------------------------------------ #

class ImageQueueItem(QListWidgetItem):
    def __init__(self, image_path: str, state: int = PENDING,
                 done_word: str = "Labeled", pending_word: str = "Pending"):
        super().__init__()
        self.image_path    = image_path
        self.orig_size: Optional[QSize] = None
        self._state        = state
        self._done_word    = done_word
        self._pending_word = pending_word
        self._name         = os.path.basename(image_path)
        self.setSizeHint(QSize(190, 68))
        self._refresh_text()

    def set_status_words(self, done_word: str, pending_word: str):
        self._done_word    = done_word
        self._pending_word = pending_word
        self._refresh_text()

    def _refresh_text(self):
        name = self._name
        if len(name) > 22:
            name = name[:19] + "..."
        if self._state == DONE:
            status, color = f"✓ {self._done_word}", "#a6e3a1"
        elif self._state == FLAGGED:
            status, color = "⚑ Review", "#f9e2af"
        else:
            status, color = f"○ {self._pending_word}", "#a6adc8"
        self.setText(f"{name}\n{status}")
        self.setForeground(QColor(color))
        self.setToolTip(self.image_path)

    def set_thumbnail(self, image: QImage):
        self.setIcon(QIcon(QPixmap.fromImage(image)))

    def set_state(self, state: int) -> bool:
        if self._state == state:
            return False
        self._state = state
        self._refresh_text()
        return True

    @property
    def state(self) -> int:
        return self._state

    @property
    def is_labeled(self) -> bool:
        return self._state == DONE

    @property
    def is_flagged(self) -> bool:
        return self._state == FLAGGED


# ------------------------------------------------------------------ #
#  Panel widget                                                        #
# ------------------------------------------------------------------ #

class ImageQueuePanel(QWidget):
    image_selected      = pyqtSignal(str, int)   # (path, index)
    remove_requested    = pyqtSignal(str)
    delete_requested    = pyqtSignal(str)
    flag_requested      = pyqtSignal(str)
    clear_all_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image_paths: List[str] = []
        self._rows: Dict[str, ImageQueueItem] = {}
        self._done_word    = "Labeled"
        self._pending_word = "Pending"
        self._filter       = FILTER_ALL
        self._n_done       = 0
        self._n_flagged    = 0

        self._thumbs = ThumbnailService(THUMB_SIZE, "queue", self)
        self._thumbs.thumbnail_ready.connect(self._on_thumbnail_ready)

        self._thumb_timer = QTimer(self)
        self._thumb_timer.setSingleShot(True)
        self._thumb_timer.timeout.connect(self._request_visible_thumbnails)

        self._setup_ui()

    # ------------------------------------------------------------------ #
    #  UI                                                                  #
    # ------------------------------------------------------------------ #

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        hdr_frame = QFrame()
        hdr_frame.setObjectName("queue_header_frame")
        hdr_layout = QHBoxLayout(hdr_frame)
        hdr_layout.setContentsMargins(8, 6, 4, 6)
        hdr_layout.setSpacing(4)

        hdr_lbl = QLabel("Image Queue")
        hdr_lbl.setObjectName("panel_header_inline")
        hdr_layout.addWidget(hdr_lbl, 1)

        self._btn_clear = QPushButton("Clear")
        self._btn_clear.setObjectName("queue_clear_btn")
        self._btn_clear.setMinimumHeight(theme.px(22))
        self._btn_clear.setToolTip("Clear entire image queue")
        self._btn_clear.setEnabled(False)
        self._btn_clear.clicked.connect(lambda _: self.clear_all_requested.emit())
        hdr_layout.addWidget(self._btn_clear)
        layout.addWidget(hdr_frame)

        count_frame = QFrame()
        count_frame.setObjectName("count_frame")
        cf = QHBoxLayout(count_frame)
        cf.setContentsMargins(6, 2, 4, 2)
        cf.setSpacing(4)

        self._count_label = QLabel("0 images")
        self._count_label.setObjectName("count_label")
        self._count_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        cf.addWidget(self._count_label, 1)

        self._filter_combo = QComboBox()
        self._filter_combo.setObjectName("queue_filter_combo")
        self._filter_combo.addItems(
            [FILTER_ALL, FILTER_PENDING, FILTER_DONE, FILTER_FLAGGED]
        )
        self._filter_combo.setMinimumHeight(theme.px(22))
        self._filter_combo.setToolTip("Show only rows in this state")
        self._filter_combo.currentTextChanged.connect(self._on_filter_changed)
        cf.addWidget(self._filter_combo)
        layout.addWidget(count_frame)

        self._list = QListWidget()
        self._list.setObjectName("image_list")
        self._list.setIconSize(QSize(theme.px(THUMB_SIZE), theme.px(THUMB_SIZE)))
        self._list.setSpacing(1)
        # Uniform rows are what make a 50k queue cheap: the view derives every
        # row's position arithmetically instead of measuring each one, so a
        # single-pass layout is already O(1) in geometry work.
        #
        # Batched layout must NOT be used with it. Batched lays the view out
        # 'batch size' items at a time on a timer, and anything that
        # invalidates the layout - marking a row done, a status refresh -
        # restarts it from item 0. While it catches up the scrollbar range
        # collapses to a single batch, which threw the queue back to the top
        # mid-session and made the lower rows unreachable: dragging past the
        # first batch simply snapped back.
        self._list.setUniformItemSizes(True)
        self._list.setLayoutMode(QListView.SinglePass)
        self._list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self._list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._list.currentRowChanged.connect(self._on_row_changed)
        self._list.customContextMenuRequested.connect(self._show_context_menu)
        self._list.verticalScrollBar().valueChanged.connect(
            lambda _: self._thumb_timer.start(50)
        )
        layout.addWidget(self._list)

    # ------------------------------------------------------------------ #
    #  Context menu                                                        #
    # ------------------------------------------------------------------ #

    def _show_context_menu(self, pos):
        item = self._list.itemAt(pos)
        if not isinstance(item, ImageQueueItem):
            return
        menu = QMenu(self)
        act_flag = menu.addAction(
            "Unflag" if item.is_flagged else "Flag for Review"
        )
        act_flag.setToolTip("Mark it to come back to later  (Ctrl+F)")
        menu.addSeparator()
        act_remove = menu.addAction("Remove from Queue")
        act_remove.setToolTip("Take it out of this session. The file is not touched.")
        act_delete = menu.addAction("Delete Image…")
        act_delete.setToolTip(
            "Move the file into the _rejected folder and drop it from the dataset."
        )
        chosen = menu.exec_(self._list.mapToGlobal(pos))
        if chosen == act_flag:
            self.flag_requested.emit(item.image_path)
        elif chosen == act_remove:
            self.remove_requested.emit(item.image_path)
        elif chosen == act_delete:
            self.delete_requested.emit(item.image_path)

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def load_images(self, paths: List[str],
                    state_checker: Optional[Callable[[str], int]] = None):
        self._thumbs.cancel_pending()
        self._list.clear()
        self._rows.clear()
        self._image_paths = list(paths)
        self._n_done = self._n_flagged = 0
        self._append_items(paths, state_checker)
        self._apply_filter()
        self._update_count()
        self._btn_clear.setEnabled(bool(paths))
        self._thumb_timer.start(0)

    def add_images(self, paths: List[str],
                   state_checker: Optional[Callable[[str], int]] = None):
        new = [p for p in paths if p not in self._rows]
        self._image_paths.extend(new)
        self._append_items(new, state_checker)
        self._apply_filter()
        self._update_count()
        self._btn_clear.setEnabled(True)
        self._thumb_timer.start(0)

    def _append_items(self, paths: List[str],
                      state_checker: Optional[Callable[[str], int]]):
        self._list.setUpdatesEnabled(False)
        try:
            for path in paths:
                state = state_checker(path) if state_checker else PENDING
                item = ImageQueueItem(path, state, self._done_word,
                                      self._pending_word)
                self._list.addItem(item)
                self._rows[path] = item
                if state == DONE:
                    self._n_done += 1
                elif state == FLAGGED:
                    self._n_flagged += 1
        finally:
            self._list.setUpdatesEnabled(True)

    def remove_image(self, path: str) -> int:
        """Remove one image. Returns the row index removed, or -1."""
        item = self._rows.pop(path, None)
        if item is None:
            return -1
        row = self._list.row(item)
        if item.state == DONE:
            self._n_done -= 1
        elif item.state == FLAGGED:
            self._n_flagged -= 1
        self._list.takeItem(row)
        try:
            self._image_paths.remove(path)
        except ValueError:
            pass
        self._thumbs.forget(path)
        self._update_count()
        self._btn_clear.setEnabled(bool(self._image_paths))
        return row

    def mark_labeled(self, path: str, labeled: bool = True):
        self.set_state(path, DONE if labeled else PENDING)

    def set_flagged(self, path: str, flagged: bool):
        item = self._rows.get(path)
        if item is None:
            return
        if flagged:
            self.set_state(path, FLAGGED)
        elif item.state == FLAGGED:
            self.set_state(path, PENDING)

    def set_state(self, path: str, state: int):
        item = self._rows.get(path)
        if item is None:
            return
        old = item.state
        if not item.set_state(state):
            return
        if old == DONE:
            self._n_done -= 1
        elif old == FLAGGED:
            self._n_flagged -= 1
        if state == DONE:
            self._n_done += 1
        elif state == FLAGGED:
            self._n_flagged += 1
        # Keep the row visible while it is the current one — hiding the row
        # under the cursor is jarring. (Compared by identity: QListWidget.row()
        # is a linear search and this runs on every save.)
        if self._filter != FILTER_ALL and self._list.currentItem() is not item:
            item.setHidden(not self._matches_filter(item))
        self._update_count()

    def state_of(self, path: str) -> int:
        item = self._rows.get(path)
        return item.state if item else PENDING

    # ------------------------------------------------------------------ #
    #  Task-aware wording / bulk refresh                                   #
    # ------------------------------------------------------------------ #

    def set_status_labels(self, done_word: str, pending_word: str = "Pending"):
        self._done_word    = done_word
        self._pending_word = pending_word
        for item in self._rows.values():
            item.set_status_words(done_word, pending_word)
        self._update_count()

    def refresh_status(self, checker: Optional[Callable[[str], int]] = None):
        """Re-evaluate every row. `checker` returns PENDING/DONE/FLAGGED."""
        self._n_done = self._n_flagged = 0
        self._list.setUpdatesEnabled(False)
        try:
            for item in self._rows.values():
                state = checker(item.image_path) if checker else PENDING
                item.set_state(state)
                if state == DONE:
                    self._n_done += 1
                elif state == FLAGGED:
                    self._n_flagged += 1
        finally:
            self._list.setUpdatesEnabled(True)
        self._apply_filter()
        self._update_count()

    # ------------------------------------------------------------------ #
    #  Filtering                                                           #
    # ------------------------------------------------------------------ #

    def _on_filter_changed(self, text: str):
        self._filter = text
        self._apply_filter()
        self._thumb_timer.start(0)

    def _matches_filter(self, item: ImageQueueItem) -> bool:
        if self._filter == FILTER_ALL:
            return True
        if self._filter == FILTER_PENDING:
            return item.state == PENDING
        if self._filter == FILTER_DONE:
            return item.state == DONE
        if self._filter == FILTER_FLAGGED:
            return item.state == FLAGGED
        return True

    def _apply_filter(self):
        current = self._list.currentRow()
        self._list.setUpdatesEnabled(False)
        try:
            for i in range(self._list.count()):
                item = self._list.item(i)
                if not isinstance(item, ImageQueueItem):
                    continue
                item.setHidden(not (self._matches_filter(item) or i == current))
        finally:
            self._list.setUpdatesEnabled(True)

    def is_visible(self, index: int) -> bool:
        item = self._list.item(index)
        return item is not None and not item.isHidden()

    def is_done(self, index: int) -> bool:
        item = self._list.item(index)
        return isinstance(item, ImageQueueItem) and item.is_labeled

    def first_pending_index(self, start: int = 0) -> int:
        """First not-done row at or after `start`, else -1."""
        for i in range(max(start, 0), self._list.count()):
            item = self._list.item(i)
            if isinstance(item, ImageQueueItem) and not item.is_labeled:
                return i
        return -1

    def index_of(self, path: str) -> int:
        item = self._rows.get(path)
        return self._list.row(item) if item is not None else -1

    def set_current_index(self, index: int):
        if 0 <= index < self._list.count():
            self._list.blockSignals(True)
            self._list.setCurrentRow(index)
            self._list.blockSignals(False)
            item = self._list.item(index)
            if item is not None:
                if item.isHidden():
                    item.setHidden(False)      # never strand the current row
                self._list.scrollToItem(item, QListWidget.EnsureVisible)
            self._thumb_timer.start(0)

    def current_index(self) -> int:
        return self._list.currentRow()

    def clear(self):
        self._thumbs.cancel_pending()
        self._thumbs.clear_memory()
        self._list.clear()
        self._rows.clear()
        self._image_paths.clear()
        self._n_done = self._n_flagged = 0
        self._update_count()
        self._btn_clear.setEnabled(False)

    @property
    def image_paths(self) -> List[str]:
        return list(self._image_paths)

    @property
    def count(self) -> int:
        return len(self._image_paths)

    @property
    def done_count(self) -> int:
        return self._n_done

    @property
    def flagged_count(self) -> int:
        return self._n_flagged

    @property
    def thumbnails(self) -> ThumbnailService:
        return self._thumbs

    def original_size(self, path: str) -> Optional[QSize]:
        item = self._rows.get(path)
        return item.orig_size if item else None

    # ------------------------------------------------------------------ #
    #  Internal                                                            #
    # ------------------------------------------------------------------ #

    def _on_row_changed(self, row: int):
        item = self._list.item(row)
        if isinstance(item, ImageQueueItem):
            self.image_selected.emit(item.image_path, row)

    @pyqtSlot(str, QImage, QSize)
    def _on_thumbnail_ready(self, path: str, image: QImage, size: QSize):
        item = self._rows.get(path)          # O(1) — used to be a full scan
        if item is not None:
            item.set_thumbnail(image)
            if size.isValid():
                item.orig_size = size

    def _update_count(self):
        total   = len(self._rows)
        pending = total - self._n_done - self._n_flagged
        text = (f"{total} images  ·  {self._n_done} {self._done_word.lower()}"
                f"  ·  {pending} pending")
        if self._n_flagged:
            text += f"  ·  {self._n_flagged} flagged"
        self._count_label.setText(text)

    # ---- Lazy thumbnails -------------------------------------------------

    def _visible_row_range(self):
        if self._list.count() == 0:
            return 0, -1
        viewport = self._list.viewport().rect()
        first = self._list.indexAt(QPoint(4, 0)).row()
        last  = self._list.indexAt(QPoint(4, max(0, viewport.height() - 2))).row()
        if first < 0:
            first = 0
        if last < 0:
            last = min(self._list.count() - 1, first + 30)
        return max(0, first - VIEW_MARGIN), min(self._list.count() - 1,
                                                last + VIEW_MARGIN)

    def _request_visible_thumbnails(self):
        first, last = self._visible_row_range()
        for i in range(first, last + 1):
            item = self._list.item(i)
            if not isinstance(item, ImageQueueItem) or item.isHidden():
                continue
            if item.icon().isNull():
                self._thumbs.request(item.image_path)

    def showEvent(self, event):
        super().showEvent(event)
        self._thumb_timer.start(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._thumb_timer.start(80)

    def shutdown(self):
        self._thumb_timer.stop()
        self._thumbs.shutdown()

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)
