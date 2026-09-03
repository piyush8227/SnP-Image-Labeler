"""
SnP Image Labeler - BBox Panel
Right-panel tab showing the list of bounding boxes for the current image.
Allows selecting, renaming and deleting boxes from the list.
"""

from typing import Callable, List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor, QPixmap, QIcon
from PyQt5.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout, QWidget,
)


def _color_icon(color: QColor, size: int = 14) -> QIcon:
    px = QPixmap(size, size)
    px.fill(color)
    return QIcon(px)


class BBoxPanel(QWidget):
    """
    Displays all bounding boxes for the current image.

    Signals:
      box_select_requested(str)   — user clicked a row → select that box in viewer
      box_delete_requested(str)   — user clicked delete button for a box
      clear_all_requested()       — user clicked Clear All
    """

    box_select_requested = pyqtSignal(str)
    box_delete_requested = pyqtSignal(str)
    clear_all_requested  = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._boxes: List[dict] = []   # list of {id, label, x, y, w, h}
        self._label_colors: dict = {}  # label -> QColor
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        hdr = QLabel("Annotations")
        hdr.setObjectName("panel_header")
        hdr.setAlignment(Qt.AlignCenter)
        layout.addWidget(hdr)

        # Count bar
        self._count_lbl = QLabel("0 boxes")
        self._count_lbl.setObjectName("count_label")
        self._count_lbl.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._count_lbl)

        # Box list
        self._list = QListWidget()
        self._list.setObjectName("bbox_list")
        self._list.setSpacing(1)
        self._list.currentRowChanged.connect(self._on_row_changed)
        layout.addWidget(self._list, 1)

        # Footer buttons
        footer = QFrame()
        footer.setObjectName("save_frame")
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(8, 6, 8, 6)
        fl.setSpacing(6)

        self._btn_del = QPushButton("Delete Selected")
        self._btn_del.setEnabled(False)
        self._btn_del.clicked.connect(lambda _: self._delete_selected())

        self._btn_clear = QPushButton("Clear All")
        self._btn_clear.setEnabled(False)
        self._btn_clear.clicked.connect(lambda _: self.clear_all_requested.emit())

        fl.addWidget(self._btn_del)
        fl.addWidget(self._btn_clear)
        layout.addWidget(footer)

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def refresh(self, boxes: List[dict], label_colors: dict):
        """Reload list from fresh box data."""
        self._boxes = list(boxes)
        self._label_colors = dict(label_colors)
        self._rebuild_list()

    def select_by_id(self, box_id: str):
        """Highlight the row matching box_id (called from viewer)."""
        self._list.blockSignals(True)
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item and item.data(Qt.UserRole) == box_id:
                self._list.setCurrentRow(i)
                break
        self._list.blockSignals(False)

    def clear(self):
        self._boxes.clear()
        self._list.clear()
        self._update_count()
        self._btn_del.setEnabled(False)
        self._btn_clear.setEnabled(False)

    # ------------------------------------------------------------------ #
    #  Internal                                                            #
    # ------------------------------------------------------------------ #

    def _rebuild_list(self):
        self._list.clear()
        for bd in self._boxes:
            label = bd.get('label') or '(no label)'
            left   = round(bd.get('left',   0) or 0)
            top    = round(bd.get('top',    0) or 0)
            right  = round(bd.get('right',  0) or 0)
            bottom = round(bd.get('bottom', 0) or 0)
            text   = f"{label}  [L:{left} T:{top} R:{right} B:{bottom}]"
            item  = QListWidgetItem(text)
            item.setData(Qt.UserRole, bd['id'])

            color = self._label_colors.get(bd.get('label', ''))
            if color:
                item.setIcon(_color_icon(color))

            self._list.addItem(item)

        self._update_count()
        has = self._list.count() > 0
        self._btn_clear.setEnabled(has)
        self._btn_del.setEnabled(False)

    def _on_row_changed(self, row: int):
        item = self._list.item(row)
        has  = item is not None
        self._btn_del.setEnabled(has)
        if has:
            self.box_select_requested.emit(item.data(Qt.UserRole))

    def _delete_selected(self):
        item = self._list.currentItem()
        if item:
            self.box_delete_requested.emit(item.data(Qt.UserRole))

    def _update_count(self):
        n = self._list.count()
        self._count_lbl.setText(f"{n} box{'es' if n != 1 else ''}")
