"""
SnP Image Labeler - Review Grid

A contact sheet of the queue with each saved bounding box drawn on the
thumbnail, so a batch can be checked at a glance instead of stepping through
it one image at a time.  Double-click jumps the main window to that image.

Thumbnails come from the same cached, scaled-decode service the queue uses,
so opening this on a folder that has already been browsed costs almost
nothing.
"""

import os
from typing import Callable, Dict, List, Optional

from PyQt5.QtCore import QPoint, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QIcon, QImage, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QListView, QListWidget,
    QListWidgetItem, QPushButton, QVBoxLayout,
)

from core.thumbnail_cache import ThumbnailService
from widgets.image_queue import DONE, FLAGGED, PENDING

CELL = 168
MARGIN_ROWS = 2

STATE_COLORS = {
    PENDING: QColor("#a6adc8"),
    DONE:    QColor("#a6e3a1"),
    FLAGGED: QColor("#f9e2af"),
}
STATE_NAMES = {PENDING: "Pending", DONE: "Done", FLAGGED: "Flagged"}


class ReviewGridDialog(QDialog):
    """Contact-sheet QC view over the current queue."""

    open_requested = pyqtSignal(str)

    def __init__(self,
                 paths: List[str],
                 state_provider: Callable[[str], int],
                 box_provider: Callable[[str], Optional[dict]],
                 parent=None):
        super().__init__(parent)
        self._paths          = list(paths)
        self._state_provider = state_provider
        self._box_provider   = box_provider
        self._items: Dict[str, QListWidgetItem] = {}
        self._filter = "All"

        self._thumbs = ThumbnailService(CELL, "grid", self)
        self._thumbs.thumbnail_ready.connect(self._on_thumbnail_ready)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._request_visible)

        self.setWindowTitle("Review Grid")
        self.setModal(False)
        self.resize(1000, 700)
        self._build()
        self.reload(self._paths)

    # ------------------------------------------------------------------ #
    #  UI                                                                  #
    # ------------------------------------------------------------------ #

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self._summary = QLabel("")
        self._summary.setObjectName("info_label")
        bar.addWidget(self._summary, 1)

        bar.addWidget(QLabel("Show:"))
        self._filter_combo = QComboBox()
        self._filter_combo.addItems(["All", "Pending", "Done", "Flagged"])
        self._filter_combo.currentTextChanged.connect(self._on_filter)
        bar.addWidget(self._filter_combo)

        btn_refresh = QPushButton("Refresh")
        btn_refresh.clicked.connect(lambda _: self.reload(self._paths))
        bar.addWidget(btn_refresh)

        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.close)
        bar.addWidget(btn_close)
        root.addLayout(bar)

        self._list = QListWidget()
        self._list.setObjectName("review_grid")
        self._list.setViewMode(QListView.IconMode)
        self._list.setResizeMode(QListView.Adjust)
        self._list.setMovement(QListView.Static)
        self._list.setUniformItemSizes(True)
        self._list.setLayoutMode(QListView.Batched)
        self._list.setBatchSize(60)
        self._list.setIconSize(QSize(CELL, CELL))
        self._list.setGridSize(QSize(CELL + 22, CELL + 46))
        self._list.setSpacing(6)
        self._list.setWordWrap(True)
        self._list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self._list.itemDoubleClicked.connect(self._on_double_click)
        self._list.verticalScrollBar().valueChanged.connect(
            lambda _: self._timer.start(50)
        )
        root.addWidget(self._list, 1)

        hint = QLabel("Double-click an image to open it in the labeller.")
        hint.setObjectName("empty_label")
        root.addWidget(hint)

    # ------------------------------------------------------------------ #
    #  Data                                                                #
    # ------------------------------------------------------------------ #

    def reload(self, paths: List[str]):
        self._paths = list(paths)
        self._thumbs.cancel_pending()
        self._list.clear()
        self._items.clear()

        counts = {PENDING: 0, DONE: 0, FLAGGED: 0}
        self._list.setUpdatesEnabled(False)
        try:
            for path in self._paths:
                state = self._state_provider(path)
                counts[state] = counts.get(state, 0) + 1
                if not self._matches(state):
                    continue
                name = os.path.basename(path)
                caption = name if len(name) <= 22 else name[:19] + "..."
                item = QListWidgetItem(f"{caption}\n{STATE_NAMES.get(state, '')}")
                item.setData(Qt.UserRole, path)
                item.setForeground(STATE_COLORS.get(state, STATE_COLORS[PENDING]))
                item.setToolTip(path)
                item.setSizeHint(QSize(CELL + 20, CELL + 44))
                self._list.addItem(item)
                self._items[path] = item
        finally:
            self._list.setUpdatesEnabled(True)

        self._summary.setText(
            f"{len(self._paths)} images   ·   {counts[DONE]} done   ·   "
            f"{counts[PENDING]} pending   ·   {counts[FLAGGED]} flagged"
        )
        self._timer.start(0)

    def _matches(self, state: int) -> bool:
        if self._filter == "All":
            return True
        return STATE_NAMES.get(state, "") == self._filter

    def _on_filter(self, text: str):
        self._filter = text
        self.reload(self._paths)

    # ------------------------------------------------------------------ #
    #  Thumbnails                                                          #
    # ------------------------------------------------------------------ #

    def _visible_range(self):
        if self._list.count() == 0:
            return 0, -1
        rect = self._list.viewport().rect()
        first = self._list.indexAt(QPoint(4, 0)).row()
        last  = self._list.indexAt(QPoint(rect.width() - 4,
                                          max(0, rect.height() - 2))).row()
        if first < 0:
            first = 0
        if last < 0:
            last = min(self._list.count() - 1, first + 40)
        per_row = max(1, self._list.viewport().width() // (CELL + 22))
        pad = per_row * MARGIN_ROWS
        return max(0, first - pad), min(self._list.count() - 1, last + pad)

    def _request_visible(self):
        first, last = self._visible_range()
        for i in range(first, last + 1):
            item = self._list.item(i)
            if item is None or not item.icon().isNull():
                continue
            self._thumbs.request(item.data(Qt.UserRole))

    def _on_thumbnail_ready(self, path: str, image: QImage, size: QSize):
        item = self._items.get(path)
        if item is None:
            return
        item.setIcon(QIcon(self._compose(image, size, self._box_provider(path))))

    def _compose(self, thumb: QImage, orig: QSize, box: Optional[dict]) -> QPixmap:
        """Draw the saved bounding box on top of the thumbnail."""
        pixmap = QPixmap.fromImage(thumb)
        if not box or box.get("left") is None or not orig or not orig.isValid():
            return pixmap
        try:
            sx = pixmap.width()  / float(orig.width())
            sy = pixmap.height() / float(orig.height())
            left   = float(box["left"])   * sx
            top    = float(box["top"])    * sy
            right  = float(box["right"])  * sx
            bottom = float(box["bottom"]) * sy
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return pixmap

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.setPen(QPen(QColor("#89b4fa"), 2))
        painter.drawRect(int(left), int(top),
                         max(1, int(right - left)), max(1, int(bottom - top)))
        label = str(box.get("label") or "")
        if label:
            painter.setPen(QPen(QColor("#1e1e2e")))
            painter.fillRect(int(left), max(0, int(top) - 14),
                             8 + 6 * len(label), 14, QColor("#89b4fa"))
            painter.drawText(int(left) + 4, max(10, int(top) - 3), label)
        painter.end()
        return pixmap

    # ------------------------------------------------------------------ #
    #  Events                                                              #
    # ------------------------------------------------------------------ #

    def _on_double_click(self, item: QListWidgetItem):
        path = item.data(Qt.UserRole)
        if path:
            self.open_requested.emit(path)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._timer.start(80)

    def closeEvent(self, event):
        self._timer.stop()
        self._thumbs.shutdown()
        super().closeEvent(event)
