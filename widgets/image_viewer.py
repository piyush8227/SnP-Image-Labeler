"""
SnP Image Labeler - Image Viewer

Loading is asynchronous: the file is decoded on a worker thread and the
neighbours of the current image are prefetched, so stepping through a queue
of 12 MP photos no longer blocks the GUI thread for ~70 ms per image.

Also here:
  - byte-budgeted LRU pixmap cache (see core.image_cache)
  - zoom/pan lock, so a window resize or the next image does not throw away
    the zoom the user set up
  - space-bar and middle-mouse panning, which work in every mode
"""

from typing import List, Optional

from PyQt5.QtCore import QPoint, QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QBrush, QColor, QPainter, QPixmap, QTransform
from PyQt5.QtWidgets import (
    QFrame, QGraphicsPixmapItem, QGraphicsScene, QGraphicsView, QLabel,
)

from core.image_cache import AsyncImageLoader, PixmapCache


class ImageViewer(QGraphicsView):
    """Image viewer with async loading, zoom and pan."""

    zoom_changed = pyqtSignal(float)
    image_loaded = pyqtSignal(str, QSize)      # path, original pixel size
    image_failed = pyqtSignal(str)
    load_started = pyqtSignal(str)

    MIN_ZOOM  = 0.05
    MAX_ZOOM  = 20.0
    ZOOM_STEP = 1.25

    def __init__(self, parent=None, cache_budget_mb: int = 384):
        super().__init__(parent)

        self._scene = QGraphicsScene(self)
        self._scene.setBackgroundBrush(QBrush(QColor(30, 30, 46)))
        self.setScene(self._scene)

        self._pixmap_item: Optional[QGraphicsPixmapItem] = None
        self._current_zoom  = 1.0
        self._current_path: Optional[str] = None      # requested
        self._loaded_path:  Optional[str] = None      # actually on screen
        self._user_zoomed   = False
        self._zoom_locked   = False
        self._locked_transform: Optional[QTransform] = None
        self._locked_center:    Optional[QPointF]    = None

        # Panning
        self._space_pan   = False
        self._mid_pan     = False
        self._mid_anchor  = QPoint()
        self._saved_drag  = QGraphicsView.ScrollHandDrag

        # Async loading
        self._cache  = PixmapCache(cache_budget_mb)
        self._loader = AsyncImageLoader(self._cache, self)
        self._loader.image_ready.connect(self._on_image_ready)
        self._loader.image_failed.connect(self._on_image_failed)

        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setFrameShape(QFrame.NoFrame)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setOptimizationFlags(
            QGraphicsView.DontAdjustForAntialiasing | QGraphicsView.DontSavePainterState
        )
        self.setViewportUpdateMode(QGraphicsView.SmartViewportUpdate)

        # "Loading…" hint — only shown when a decode outlives the grace period
        self._hint = QLabel("", self)
        self._hint.setObjectName("viewer_hint")
        self._hint.setAlignment(Qt.AlignCenter)
        self._hint.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._hint.hide()

        self._hint_timer = QTimer(self)
        self._hint_timer.setSingleShot(True)
        self._hint_timer.timeout.connect(self._show_loading_hint)

    # ------------------------------------------------------------------ #
    #  Load / clear                                                        #
    # ------------------------------------------------------------------ #

    def load_image(self, path: str) -> bool:
        """
        Request an image. True means the request was accepted — the pixmap
        may arrive now (cache hit) or shortly, via image_loaded.
        """
        if not path:
            return False

        self._remember_view_state()
        self._current_path = path

        pixmap = self._loader.request(path)
        if pixmap is not None:
            self._apply_pixmap(path, pixmap, self._loader.original_size(path))
            return True

        self.load_started.emit(path)
        self._hint_timer.start(120)          # no flicker on fast decodes
        return True

    def prefetch(self, paths: List[str]) -> None:
        self._loader.prefetch([p for p in paths if p])

    def is_cached(self, path: str) -> bool:
        """True when this image can be shown without waiting for a decode."""
        return bool(path) and self._cache.contains(path)

    def clear(self):
        self._hint_timer.stop()
        self._hint.hide()
        self._current_path = None
        self._loaded_path  = None
        self._pixmap_item  = None
        self._scene.clear()
        self._on_scene_cleared()

    def invalidate_cache(self, path: str):
        self._loader.invalidate(path)

    def release_memory(self):
        """Drop every cached pixmap (queue cleared, dataset changed, close)."""
        self._loader.clear()

    def shutdown(self):
        self._hint_timer.stop()
        self._loader.shutdown()

    # ------------------------------------------------------------------ #
    #  Async plumbing                                                      #
    # ------------------------------------------------------------------ #

    def _on_image_ready(self, path: str, pixmap: QPixmap, size: QSize):
        if path != self._current_path:
            return                                   # user moved on already
        self._apply_pixmap(path, pixmap, size)

    def _on_image_failed(self, path: str):
        if path != self._current_path:
            return
        self._hint_timer.stop()
        self._hint.hide()
        self._pixmap_item = None
        self._scene.clear()
        self._on_scene_cleared()
        self.image_failed.emit(path)

    def _apply_pixmap(self, path: str, pixmap: QPixmap, size: Optional[QSize]):
        self._hint_timer.stop()
        self._hint.hide()

        # Null the item BEFORE clear(): resizeEvent must never touch a
        # deleted C++ object between clear() and addPixmap().
        self._pixmap_item = None
        self._scene.clear()
        self._on_scene_cleared()

        self._pixmap_item = self._scene.addPixmap(pixmap)
        self._pixmap_item.setTransformationMode(Qt.SmoothTransformation)
        self._scene.setSceneRect(QRectF(pixmap.rect()))
        self._loaded_path = path

        if self._zoom_locked and self._locked_transform is not None:
            self.setTransform(self._locked_transform)
            self._current_zoom = self.transform().m11()
            if self._locked_center is not None:
                self.centerOn(self._locked_center)
            self.zoom_changed.emit(self._current_zoom)
        else:
            self._user_zoomed = False
            self.resetTransform()
            self.fit_in_view()

        self.image_loaded.emit(path, size if size and size.isValid() else pixmap.size())

    def _on_scene_cleared(self):
        """Hook for subclasses that keep their own items in the scene."""

    def _show_loading_hint(self):
        if self._current_path and self._current_path != self._loaded_path:
            self._hint.setText("Loading…")
            self._hint.adjustSize()
            self._hint.resize(self._hint.width() + 28, self._hint.height() + 14)
            self._position_hint()
            self._hint.show()
            self._hint.raise_()

    def _position_hint(self):
        self._hint.move(
            max(0, (self.width()  - self._hint.width())  // 2),
            max(0, (self.height() - self._hint.height()) // 2),
        )

    def _remember_view_state(self):
        if self._zoom_locked and self._pixmap_item is not None:
            self._locked_transform = QTransform(self.transform())
            self._locked_center = self.mapToScene(
                self.viewport().rect().center()
            )

    # ------------------------------------------------------------------ #
    #  Zoom                                                                #
    # ------------------------------------------------------------------ #

    def zoom_in(self):
        self._apply_zoom(self.ZOOM_STEP)

    def zoom_out(self):
        self._apply_zoom(1.0 / self.ZOOM_STEP)

    def zoom_reset(self):
        """100% — one image pixel per screen pixel."""
        if self._pixmap_item is None:
            return
        self.resetTransform()
        self._current_zoom = 1.0
        self._user_zoomed = True
        self.zoom_changed.emit(self._current_zoom)

    def fit_in_view(self):
        if self._pixmap_item:
            self.fitInView(self._pixmap_item, Qt.KeepAspectRatio)
            self._current_zoom = self.transform().m11()
            self._user_zoomed = False
            self.zoom_changed.emit(self._current_zoom)

    def _apply_zoom(self, factor: float):
        new_zoom = self._current_zoom * factor
        if self.MIN_ZOOM <= new_zoom <= self.MAX_ZOOM:
            self.scale(factor, factor)
            self._current_zoom = new_zoom
            self._user_zoomed = True
            self.zoom_changed.emit(self._current_zoom)

    def set_zoom_locked(self, locked: bool):
        """Keep the current zoom/pan when moving to the next image."""
        self._zoom_locked = bool(locked)
        if self._zoom_locked:
            self._remember_view_state()
        else:
            self._locked_transform = None
            self._locked_center = None

    @property
    def zoom_locked(self) -> bool:
        return self._zoom_locked

    # ------------------------------------------------------------------ #
    #  Panning helpers (shared by both viewer modes)                       #
    # ------------------------------------------------------------------ #

    @property
    def panning(self) -> bool:
        """True while a space-bar or middle-mouse pan is in progress."""
        return self._space_pan or self._mid_pan

    def _begin_space_pan(self):
        if self._space_pan:
            return
        self._space_pan  = True
        self._saved_drag = self.dragMode()
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.viewport().setCursor(Qt.OpenHandCursor)

    def _end_space_pan(self):
        if not self._space_pan:
            return
        self._space_pan = False
        self.setDragMode(self._saved_drag)
        self.viewport().unsetCursor()

    def _pan_by(self, delta: QPoint):
        h = self.horizontalScrollBar()
        v = self.verticalScrollBar()
        h.setValue(h.value() - delta.x())
        v.setValue(v.value() - delta.y())

    # ------------------------------------------------------------------ #
    #  Events                                                              #
    # ------------------------------------------------------------------ #

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            if event.angleDelta().y() > 0:
                self.zoom_in()
            else:
                self.zoom_out()
            event.accept()
        else:
            super().wheelEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self._mid_pan    = True
            self._mid_anchor = event.pos()
            self.viewport().setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._mid_pan:
            self._pan_by(event.pos() - self._mid_anchor)
            self._mid_anchor = event.pos()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._mid_pan and event.button() == Qt.MiddleButton:
            self._mid_pan = False
            self.viewport().unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Space and not event.isAutoRepeat():
            self._begin_space_pan()
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key_Space and not event.isAutoRepeat():
            self._end_space_pan()
            event.accept()
            return
        super().keyReleaseEvent(event)

    def focusOutEvent(self, event):
        self._end_space_pan()                 # never leave the view stuck panning
        super().focusOutEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_hint()
        # Refit only when the user has not chosen their own zoom. Resizing a
        # window must not throw away a zoom someone set up deliberately.
        if self._pixmap_item and not self._user_zoomed and not self._zoom_locked:
            self.fit_in_view()

    # ------------------------------------------------------------------ #
    #  Properties                                                          #
    # ------------------------------------------------------------------ #

    @property
    def current_zoom(self) -> float:
        return self._current_zoom

    @property
    def current_path(self) -> Optional[str]:
        return self._current_path

    @property
    def loaded_path(self) -> Optional[str]:
        return self._loaded_path

    @property
    def has_image(self) -> bool:
        return self._pixmap_item is not None

    def cache_stats(self) -> str:
        return f"{self._cache.count} images / {self._cache.megabytes:.0f} MB"
