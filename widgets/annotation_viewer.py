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
    QBrush, QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen,
)
from PyQt5.QtWidgets import (
    QDialog, QDialogButtonBox, QGraphicsItem, QGraphicsObject,
    QGraphicsRectItem, QGraphicsView, QGridLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout,
)

from core import theme

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

# Filled in by set_chrome(); the defaults are the light-theme set so an import
# before any theme is applied still draws something sane.
LABEL_COLORS = [QColor(c) for c in theme.box_colors(theme.LIGHT)]


class _Chrome:
    """
    Shared drawing constants for every box on screen.

    A bounding box sits on an arbitrary photograph, so its outline cannot rely
    on the theme background for contrast the way the rest of the UI can. Every
    stroke is therefore drawn twice - a dark halo underneath a bright colour -
    which stays readable over white paper, dark cloth and busy texture alike.
    """

    halo   = QColor("#000000")        # outline under every stroke
    handle = QColor("#ffffff")        # grab-point fill
    rim    = QColor("#000000")        # grab-point outline
    ui_scale = 1.0                    # so handles grow with the UI scale

    PEN_PX        = 2.0               # bright stroke, in SCREEN pixels
    HALO_PX       = 5.0               # dark stroke under it
    CORNER_PX     = 11.0              # corner grab-point, screen pixels
    EDGE_PX       = 9.0               # mid-edge grab-point
    TAG_PAD_PX    = 4.0


CHROME = _Chrome()


def set_chrome(mode: str, scale: float = 1.0) -> None:
    """Point the box painter at a theme's palette. Called by the viewer."""
    global LABEL_COLORS
    LABEL_COLORS = [QColor(c) for c in theme.box_colors(mode)]
    CHROME.halo     = QColor(theme.color(mode, "box_halo"))
    CHROME.handle   = QColor(theme.color(mode, "box_handle"))
    CHROME.rim      = QColor(theme.color(mode, "box_handle_rim"))
    CHROME.ui_scale = theme.clamp_scale(scale)


def _readable_on(color: QColor) -> QColor:
    """Black or white, whichever stays legible on `color`."""
    r, g, b = color.red(), color.green(), color.blue()
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
    return QColor("#000000") if luminance > 0.6 else QColor("#ffffff")


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
                btn.setMinimumHeight(theme.px(26))
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
#  Label tag — a child item that ignores the view transform            #
# ------------------------------------------------------------------ #

class TagItem(QGraphicsItem):
    """
    The label chip pinned to a box's top-left corner.

    It carries ItemIgnoresTransformations, so it is laid out and painted in
    screen pixels no matter how far the image is zoomed. Drawing the text
    inside BoxItem.paint() instead would scale it with the scene: illegibly
    small when zoomed out, and a wall of text when zoomed in.
    """

    def __init__(self, parent: "BoxItem"):
        super().__init__(parent)
        self._text  = ""
        self._color = QColor("#ffffff")
        self.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        self.setZValue(3)
        # Decoration only. The chip overhangs the box's top-left corner, so if
        # it answered hit tests it would swallow the clicks meant for that
        # corner handle and the viewer would start drawing a new box instead
        # of resizing the one under the cursor.
        self.setAcceptedMouseButtons(Qt.NoButton)
        self.setAcceptHoverEvents(False)

    def shape(self) -> QPainterPath:
        return QPainterPath()          # empty: never the answer to itemAt()

    def set_tag(self, text: str, color: QColor):
        if text == self._text and QColor(color) == self._color:
            return
        self.prepareGeometryChange()
        self._text  = text or ""
        self._color = QColor(color)
        self.setVisible(bool(self._text))
        self.update()

    def _font(self) -> QFont:
        font = QFont("Segoe UI")
        font.setPointSizeF(max(6.0, 8.0 * CHROME.ui_scale))
        font.setBold(True)
        return font

    def _size(self):
        fm  = QFontMetricsF(self._font())
        pad = max(3.0, CHROME.TAG_PAD_PX * CHROME.ui_scale)
        return (fm.horizontalAdvance(self._text) + pad * 3.0, fm.height() + pad)

    def boundingRect(self) -> QRectF:
        if not self._text:
            return QRectF()
        w, h = self._size()
        # Sits just above the corner it is anchored to.
        return QRectF(-1, -h - 1, w + 2, h + 2)

    def paint(self, painter: QPainter, option, widget):
        if not self._text:
            return
        w, h = self._size()
        rect = QRectF(0, -h, w, h)
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(CHROME.halo)))
        painter.drawRect(rect.adjusted(-1, -1, 1, 1))
        painter.setBrush(QBrush(self._color))
        painter.drawRect(rect)
        painter.setFont(self._font())
        painter.setPen(QPen(_readable_on(self._color)))
        painter.drawText(rect, Qt.AlignCenter, self._text)


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
        self._tag = TagItem(self)
        self._sync_tag(self._color)

    # ---- Geometry -------------------------------------------------------

    def rect(self) -> QRectF:
        return QRectF(self._rect)

    def scene_rect(self) -> QRectF:
        return self.mapRectToScene(self._rect)

    def set_bounds(self, bounds: Optional[QRectF]):
        self._bounds = QRectF(bounds) if bounds is not None else None

    def view_k(self) -> float:
        """
        Scene units per screen pixel.

        Chrome is specified in screen pixels and divided by the zoom, so a
        handle is the same easy-to-hit size whether the image is at 10% or 800%.
        Before this, handle size and pen width were scene units: zoomed out, the
        outline thinned to nothing and the corners became un-grabbable.
        """
        scene = self.scene()
        views = scene.views() if scene is not None else []
        if views:
            m11 = views[0].transform().m11()
            if m11 > 1e-9:
                return 1.0 / m11
        return 1.0

    def refresh_chrome(self):
        """Re-measure after a zoom change; boundingRect depends on view_k()."""
        self.prepareGeometryChange()
        self.update()

    def boundingRect(self) -> QRectF:
        k = self.view_k()
        # Room for the halo, the grab-points and the label tag above the box.
        side = (CHROME.CORNER_PX * CHROME.ui_scale + CHROME.HALO_PX) * k
        top  = side + self._tag_height(k)
        return self._rect.adjusted(-side, -top, side, side)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        # Winding, not the odd-even default: each handle rect straddles the
        # box edge, and under odd-even the overlap counts as OUTSIDE - which
        # punched a hole through the inner half of every grab-point and made
        # corners fail hit testing exactly where they look most clickable.
        path.setFillRule(Qt.WindingFill)
        path.addRect(self._rect)
        for r in self._handle_rects(active_only=True).values():
            path.addRect(r)
        return path

    def detach(self):
        """
        Tear the label chip down before this box is dropped.

        The chip is a child item, so C++ deletes it with its parent - but the
        Python wrapper in self._tag would outlive that and leave a dangling
        reference for the next paint or garbage collection to trip over, which
        aborts the interpreter rather than raising. Unparent it first.
        """
        tag, self._tag = self._tag, None
        if tag is None:
            return
        try:
            tag.setParentItem(None)
            scene = tag.scene()
            if scene is not None:
                scene.removeItem(tag)
        except RuntimeError:
            pass                      # already gone with a scene.clear()

    def set_color(self, color: QColor):
        self._color = QColor(color)
        self.update()

    def nudge(self, dx: float, dy: float):
        """Move by a pixel delta, clamped — used by Alt+Arrow keys."""
        self.setPos(self.pos() + QPointF(dx, dy))
        self.geometry_committed.emit(self.box_id)

    # ---- Handles --------------------------------------------------------

    CORNERS = (0, 2, 5, 7)          # TL, TR, BL, BR

    def _handle_rects(self, active_only: bool = False) -> Dict[int, QRectF]:
        """
        The eight grab-points, sized in screen pixels.

        The four corners are always present - they are what "see all four
        points of the box" means, and they stay draggable without selecting the
        box first. The mid-edge handles appear once the box is selected, so an
        unselected box is not covered in chrome.
        """
        r = self._rect
        k = self.view_k()
        corner = CHROME.CORNER_PX * CHROME.ui_scale * k
        edge   = CHROME.EDGE_PX   * CHROME.ui_scale * k
        cx, cy = r.center().x(), r.center().y()

        def sq(x: float, y: float, size: float) -> QRectF:
            h = size / 2.0
            return QRectF(x - h, y - h, size, size)

        rects = {
            self.TL: sq(r.left(),  r.top(),    corner),
            self.TR: sq(r.right(), r.top(),    corner),
            self.BL: sq(r.left(),  r.bottom(), corner),
            self.BR: sq(r.right(), r.bottom(), corner),
        }
        if self.isSelected() or not active_only:
            rects.update({
                self.TM: sq(cx,        r.top(),    edge),
                self.BM: sq(cx,        r.bottom(), edge),
                self.ML: sq(r.left(),  cy,         edge),
                self.MR: sq(r.right(), cy,         edge),
            })
        return rects

    def _handle_at(self, pos: QPointF) -> Optional[int]:
        """Corners respond always; mid-edges only on a selected box."""
        for hid, hr in self._handle_rects(active_only=True).items():
            if hr.contains(pos):
                return hid
        return None

    def _tag_height(self, k: float) -> float:
        """Scene-space room to reserve above the box for the screen-space tag."""
        if not self.label:
            return 0.0
        return (14.0 * CHROME.ui_scale + CHROME.TAG_PAD_PX * 2) * k

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
        """
        Draw the box so it reads over any photograph.

        Every stroke goes down twice: a dark halo first, the label colour on
        top. That is what makes the outline survive both a white banknote and a
        dark background - a single pastel stroke (the old behaviour) disappeared
        into either one. All widths are screen pixels divided by the zoom, so
        the outline never thins out or turns into a slab.
        """
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, False)

        k        = self.view_k()
        selected = self.isSelected()
        color    = QColor(self._color)

        pen_w  = CHROME.PEN_PX  * CHROME.ui_scale * k
        halo_w = CHROME.HALO_PX * CHROME.ui_scale * k
        if selected:
            pen_w  *= 1.5
            halo_w *= 1.4

        # Interior tint: light enough to keep the object readable underneath.
        fill = QColor(color)
        fill.setAlpha(48 if selected else 30)
        painter.setBrush(QBrush(fill))
        painter.setPen(Qt.NoPen)
        painter.drawRect(self._rect)

        # Halo, then the bright stroke over it.
        painter.setBrush(Qt.NoBrush)
        halo = QColor(CHROME.halo)
        halo.setAlpha(170)
        painter.setPen(QPen(halo, halo_w, Qt.SolidLine, Qt.SquareCap, Qt.MiterJoin))
        painter.drawRect(self._rect)
        painter.setPen(QPen(color, pen_w, Qt.SolidLine, Qt.SquareCap, Qt.MiterJoin))
        painter.drawRect(self._rect)

        # A dashed inner line marks the selected box without hiding the edge.
        if selected:
            inset = pen_w
            painter.setPen(QPen(QColor(CHROME.handle), pen_w * 0.6, Qt.DashLine))
            painter.drawRect(self._rect.adjusted(inset, inset, -inset, -inset))

        self._sync_tag(color)
        self._paint_handles(painter, k)
        painter.restore()

    def _sync_tag(self, color: QColor):
        """Keep the screen-space label chip on the box's top-left corner."""
        if self._tag is None:
            return
        try:
            self._tag.set_tag(self.label, color)
            self._tag.setPos(self._rect.topLeft())
        except RuntimeError:
            self._tag = None          # detached underneath us; stop drawing it

    def _paint_handles(self, painter: QPainter, k: float):
        """Grab-points: a rimmed square so they show on any background."""
        rim = QPen(QColor(CHROME.rim), max(1.0 * k, 0.5))
        painter.setPen(rim)
        corners = self._handle_rects(active_only=True)
        for hid, hr in corners.items():
            # Corners carry the label colour so the box they belong to is
            # obvious when several overlap; mid-edges stay neutral.
            painter.setBrush(QBrush(QColor(CHROME.handle)
                                    if hid not in self.CORNERS else self._color))
            painter.drawRect(hr)
            if hid in self.CORNERS:
                inner = hr.adjusted(hr.width() * 0.3, hr.height() * 0.3,
                                    -hr.width() * 0.3, -hr.height() * 0.3)
                painter.setBrush(QBrush(QColor(CHROME.handle)))
                painter.setPen(Qt.NoPen)
                painter.drawRect(inner)
                painter.setPen(rim)


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
        self._max_boxes = 0          # 0 = unlimited
        self._label_choices: List[str] = []

        # Label entry behaviour
        self._recent_labels: List[str] = []
        self._sticky_label = ""
        self._sticky_enabled = False

        # Drawing state
        self._draw_start:   Optional[QPointF] = None
        self._preview_item: Optional[QGraphicsRectItem] = None
        self._preview_halo: Optional[QGraphicsRectItem] = None
        # Box chrome is sized in screen pixels, so it must be re-measured
        # whenever the zoom changes or handles drift from the corners.
        self.zoom_changed.connect(lambda *_: self._refresh_box_chrome())
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
        """0 (or less) means no limit - annotation allows many labelled boxes."""
        try:
            self._max_boxes = max(0, int(n))
        except (TypeError, ValueError):
            self._max_boxes = 0

    def at_box_limit(self) -> bool:
        return self._max_boxes > 0 and len(self._boxes) >= self._max_boxes

    def apply_theme(self, mode: str, scale: float = 1.0):
        """
        Re-colour the box chrome and every live box for a new theme/scale.

        The order matters. Chrome sizes come from CHROME.ui_scale, so changing
        it silently changes what every BoxItem.boundingRect() returns.
        QGraphicsScene keeps a spatial index keyed on those rectangles and only
        refreshes it when prepareGeometryChange() is called BEFORE the geometry
        moves - calling set_chrome() first left the index holding rectangles
        that no longer matched their items, and the stale entries brought the
        process down when those items were later removed.
        """
        for box in self._boxes.values():
            box.prepareGeometryChange()          # while the old scale still holds

        set_chrome(mode, scale)

        self.setBackgroundBrush(QBrush(QColor(theme.color(mode, "canvas"))))
        self._label_colors.clear()
        for box in self._boxes.values():
            box.set_color(self._label_color(box.label))
        self._refresh_box_chrome()
        self.viewport().update()

    def _refresh_box_chrome(self):
        """Boxes size their chrome from the zoom, so re-measure after one."""
        for box in self._boxes.values():
            box.refresh_chrome()

    # ---- Zoom: warn the scene index before the geometry moves -----------
    #
    # Box chrome is specified in screen pixels, so every BoxItem.boundingRect()
    # depends on the view transform. Changing that transform therefore changes
    # their bounding rectangles - and QGraphicsScene's spatial index has to be
    # told BEFORE it happens, not after, or it keeps entries that no longer
    # match their items. Stale entries abort the process when the item is later
    # removed. Every zoom path goes through one of these three.

    def _warn_geometry_change(self):
        for box in self._boxes.values():
            try:
                box.prepareGeometryChange()
            except RuntimeError:
                pass

    def scale(self, sx, sy):
        self._warn_geometry_change()
        super().scale(sx, sy)
        self._refresh_box_chrome()

    def setTransform(self, matrix, combine=False):
        self._warn_geometry_change()
        super().setTransform(matrix, combine)
        self._refresh_box_chrome()

    def resetTransform(self):
        self._warn_geometry_change()
        super().resetTransform()
        self._refresh_box_chrome()

    def _next_box_color(self) -> QColor:
        """The colour the box currently being dragged out will receive."""
        sticky = getattr(self, "_sticky_label", "") or ""
        if sticky:
            return self._label_color(sticky)
        return LABEL_COLORS[len(self._boxes) % len(LABEL_COLORS)]

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

    def set_label_choices(self, labels: List[str]):
        """
        The managed label list from the config.

        These lead the quick-pick so annotators reach for the project's agreed
        labels first; anything already used on screen is still offered after.
        """
        self._label_choices = [str(l) for l in labels if str(l).strip()]
        for index, label in enumerate(self._label_choices):
            self._label_color(label)        # stable colour per label, in order

    def existing_labels(self) -> List[str]:
        seen = list(getattr(self, "_label_choices", []))
        for lbl in self._recent_labels:
            if lbl and lbl not in seen:
                seen.append(lbl)
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
                box.detach()
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
                item.detach()
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
                box.detach()
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
            if self.at_box_limit():
                self._show_toast(
                    f"Max {self._max_boxes} bounding box(es) per image."
                    "  Delete one first (Del)."
                )
                event.accept()
                return
            self._draw_start = scene_pos
            k = 1.0 / max(self.transform().m11(), 1e-9)
            accent = QColor(self._next_box_color())
            # Halo under the preview too: dragging a box out over a bright
            # banknote used to leave an almost invisible pale-blue outline.
            self._preview_halo = QGraphicsRectItem(QRectF(scene_pos, scene_pos))
            halo = QColor(CHROME.halo)
            halo.setAlpha(170)
            self._preview_halo.setPen(
                QPen(halo, CHROME.HALO_PX * CHROME.ui_scale * k,
                     Qt.SolidLine, Qt.SquareCap, Qt.MiterJoin))
            # QPainter takes the Qt.NoBrush enum; a QGraphicsItem does not.
            self._preview_halo.setBrush(QBrush(Qt.NoBrush))
            self._preview_halo.setZValue(98)
            self._scene.addItem(self._preview_halo)

            self._preview_item = QGraphicsRectItem(QRectF(scene_pos, scene_pos))
            self._preview_item.setPen(
                QPen(accent, CHROME.PEN_PX * CHROME.ui_scale * k, Qt.DashLine))
            fill = QColor(accent)
            fill.setAlpha(40)
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
                if self._preview_halo is not None:
                    self._scene.removeItem(self._preview_halo)
            except RuntimeError:
                pass
            self._preview_item = None
            self._preview_halo = None
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
                    if self._preview_halo is not None:
                        self._scene.removeItem(self._preview_halo)
                except RuntimeError:
                    pass
                self._preview_item = None
                self._preview_halo = None
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
