"""
SnP Image Labeler - Annotation Viewer
Extends ImageViewer with full bounding-box annotation:
  - NAVIGATE mode  : pan / zoom (unchanged behaviour)
  - ANNOTATE mode  : draw new boxes, select / move / resize / delete existing ones
"""

import uuid
from enum import Enum
from typing import Dict, List, Optional

from PyQt5.QtCore import (
    QPointF, QRectF, Qt, pyqtSignal,
)
from PyQt5.QtGui import (
    QBrush, QColor, QPainter, QPainterPath, QPen, QPixmap, QFont,
)
from PyQt5.QtWidgets import (
    QDialog, QDialogButtonBox,
    QGraphicsItem, QGraphicsObject,
    QGraphicsRectItem, QGraphicsScene, QGraphicsView,
    QGridLayout, QInputDialog, QLabel, QLineEdit,
    QPushButton, QVBoxLayout,
)

from widgets.image_viewer import ImageViewer

# ------------------------------------------------------------------ #
#  Mode enum                                                           #
# ------------------------------------------------------------------ #

class ViewerMode(Enum):
    NAVIGATE = "navigate"
    ANNOTATE = "annotate"


# ------------------------------------------------------------------ #
#  Label colour palette                                                #
# ------------------------------------------------------------------ #

LABEL_COLORS = [
    QColor('#89b4fa'), QColor('#a6e3a1'), QColor('#fab387'),
    QColor('#f38ba8'), QColor('#cba6f7'), QColor('#94e2d5'),
    QColor('#f9e2af'), QColor('#74c7ec'), QColor('#eba0ac'),
    QColor('#b4befe'),
]


# ------------------------------------------------------------------ #
#  Quick label-pick dialog                                             #
# ------------------------------------------------------------------ #

class LabelDialog(QDialog):
    def __init__(self, existing_labels: List[str], current: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Assign Label")
        self.setMinimumWidth(300)
        self.setModal(True)
        self._build(existing_labels, current)

    def _build(self, existing_labels, current):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.addWidget(QLabel("Label:"))

        self._edit = QLineEdit(current)
        self._edit.setPlaceholderText("Type a label name...")
        layout.addWidget(self._edit)

        if existing_labels:
            layout.addWidget(QLabel("Quick pick:"))
            grid = QGridLayout()
            grid.setSpacing(4)
            for i, lbl in enumerate(existing_labels[:12]):
                btn = QPushButton(lbl)
                btn.setFixedHeight(26)
                btn.clicked.connect(lambda _, l=lbl: (self._edit.setText(l), self.accept()))
                grid.addWidget(btn, i // 3, i % 3)
            layout.addLayout(grid)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)
        self._edit.setFocus()
        self._edit.selectAll()

    def get_label(self) -> str:
        return self._edit.text().strip()


# ------------------------------------------------------------------ #
#  BoxItem — interactive bounding box                                  #
# ------------------------------------------------------------------ #

class BoxItem(QGraphicsObject):
    """
    A single bounding box drawn on the scene.
    Inherits QGraphicsObject so we can emit pyqtSignals.

    Resize handles (8 points) appear when the item is selected.
    Moving is handled by Qt's built-in ItemIsMovable flag.
    Resizing is handled via custom mouse press/move/release logic.
    """

    item_modified = pyqtSignal(str)   # box_id

    # Handle IDs
    TL, TM, TR = 0, 1, 2
    ML,     MR = 3,    4
    BL, BM, BR = 5, 6, 7

    HANDLE_SIZE  = 9
    MIN_BOX_SIZE = 12

    HANDLE_CURSORS = {
        0: Qt.SizeFDiagCursor, 1: Qt.SizeVerCursor,  2: Qt.SizeBDiagCursor,
        3: Qt.SizeHorCursor,                          4: Qt.SizeHorCursor,
        5: Qt.SizeBDiagCursor, 6: Qt.SizeVerCursor,  7: Qt.SizeFDiagCursor,
    }

    def __init__(self, rect: QRectF, label: str, box_id: str, color: QColor):
        super().__init__()
        self._rect  = QRectF(rect)
        self.label  = label
        self.box_id = box_id
        self._color = QColor(color)

        self._drag_handle    = None
        self._drag_start_pos = None
        self._drag_start_rect= None

        self.setFlags(
            QGraphicsItem.ItemIsSelectable |
            QGraphicsItem.ItemIsMovable    |
            QGraphicsItem.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)
        self.setZValue(1)

    # ---- Geometry -------------------------------------------------------

    def rect(self) -> QRectF:
        return QRectF(self._rect)

    def boundingRect(self) -> QRectF:
        m = self.HANDLE_SIZE + 4
        return self._rect.adjusted(-m, -m, m, m)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addRect(self._rect)
        if self.isSelected():
            for r in self._handle_rects().values():
                path.addRect(r)
        return path

    # ---- Handles --------------------------------------------------------

    def _handle_rects(self) -> Dict[int, QRectF]:
        r  = self._rect
        hs = self.HANDLE_SIZE / 2
        cx = r.center().x()
        cy = r.center().y()
        s  = self.HANDLE_SIZE
        return {
            self.TL: QRectF(r.left() - hs,  r.top() - hs,    s, s),
            self.TM: QRectF(cx - hs,         r.top() - hs,    s, s),
            self.TR: QRectF(r.right()- hs,   r.top() - hs,    s, s),
            self.ML: QRectF(r.left() - hs,   cy - hs,         s, s),
            self.MR: QRectF(r.right()- hs,   cy - hs,         s, s),
            self.BL: QRectF(r.left() - hs,   r.bottom() - hs, s, s),
            self.BM: QRectF(cx - hs,          r.bottom() - hs, s, s),
            self.BR: QRectF(r.right()- hs,   r.bottom() - hs, s, s),
        }

    def _handle_at(self, pos: QPointF) -> Optional[int]:
        if not self.isSelected():
            return None
        for hid, hr in self._handle_rects().items():
            if hr.contains(pos):
                return hid
        return None

    # ---- Hover ----------------------------------------------------------

    def hoverMoveEvent(self, event):
        h = self._handle_at(event.pos())
        if h is not None:
            self.setCursor(self.HANDLE_CURSORS.get(h, Qt.SizeAllCursor))
        else:
            self.setCursor(Qt.SizeAllCursor if self.isSelected() else Qt.PointingHandCursor)
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event):
        self.setCursor(Qt.ArrowCursor)
        super().hoverLeaveEvent(event)

    # ---- Mouse ----------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            h = self._handle_at(event.pos())
            if h is not None:
                self._drag_handle     = h
                self._drag_start_pos  = event.scenePos()
                self._drag_start_rect = QRectF(self._rect)
                # Disable move while resizing
                self.setFlag(QGraphicsItem.ItemIsMovable, False)
                event.accept()
                return
        self._drag_handle = None
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_handle is not None:
            delta = event.scenePos() - self._drag_start_pos
            r = QRectF(self._drag_start_rect)
            h = self._drag_handle

            if h in (self.TL, self.ML, self.BL): r.setLeft(r.left()   + delta.x())
            if h in (self.TR, self.MR, self.BR): r.setRight(r.right() + delta.x())
            if h in (self.TL, self.TM, self.TR): r.setTop(r.top()     + delta.y())
            if h in (self.BL, self.BM, self.BR): r.setBottom(r.bottom()+ delta.y())

            nr = r.normalized()
            if nr.width() >= self.MIN_BOX_SIZE and nr.height() >= self.MIN_BOX_SIZE:
                self.prepareGeometryChange()
                self._rect = nr
                self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_handle is not None:
            self._drag_handle = None
            self.setFlag(QGraphicsItem.ItemIsMovable, True)
            self.item_modified.emit(self.box_id)
            event.accept()
            return
        super().mouseReleaseEvent(event)
        # Movement ended
        if event.button() == Qt.LeftButton:
            self.item_modified.emit(self.box_id)

    def mouseDoubleClickEvent(self, event):
        # Double-click → edit label inline
        existing = list(self.scene().views()[0]._label_colors.keys()) if self.scene() and self.scene().views() else []
        dlg = LabelDialog(existing, self.label, None)
        if dlg.exec_() == QDialog.Accepted:
            self.label = dlg.get_label()
            self.update()
            self.item_modified.emit(self.box_id)

    # ---- Item position change (moving) ----------------------------------

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            self.item_modified.emit(self.box_id)
        return super().itemChange(change, value)

    # ---- Paint ----------------------------------------------------------

    def paint(self, painter: QPainter, option, widget):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, False)

        # Fill
        fill = QColor(self._color)
        fill.setAlpha(35)
        painter.setBrush(QBrush(fill))

        # Border
        border_color = QColor(self._color)
        pen_style = Qt.DashLine if self.isSelected() else Qt.SolidLine
        pen = QPen(border_color, 2, pen_style)
        painter.setPen(pen)
        painter.drawRect(self._rect)

        # Label tag (drawn above box)
        if self.label:
            font = QFont("Segoe UI", 8, QFont.Bold)
            painter.setFont(font)
            fm = painter.fontMetrics()
            tw = fm.horizontalAdvance(self.label) + 10
            th = fm.height() + 4
            tag_rect = QRectF(
                self._rect.left(),
                self._rect.top() - th,
                max(tw, 40),
                th,
            )
            painter.fillRect(tag_rect, border_color)
            painter.setPen(QPen(QColor('#1e1e2e')))
            painter.setFont(font)
            painter.drawText(tag_rect, Qt.AlignCenter, self.label)

        # Resize handles (only when selected)
        if self.isSelected():
            painter.setPen(QPen(QColor('#1e1e2e'), 1))
            painter.setBrush(QBrush(QColor('#ffffff')))
            for hr in self._handle_rects().values():
                painter.drawRect(hr)

        painter.restore()


# ------------------------------------------------------------------ #
#  Annotation Viewer                                                   #
# ------------------------------------------------------------------ #

class AnnotationViewer(ImageViewer):
    """
    ImageViewer + bounding-box annotation layer.

    Signals:
      box_added(dict)      — new box drawn
      box_modified(str)    — box moved/resized (box_id)
      box_deleted(str)     — box deleted (box_id)
      selection_changed()  — selected item changed
    """

    box_added      = pyqtSignal(dict)
    box_modified   = pyqtSignal(str)
    box_deleted    = pyqtSignal(str)
    selection_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mode: ViewerMode  = ViewerMode.NAVIGATE
        self._boxes: Dict[str, BoxItem] = {}
        self._label_colors: Dict[str, QColor] = {}

        # Drawing state
        self._draw_start:   Optional[QPointF]          = None
        self._preview_item: Optional[QGraphicsRectItem] = None

        # Connect scene selection change
        self._scene.selectionChanged.connect(self._on_selection_changed)

    # ---- Mode -----------------------------------------------------------

    @property
    def mode(self) -> ViewerMode:
        return self._mode

    def set_mode(self, mode: ViewerMode):
        self._mode = mode
        if mode == ViewerMode.NAVIGATE:
            self.setDragMode(QGraphicsView.ScrollHandDrag)
            self._set_boxes_movable(False)
        else:
            self.setDragMode(QGraphicsView.NoDrag)
            self._set_boxes_movable(True)

    def _set_boxes_movable(self, movable: bool):
        for box in self._boxes.values():
            box.setFlag(QGraphicsItem.ItemIsMovable, movable)
            box.setFlag(QGraphicsItem.ItemIsSelectable, movable)

    # ---- Image loading (override) ---------------------------------------

    def load_image(self, path: str) -> bool:
        # CRITICAL: clear _boxes dict BEFORE super().load_image() which calls
        # scene.clear() — if we clear after, Python holds stale refs to
        # deleted C++ BoxItem objects and removeItem() will crash.
        self._boxes.clear()
        self._draw_start   = None
        self._preview_item = None
        result = super().load_image(path)
        return result

    # ---- Box management -------------------------------------------------

    def load_boxes(self, boxes_data: List[dict]):
        """Populate scene from saved box dicts (left/top/right/bottom format)."""
        self.clear_boxes()
        for bd in boxes_data:
            left   = bd.get('left',   0) or 0
            top    = bd.get('top',    0) or 0
            right  = bd.get('right',  0) or 0
            bottom = bd.get('bottom', 0) or 0
            rect   = QRectF(left, top, right - left, bottom - top)
            self._add_box_item(rect, bd.get('label', ''), bd['id'])

    def clear_boxes(self):
        """
        Remove all boxes safely.
        Guard with try/except because scene.clear() (called by load_image/clear)
        already deletes the C++ objects — attempting removeItem on them crashes.
        """
        for box in list(self._boxes.values()):
            try:
                if box.scene() is not None:
                    self._scene.removeItem(box)
            except RuntimeError:
                pass  # C++ object already deleted by scene.clear()
        self._boxes.clear()

    def get_boxes_data(self) -> List[dict]:
        result = []
        for box in self._boxes.values():
            sr = box.mapRectToScene(box.rect())
            result.append({
                'id':     box.box_id,
                'label':  box.label,
                'left':   round(sr.x(), 4),
                'top':    round(sr.y(), 4),
                'right':  round(sr.x() + sr.width(), 4),
                'bottom': round(sr.y() + sr.height(), 4),
            })
        return result

    def get_single_box(self) -> dict:
        """
        Returns the single bounding box as a flat dict with
        left/top/right/bottom, or all-None if no box exists.
        """
        null_box = {"left": None, "top": None, "right": None, "bottom": None}
        if not self._boxes:
            return null_box
        box = next(iter(self._boxes.values()))
        try:
            sr = box.mapRectToScene(box.rect())
            return {
                "left":   round(sr.x(), 4),
                "top":    round(sr.y(), 4),
                "right":  round(sr.x() + sr.width(), 4),
                "bottom": round(sr.y() + sr.height(), 4),
            }
        except RuntimeError:
            return null_box

    def _show_toast(self, message: str):
        """Non-blocking status message shown as a tooltip-style overlay."""
        from PyQt5.QtWidgets import QLabel
        from PyQt5.QtCore import QTimer
        toast = QLabel(message, self)
        toast.setObjectName("toast_label")
        toast.setAlignment(Qt.AlignCenter)
        toast.setWordWrap(True)
        toast.adjustSize()
        # Centre in viewer
        vw, vh = self.width(), self.height()
        tw, th = toast.width() + 40, toast.height() + 20
        toast.resize(tw, th)
        toast.move((vw - tw) // 2, (vh - th) // 2)
        toast.show()
        toast.raise_()
        QTimer.singleShot(2500, toast.deleteLater)

    def delete_selected_boxes(self):
        for item in list(self._scene.selectedItems()):
            if isinstance(item, BoxItem):
                self._scene.removeItem(item)
                bid = item.box_id
                self._boxes.pop(bid, None)
                self.box_deleted.emit(bid)

    def delete_box_by_id(self, box_id: str):
        box = self._boxes.pop(box_id, None)
        if box:
            self._scene.removeItem(box)
            self.box_deleted.emit(box_id)

    def select_box_by_id(self, box_id: str):
        self._scene.clearSelection()
        box = self._boxes.get(box_id)
        if box:
            box.setSelected(True)
            self.ensureVisible(box)

    def get_selected_box_id(self) -> Optional[str]:
        for item in self._scene.selectedItems():
            if isinstance(item, BoxItem):
                return item.box_id
        return None

    def existing_labels(self) -> List[str]:
        return list(self._label_colors.keys())

    # ---- Internal helpers -----------------------------------------------

    def _label_color(self, label: str) -> QColor:
        if label and label not in self._label_colors:
            idx = len(self._label_colors) % len(LABEL_COLORS)
            self._label_colors[label] = LABEL_COLORS[idx]
        return self._label_colors.get(label, LABEL_COLORS[0])

    def _add_box_item(self, rect: QRectF, label: str, box_id: str) -> BoxItem:
        color = self._label_color(label)
        box   = BoxItem(rect, label, box_id, color)
        box.setFlag(QGraphicsItem.ItemIsMovable,   self._mode == ViewerMode.ANNOTATE)
        box.setFlag(QGraphicsItem.ItemIsSelectable, self._mode == ViewerMode.ANNOTATE)
        box.item_modified.connect(self.box_modified)
        self._scene.addItem(box)
        self._boxes[box_id] = box
        return box

    def _on_selection_changed(self):
        self.selection_changed.emit()

    # ---- Mouse events (drawing) ----------------------------------------

    def mousePressEvent(self, event):
        if self._mode != ViewerMode.ANNOTATE or event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return

        scene_pos = self.mapToScene(event.pos())

        # If clicking on an existing BoxItem → let scene handle selection
        hit = self._scene.itemAt(scene_pos, self.transform())
        if isinstance(hit, BoxItem):
            super().mousePressEvent(event)
            return

        # Check cursor is inside image bounds
        if self._pixmap_item and self._pixmap_item.boundingRect().contains(scene_pos):
            self._draw_start = scene_pos
            # Preview rect
            self._preview_item = QGraphicsRectItem(QRectF(scene_pos, scene_pos))
            self._preview_item.setPen(QPen(QColor('#89b4fa'), 2, Qt.DashLine))
            fill = QColor('#89b4fa')
            fill.setAlpha(25)
            self._preview_item.setBrush(QBrush(fill))
            self._preview_item.setZValue(99)
            self._scene.addItem(self._preview_item)
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._mode == ViewerMode.ANNOTATE and self._draw_start and self._preview_item:
            scene_pos = self.mapToScene(event.pos())
            self._preview_item.setRect(
                QRectF(self._draw_start, scene_pos).normalized()
            )
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._mode == ViewerMode.ANNOTATE and self._draw_start and self._preview_item:
            scene_pos = self.mapToScene(event.pos())
            rect = QRectF(self._draw_start, scene_pos).normalized()

            # Remove preview
            self._scene.removeItem(self._preview_item)
            self._preview_item = None
            self._draw_start   = None

            # Minimum size
            if rect.width() >= BoxItem.MIN_BOX_SIZE and rect.height() >= BoxItem.MIN_BOX_SIZE:
                # Max 1 bounding box per image — block if one already exists
                if self._boxes:
                    self._show_toast("Max one bounding box permitted per image.\nDelete the existing box first.")
                else:
                    # Ask for a label before creating the box
                    dlg = LabelDialog(self.existing_labels(), parent=self)
                    if dlg.exec_() == QDialog.Accepted:
                        label  = dlg.get_label()
                        box_id = str(uuid.uuid4())[:8]
                        self._add_box_item(rect, label, box_id)
                        data = {
                            'id':     box_id,
                            'label':  label,
                            'left':   round(rect.x(), 4),
                            'top':    round(rect.y(), 4),
                            'right':  round(rect.x() + rect.width(), 4),
                            'bottom': round(rect.y() + rect.height(), 4),
                        }
                        self.box_added.emit(data)

            event.accept()
            return
        super().mouseReleaseEvent(event)

    # ---- Keyboard -------------------------------------------------------

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.delete_selected_boxes()
            event.accept()
            return
        if event.key() == Qt.Key_Escape:
            # Cancel in-progress draw
            if self._preview_item:
                self._scene.removeItem(self._preview_item)
                self._preview_item = None
                self._draw_start   = None
            self._scene.clearSelection()
            event.accept()
            return
        super().keyPressEvent(event)
