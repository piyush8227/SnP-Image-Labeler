"""
SnP Image Labeler - Annotation Viewer
Extends ImageViewer with bounding-box annotation:
  - NAVIGATE mode : pan / zoom
  - ANNOTATE mode : draw, select, move, resize, relabel, delete

Behaviour that matters for data quality and for the feel of the tool:
  - every box is clamped to the image, on draw, on move and on resize, so a
    box can never be saved with coordinates outside the picture
  - geometry changes are committed on mouse release, not on every mouse-move
    frame, so dragging a box does not rebuild the side panel 60 times a second
  - the label typed for a box is kept with the box and is written to the
    label file (it used to be collected and thrown away)
  - space-bar / middle-mouse panning works while annotating
  - Alt+Arrows nudge the selected box (Alt+Shift+Arrows = 10 px)
"""

import uuid
from enum import Enum
from typing import Dict, List, Optional

from PyQt5.QtCore import QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import (
    QBrush, QColor, QFont, QPainter, QPainterPath, QPen,
)
from PyQt5.QtWidgets import (
    QDialog, QDialogButtonBox, QGraphicsItem, QGraphicsObject,
    QGraphicsRectItem, QGraphicsView, QGridLayout, QLabel, QLineEdit,
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
        self.setMinimumWidth(320)
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
            layout.addWidget(QLabel("Quick pick  (1-9 on the keyboard):"))
            grid = QGridLayout()
            grid.setSpacing(4)
            for i, lbl in enumerate(existing_labels[:12]):
                caption = f"{i + 1}  {lbl}" if i < 9 else lbl
                btn = QPushButton(caption)
                btn.setFixedHeight(26)
                btn.clicked.connect(
                    lambda _, l=lbl: (self._edit.setText(l), self.accept())
                )
                grid.addWidget(btn, i // 3, i % 3)
            layout.addLayout(grid)
            self._quick = list(existing_labels[:9])
        else:
            self._quick = []

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)
        self._edit.setFocus()
        self._edit.selectAll()

    def keyPressEvent(self, event):
        # 1-9 pick a quick label when the text box is empty or fully selected
        key = event.key()
        if Qt.Key_1 <= key <= Qt.Key_9 and self._quick:
            index = key - Qt.Key_1
            if index < len(self._quick) and (
                not self._edit.text() or self._edit.selectedText() == self._edit.text()
            ):
                self._edit.setText(self._quick[index])
                self.accept()
                return
        super().keyPressEvent(event)

    def get_label(self) -> str:
        return self._edit.text().strip()


# ------------------------------------------------------------------ #
#  BoxItem — interactive bounding box                                  #
# ------------------------------------------------------------------ #

class BoxItem(QGraphicsObject):
    """
    A single bounding box on the scene.

    geometry_preview fires continuously while dragging (cheap: it only feeds
    the size readout).  geometry_committed fires once, on release — that is
    what triggers a panel rebuild, an undo snapshot and an autosave.
    """

    geometry_preview   = pyqtSignal(str)    # box_id — live, high frequency
    geometry_committed = pyqtSignal(str)    # box_id — once per gesture

    TL, TM, TR = 0, 1, 2
    ML,     MR = 3,    4
    BL, BM, BR = 5, 6, 7

    HANDLE_SIZE  = 9
    MIN_BOX_SIZE = 8

    HANDLE_CURSORS = {
        0: Qt.SizeFDiagCursor, 1: Qt.SizeVerCursor, 2: Qt.SizeBDiagCursor,
        3: Qt.SizeHorCursor,                        4: Qt.SizeHorCursor,
        5: Qt.SizeBDiagCursor, 6: Qt.SizeVerCursor, 7: Qt.SizeFDiagCursor,
    }

    def __init__(self, rect: QRectF, label: str, box_id: str, color: QColor,
                 bounds: Optional[QRectF] = None):
        super().__init__()
        self._rect   = QRectF(rect)
        self.label   = label
        self.box_id  = box_id
        self._color  = QColor(color)
        self._bounds = QRectF(bounds) if bounds is not None else None

        self._drag_handle     = None
        self._drag_start_pos  = None
        self._drag_start_rect = None
        self._moving          = False

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

    def scene_rect(self) -> QRectF:
        return self.mapRectToScene(self._rect)

    def set_bounds(self, bounds: Optional[QRectF]):
        self._bounds = QRectF(bounds) if bounds is not None else None

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

    def set_color(self, color: QColor):
        self._color = QColor(color)
        self.update()

    def nudge(self, dx: float, dy: float):
        """Move by a pixel delta, clamped — used by Alt+Arrow keys."""
        self.setPos(self.pos() + QPointF(dx, dy))
        self.geometry_committed.emit(self.box_id)

    # ---- Handles --------------------------------------------------------

    def _handle_rects(self) -> Dict[int, QRectF]:
        r  = self._rect
        hs = self.HANDLE_SIZE / 2
        cx, cy = r.center().x(), r.center().y()
        s = self.HANDLE_SIZE
        return {
            self.TL: QRectF(r.left() - hs,  r.top() - hs,    s, s),
            self.TM: QRectF(cx - hs,        r.top() - hs,    s, s),
            self.TR: QRectF(r.right() - hs, r.top() - hs,    s, s),
            self.ML: QRectF(r.left() - hs,  cy - hs,         s, s),
            self.MR: QRectF(r.right() - hs, cy - hs,         s, s),
            self.BL: QRectF(r.left() - hs,  r.bottom() - hs, s, s),
            self.BM: QRectF(cx - hs,        r.bottom() - hs, s, s),
            self.BR: QRectF(r.right() - hs, r.bottom() - hs, s, s),
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
                self.setFlag(QGraphicsItem.ItemIsMovable, False)
                event.accept()
                return
            self._moving = True
        self._drag_handle = None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_handle is not None:
            delta = event.scenePos() - self._drag_start_pos
            r = QRectF(self._drag_start_rect)
            h = self._drag_handle

            if h in (self.TL, self.ML, self.BL): r.setLeft(r.left()     + delta.x())
            if h in (self.TR, self.MR, self.BR): r.setRight(r.right()   + delta.x())
            if h in (self.TL, self.TM, self.TR): r.setTop(r.top()       + delta.y())
            if h in (self.BL, self.BM, self.BR): r.setBottom(r.bottom() + delta.y())

            nr = self._clamp_rect(r.normalized())
            if nr.width() >= self.MIN_BOX_SIZE and nr.height() >= self.MIN_BOX_SIZE:
                self.prepareGeometryChange()
                self._rect = nr
                self.update()
                self.geometry_preview.emit(self.box_id)
            event.accept()
            return
        super().mouseMoveEvent(event)
        if self._moving:
            self.geometry_preview.emit(self.box_id)

    def mouseReleaseEvent(self, event):
        was_resizing = self._drag_handle is not None
        was_moving   = self._moving
        self._drag_handle = None
        self._moving      = False
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        if was_resizing:
            event.accept()
            self.geometry_committed.emit(self.box_id)
            return
        super().mouseReleaseEvent(event)
        if was_moving and event.button() == Qt.LeftButton:
            self.geometry_committed.emit(self.box_id)

    def mouseDoubleClickEvent(self, event):
        views = self.scene().views() if self.scene() else []
        if views and hasattr(views[0], "request_relabel"):
            views[0].request_relabel(self)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    # ---- Clamping -------------------------------------------------------

    def _clamp_rect(self, rect: QRectF) -> QRectF:
        """Keep a rect inside the image (in item coordinates)."""
        if self._bounds is None:
            return rect
        local_bounds = self.mapFromScene(self._bounds).boundingRect()
        return rect.intersected(local_bounds)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionChange and self._bounds is not None:
            # value is the proposed position; shift it back so the whole box
            # stays on the image.
            proposed = value
            r = self._rect.translated(proposed)
            b = self._bounds
            dx = dy = 0.0
            if r.width() <= b.width():
                if r.left() < b.left():
                    dx = b.left() - r.left()
                elif r.right() > b.right():
                    dx = b.right() - r.right()
            else:
                dx = b.left() - r.left()
            if r.height() <= b.height():
                if r.top() < b.top():
                    dy = b.top() - r.top()
                elif r.bottom() > b.bottom():
                    dy = b.bottom() - r.bottom()
            else:
                dy = b.top() - r.top()
            if dx or dy:
                return QPointF(proposed.x() + dx, proposed.y() + dy)
        return super().itemChange(change, value)

    # ---- Paint ----------------------------------------------------------

    def paint(self, painter: QPainter, option, widget):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, False)

        fill = QColor(self._color)
        fill.setAlpha(35)
        painter.setBrush(QBrush(fill))

        border_color = QColor(self._color)
        pen_style = Qt.DashLine if self.isSelected() else Qt.SolidLine
        painter.setPen(QPen(border_color, 2, pen_style))
        painter.drawRect(self._rect)

        if self.label:
            font = QFont("Segoe UI", 8, QFont.Bold)
            painter.setFont(font)
            fm = painter.fontMetrics()
            tw = fm.horizontalAdvance(self.label) + 10
            th = fm.height() + 4
            tag_rect = QRectF(self._rect.left(), self._rect.top() - th,
                              max(tw, 40), th)
            painter.fillRect(tag_rect, border_color)
            painter.setPen(QPen(QColor('#1e1e2e')))
            painter.drawText(tag_rect, Qt.AlignCenter, self.label)

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
      boxes_changed()      — a committed change (add / move / resize /
                             relabel / delete / clear); the one to autosave on
      box_added(dict)
      box_deleted(str)
      selection_changed()
    """

    boxes_changed     = pyqtSignal()
    box_added         = pyqtSignal(dict)
    box_deleted       = pyqtSignal(str)
    selection_changed = pyqtSignal()
    status_message    = pyqtSignal(str)

    def __init__(self, parent=None, cache_budget_mb: int = 384):
        super().__init__(parent, cache_budget_mb)
        self._mode: ViewerMode = ViewerMode.NAVIGATE
        self._boxes: Dict[str, BoxItem] = {}
        self._label_colors: Dict[str, QColor] = {}
        self._max_boxes = 1

        # Label entry behaviour
        self._recent_labels: List[str] = []
        self._sticky_label = ""
        self._sticky_enabled = False

        # Drawing state
        self._draw_start:   Optional[QPointF] = None
        self._preview_item: Optional[QGraphicsRectItem] = None
        self._pending_boxes: List[dict] = []

        self._scene.selectionChanged.connect(self._on_selection_changed)
        self.image_loaded.connect(self._apply_pending_boxes)

        # Live geometry readout (top-left of the viewport)
        self._readout = QLabel("", self)
        self._readout.setObjectName("box_readout")
        self._readout.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._readout.hide()
        self._readout_timer = QTimer(self)
        self._readout_timer.setSingleShot(True)
        self._readout_timer.timeout.connect(self._readout.hide)

    # ---- Mode -----------------------------------------------------------

    @property
    def mode(self) -> ViewerMode:
        return self._mode

    def set_mode(self, mode: ViewerMode):
        self._mode = mode
        if mode == ViewerMode.NAVIGATE:
            self.setDragMode(QGraphicsView.ScrollHandDrag)
            self._set_boxes_interactive(False)
        else:
            self.setDragMode(QGraphicsView.NoDrag)
            self._set_boxes_interactive(True)
        self._saved_drag = self.dragMode()

    def _set_boxes_interactive(self, on: bool):
        for box in self._boxes.values():
            box.setFlag(QGraphicsItem.ItemIsMovable, on)
            box.setFlag(QGraphicsItem.ItemIsSelectable, on)

    def set_max_boxes(self, n: int):
        self._max_boxes = max(1, int(n))

    # ---- Label behaviour ------------------------------------------------

    def set_recent_labels(self, labels: List[str]):
        self._recent_labels = [l for l in labels if l][:12]

    def set_sticky_label(self, enabled: bool, label: str = ""):
        """When on, a new box takes the last label without asking."""
        self._sticky_enabled = bool(enabled)
        if label:
            self._sticky_label = label

    @property
    def sticky_label(self) -> str:
        return self._sticky_label

    def existing_labels(self) -> List[str]:
        seen = list(self._recent_labels)
        for lbl in self._label_colors:
            if lbl and lbl not in seen:
                seen.append(lbl)
        return seen

    def apply_label_to_selection(self, label: str) -> bool:
        """Hotkey 1-9 / palette click. Returns True when something changed."""
        target = None
        for item in self._scene.selectedItems():
            if isinstance(item, BoxItem):
                target = item
                break
        if target is None and len(self._boxes) == 1:
            target = next(iter(self._boxes.values()))
        if target is None:
            return False
        if target.label == label:
            return False
        target.label = label
        target.set_color(self._label_color(label))
        self._sticky_label = label
        self._remember_label(label)
        self.boxes_changed.emit()
        return True

    def request_relabel(self, box: BoxItem):
        dlg = LabelDialog(self.existing_labels(), box.label, self)
        if dlg.exec_() == QDialog.Accepted:
            label = dlg.get_label()
            box.label = label
            box.set_color(self._label_color(label))
            if label:
                self._sticky_label = label
                self._remember_label(label)
            box.update()
            self.boxes_changed.emit()

    def _remember_label(self, label: str):
        if not label:
            return
        if label in self._recent_labels:
            self._recent_labels.remove(label)
        self._recent_labels.insert(0, label)
        del self._recent_labels[12:]

    # ---- Image loading --------------------------------------------------

    def load_image(self, path: str, boxes: Optional[List[dict]] = None) -> bool:
        """Load an image and (once decoded) place its saved boxes."""
        self._pending_boxes = list(boxes or [])
        self._draw_start   = None
        self._preview_item = None
        self._readout.hide()
        return super().load_image(path)

    def _on_scene_cleared(self):
        # scene.clear() has already deleted the C++ items; drop our handles
        # before anything can touch them.
        self._boxes.clear()
        self._preview_item = None
        self._draw_start = None

    def _apply_pending_boxes(self, path: str, _size: QSize):
        boxes, self._pending_boxes = self._pending_boxes, []
        if boxes:
            self.load_boxes(boxes)

    # ---- Box management -------------------------------------------------

    def image_bounds(self) -> Optional[QRectF]:
        return self._pixmap_item.boundingRect() if self._pixmap_item else None

    def load_boxes(self, boxes_data: List[dict]):
        """Populate the scene from saved box dicts (left/top/right/bottom)."""
        self.clear_boxes(silent=True)
        bounds = self.image_bounds()
        for bd in boxes_data:
            left   = bd.get('left',   0) or 0
            top    = bd.get('top',    0) or 0
            right  = bd.get('right',  0) or 0
            bottom = bd.get('bottom', 0) or 0
            rect = QRectF(left, top, right - left, bottom - top).normalized()
            if bounds is not None:
                rect = rect.intersected(bounds)
            if rect.width() <= 0 or rect.height() <= 0:
                continue
            self._add_box_item(rect, bd.get('label', '') or '',
                               bd.get('id') or uuid.uuid4().hex[:8])

    def set_boxes(self, boxes_data: List[dict]):
        """Replace all boxes (used by undo/redo). Emits boxes_changed."""
        self.load_boxes(boxes_data)
        self.boxes_changed.emit()

    def clear_boxes(self, silent: bool = False):
        for box in list(self._boxes.values()):
            try:
                if box.scene() is not None:
                    self._scene.removeItem(box)
            except RuntimeError:
                pass                      # already deleted by scene.clear()
        self._boxes.clear()
        if not silent:
            self.boxes_changed.emit()

    def get_boxes_data(self) -> List[dict]:
        result = []
        for box in self._boxes.values():
            try:
                sr = box.scene_rect()
            except RuntimeError:
                continue
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
        The saved bounding box as a flat dict. `label` travels with the
        coordinates so the value the user typed is not lost on reload.
        """
        null_box = {"left": None, "top": None, "right": None,
                    "bottom": None, "label": ""}
        if not self._boxes:
            return null_box
        box = next(iter(self._boxes.values()))
        try:
            sr = box.scene_rect()
        except RuntimeError:
            return null_box
        return {
            "left":   round(sr.x(), 4),
            "top":    round(sr.y(), 4),
            "right":  round(sr.x() + sr.width(), 4),
            "bottom": round(sr.y() + sr.height(), 4),
            "label":  box.label or "",
        }

    def delete_selected_boxes(self) -> int:
        removed = 0
        for item in list(self._scene.selectedItems()):
            if isinstance(item, BoxItem):
                self._scene.removeItem(item)
                self._boxes.pop(item.box_id, None)
                self.box_deleted.emit(item.box_id)
                removed += 1
        if removed:
            self.boxes_changed.emit()
        return removed

    def delete_box_by_id(self, box_id: str):
        box = self._boxes.pop(box_id, None)
        if box:
            try:
                self._scene.removeItem(box)
            except RuntimeError:
                pass
            self.box_deleted.emit(box_id)
            self.boxes_changed.emit()

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

    def selected_box(self) -> Optional[BoxItem]:
        for item in self._scene.selectedItems():
            if isinstance(item, BoxItem):
                return item
        return None

    @property
    def box_count(self) -> int:
        return len(self._boxes)

    @property
    def label_colors(self) -> Dict[str, QColor]:
        return dict(self._label_colors)

    # ---- Internal helpers -----------------------------------------------

    def _label_color(self, label: str) -> QColor:
        if label and label not in self._label_colors:
            idx = len(self._label_colors) % len(LABEL_COLORS)
            self._label_colors[label] = LABEL_COLORS[idx]
        return self._label_colors.get(label, LABEL_COLORS[0])

    def _add_box_item(self, rect: QRectF, label: str, box_id: str) -> BoxItem:
        box = BoxItem(rect, label, box_id, self._label_color(label),
                      self.image_bounds())
        interactive = self._mode == ViewerMode.ANNOTATE
        box.setFlag(QGraphicsItem.ItemIsMovable, interactive)
        box.setFlag(QGraphicsItem.ItemIsSelectable, interactive)
        box.geometry_preview.connect(self._on_geometry_preview)
        box.geometry_committed.connect(self._on_geometry_committed)
        self._scene.addItem(box)
        self._boxes[box_id] = box
        return box

    def _on_selection_changed(self):
        self.selection_changed.emit()

    def _on_geometry_preview(self, box_id: str):
        box = self._boxes.get(box_id)
        if box is not None:
            self._show_readout(box.scene_rect())

    def _on_geometry_committed(self, _box_id: str):
        self.boxes_changed.emit()

    # ---- Readout / toast ------------------------------------------------

    def _show_readout(self, rect: QRectF):
        self._readout.setText(
            f"{int(round(rect.width()))} × {int(round(rect.height()))} px"
            f"   ({int(round(rect.x()))}, {int(round(rect.y()))})"
        )
        self._readout.adjustSize()
        self._readout.move(10, 10)
        self._readout.show()
        self._readout.raise_()
        self._readout_timer.start(1400)

    def _show_toast(self, message: str):
        toast = QLabel(message, self)
        toast.setObjectName("toast_label")
        toast.setAlignment(Qt.AlignCenter)
        toast.setWordWrap(True)
        toast.adjustSize()
        tw, th = toast.width() + 40, toast.height() + 20
        toast.resize(tw, th)
        toast.move(max(0, (self.width() - tw) // 2), max(0, (self.height() - th) // 2))
        toast.show()
        toast.raise_()
        QTimer.singleShot(2200, toast.deleteLater)

    def _clamp_to_image(self, pos: QPointF) -> QPointF:
        bounds = self.image_bounds()
        if bounds is None:
            return pos
        return QPointF(
            min(max(pos.x(), bounds.left()), bounds.right()),
            min(max(pos.y(), bounds.top()),  bounds.bottom()),
        )

    # ---- Mouse events (drawing) -----------------------------------------

    def mousePressEvent(self, event):
        if (self._mode != ViewerMode.ANNOTATE
                or event.button() != Qt.LeftButton
                or self.panning):
            super().mousePressEvent(event)
            return

        scene_pos = self.mapToScene(event.pos())
        hit = self._scene.itemAt(scene_pos, self.transform())
        if isinstance(hit, BoxItem):
            super().mousePressEvent(event)
            return

        if self._pixmap_item and self._pixmap_item.boundingRect().contains(scene_pos):
            if len(self._boxes) >= self._max_boxes:
                self._show_toast(
                    f"Max {self._max_boxes} bounding box per image.\n"
                    "Delete the existing box first (Del)."
                )
                event.accept()
                return
            self._draw_start = scene_pos
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
            scene_pos = self._clamp_to_image(self.mapToScene(event.pos()))
            rect = QRectF(self._draw_start, scene_pos).normalized()
            self._preview_item.setRect(rect)
            self._show_readout(rect)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._mode == ViewerMode.ANNOTATE and self._draw_start and self._preview_item:
            scene_pos = self._clamp_to_image(self.mapToScene(event.pos()))
            rect = QRectF(self._draw_start, scene_pos).normalized()

            try:
                self._scene.removeItem(self._preview_item)
            except RuntimeError:
                pass
            self._preview_item = None
            self._draw_start   = None
            self._readout.hide()

            if rect.width() < BoxItem.MIN_BOX_SIZE or rect.height() < BoxItem.MIN_BOX_SIZE:
                self._show_toast("Box too small — drag a larger area.")
                event.accept()
                return

            label = self._sticky_label if self._sticky_enabled else None
            if label is None:
                dlg = LabelDialog(self.existing_labels(), self._sticky_label, self)
                if dlg.exec_() != QDialog.Accepted:
                    event.accept()
                    return
                label = dlg.get_label()

            box_id = uuid.uuid4().hex[:8]
            self._add_box_item(rect, label, box_id)
            if label:
                self._sticky_label = label
                self._remember_label(label)

            self.box_added.emit({
                'id':     box_id,
                'label':  label,
                'left':   round(rect.x(), 4),
                'top':    round(rect.y(), 4),
                'right':  round(rect.x() + rect.width(), 4),
                'bottom': round(rect.y() + rect.height(), 4),
            })
            self.boxes_changed.emit()
            event.accept()
            return

        super().mouseReleaseEvent(event)

    # ---- Keyboard -------------------------------------------------------

    def keyPressEvent(self, event):
        key  = event.key()
        mods = event.modifiers()

        if key in (Qt.Key_Delete, Qt.Key_Backspace):
            if self.delete_selected_boxes():
                self.status_message.emit("Box deleted  (Ctrl+Z to undo)")
            event.accept()
            return

        if key == Qt.Key_Escape:
            if self._preview_item:
                try:
                    self._scene.removeItem(self._preview_item)
                except RuntimeError:
                    pass
                self._preview_item = None
                self._draw_start = None
                self._readout.hide()
            self._scene.clearSelection()
            event.accept()
            return

        # Alt+Arrows nudge the selected box (Alt+Shift = 10 px)
        if mods & Qt.AltModifier and key in (
            Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down
        ):
            box = self.selected_box()
            if box is not None:
                step = 10.0 if mods & Qt.ShiftModifier else 1.0
                dx = -step if key == Qt.Key_Left else step if key == Qt.Key_Right else 0.0
                dy = -step if key == Qt.Key_Up else step if key == Qt.Key_Down else 0.0
                box.nudge(dx, dy)
                self._show_readout(box.scene_rect())
                event.accept()
                return

        super().keyPressEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._readout.move(10, 10)
