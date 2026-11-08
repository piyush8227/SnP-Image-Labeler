"""
SnP Image Labeler - Label panel (annotation task)

Replaces the old typed telemetry fields. Annotation is object detection: an
image carries bounding boxes, each box carries one label from a managed list.
There is nothing to configure per label - no Text Input / Dropdown / Checkbox /
Radio / Boolean - so the field-type machinery is gone with it.

The panel does three things:
  - shows the label list and which one new boxes will get
  - lets you retag the selected box in one click
  - opens the Manage Labels dialog
"""

from typing import Dict, List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor, QIcon, QPixmap
from PyQt5.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from core import theme


def _swatch(color: QColor, size: int = 12) -> QIcon:
    pix = QPixmap(size, size)
    pix.fill(QColor(color))
    return QIcon(pix)


class LabelPanel(QWidget):
    """The label list for the annotation task."""

    # A label was chosen: apply it to the selected box, or arm it for the next.
    label_chosen     = pyqtSignal(str)
    manage_requested = pyqtSignal()
    save_requested   = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._labels: List[str] = []
        self._active: str = ""
        self._colors: Dict[str, QColor] = {}
        self._build()

    # ---- Build ----------------------------------------------------------

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QLabel("Labels")
        header.setObjectName("panel_header")
        outer.addWidget(header)

        # Everything below the header scrolls, so a long label list stays
        # reachable at 200% UI scale instead of being clipped.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._hint = QLabel(
            "Pick a label, then drag a box (B).\n"
            "Clicking a label retags the selected box."
        )
        self._hint.setObjectName("hint_label")
        self._hint.setWordWrap(True)
        layout.addWidget(self._hint)

        self._list = QListWidget()
        self._list.setObjectName("label_list")
        self._list.setSelectionMode(QAbstractItemView.SingleSelection)
        self._list.setUniformItemSizes(False)
        self._list.setWordWrap(True)
        self._list.itemClicked.connect(self._on_clicked)
        self._list.currentRowChanged.connect(self._on_row)
        layout.addWidget(self._list, 1)

        self._empty = QLabel("No labels yet — add some with Manage Labels.")
        self._empty.setObjectName("hint_label")
        self._empty.setWordWrap(True)
        layout.addWidget(self._empty)

        row = QHBoxLayout()
        row.setSpacing(6)
        self._btn_manage = QPushButton("Manage Labels")
        self._btn_manage.setToolTip("Add, rename, reorder or remove labels")
        self._btn_manage.clicked.connect(self.manage_requested.emit)
        row.addWidget(self._btn_manage)

        self._btn_save = QPushButton("Save")
        self._btn_save.setObjectName("primary_btn")
        self._btn_save.setToolTip("Write the label file for this image  (Ctrl+S)")
        self._btn_save.clicked.connect(self.save_requested.emit)
        row.addWidget(self._btn_save)
        layout.addLayout(row)

        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

    # ---- Data -----------------------------------------------------------

    def load_labels(self, labels: List[str], colors: Optional[Dict[str, QColor]] = None):
        """Replace the list. Keeps the active label if it survived the edit."""
        self._labels = [str(l) for l in labels if str(l).strip()]
        self._colors = dict(colors or {})
        self._rebuild()
        if self._active not in self._labels:
            self._active = self._labels[0] if self._labels else ""
        self.set_active(self._active)

    def set_colors(self, colors: Dict[str, QColor]):
        self._colors = dict(colors or {})
        self._rebuild()
        self.set_active(self._active)

    def _rebuild(self):
        self._list.blockSignals(True)
        self._list.clear()
        for index, label in enumerate(self._labels):
            item = QListWidgetItem(label)
            color = self._colors.get(label)
            if color is not None:
                item.setIcon(_swatch(color))
            if index < 9:
                item.setToolTip(f"Press {index + 1} to apply '{label}'")
            self._list.addItem(item)
        self._list.blockSignals(False)
        has = bool(self._labels)
        self._list.setVisible(has)
        self._empty.setVisible(not has)

    # ---- Active label ---------------------------------------------------

    def set_active(self, label: str):
        """Highlight the label new boxes will be given."""
        self._active = label if label in self._labels else ""
        self._list.blockSignals(True)
        if self._active:
            self._list.setCurrentRow(self._labels.index(self._active))
        else:
            self._list.clearSelection()
        self._list.blockSignals(False)

    def active_label(self) -> str:
        return self._active

    def labels(self) -> List[str]:
        return list(self._labels)

    def label_at(self, index: int) -> str:
        """Keyboard 1-9."""
        return self._labels[index] if 0 <= index < len(self._labels) else ""

    # ---- Events ---------------------------------------------------------

    def _on_row(self, row: int):
        if 0 <= row < len(self._labels):
            self._active = self._labels[row]

    def _on_clicked(self, item: QListWidgetItem):
        label = item.text()
        self._active = label
        self.label_chosen.emit(label)

    # ---- Theme ----------------------------------------------------------

    def apply_theme(self, mode: str, scale: float = 1.0):
        """Swatches come from the box palette, which is theme-dependent."""
        self._rebuild()
        self.set_active(self._active)
