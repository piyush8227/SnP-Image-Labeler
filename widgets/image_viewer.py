"""
SnP Image Labeler - Image Viewer
"""

from PyQt5.QtWidgets import QGraphicsView, QGraphicsScene, QGraphicsPixmapItem, QFrame
from PyQt5.QtCore import Qt, QRectF, pyqtSignal
from PyQt5.QtGui import QPixmap, QColor, QBrush, QPainter

# Thread-safe in-process pixmap cache (QPixmap lives only in main thread)
_CACHE: dict = {}
_CACHE_MAX = 50


class ImageViewer(QGraphicsView):
    """Zero-latency image viewer with zoom and pan."""

    zoom_changed = pyqtSignal(float)

    MIN_ZOOM = 0.05
    MAX_ZOOM = 20.0
    ZOOM_STEP = 1.25

    def __init__(self, parent=None):
        super().__init__(parent)

        self._scene = QGraphicsScene(self)
        self._scene.setBackgroundBrush(QBrush(QColor(30, 30, 46)))
        self.setScene(self._scene)

        self._pixmap_item = None   # always None or valid QGraphicsPixmapItem
        self._current_zoom = 1.0
        self._current_path = None

        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setFrameShape(QFrame.NoFrame)
        self.setOptimizationFlags(
            QGraphicsView.DontAdjustForAntialiasing | QGraphicsView.DontSavePainterState
        )
        self.setViewportUpdateMode(QGraphicsView.SmartViewportUpdate)

    # ------------------------------------------------------------------ #
    #  Cache                                                               #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _cache_get(path):
        return _CACHE.get(path)

    @staticmethod
    def _cache_set(path, pixmap):
        if len(_CACHE) >= _CACHE_MAX:
            try:
                _CACHE.pop(next(iter(_CACHE)))
            except StopIteration:
                pass
        _CACHE[path] = pixmap

    @staticmethod
    def _cache_remove(path):
        _CACHE.pop(path, None)

    # ------------------------------------------------------------------ #
    #  Load / Clear                                                        #
    # ------------------------------------------------------------------ #

    def load_image(self, path: str) -> bool:
        """Load image from path. Returns True on success."""
        if not path:
            return False

        self._current_path = path

        pixmap = self._cache_get(path)
        if pixmap is None:
            pixmap = QPixmap(path)
            if pixmap.isNull():
                return False
            self._cache_set(path, pixmap)

        # BUG FIX: null out _pixmap_item BEFORE scene.clear()
        # so resizeEvent guard 'if self._pixmap_item' cannot access
        # a deleted C++ object between clear() and addPixmap().
        self._pixmap_item = None
        self._scene.clear()

        self._pixmap_item = self._scene.addPixmap(pixmap)
        self._pixmap_item.setTransformationMode(Qt.SmoothTransformation)
        self._scene.setSceneRect(QRectF(pixmap.rect()))
        self._current_zoom = 1.0
        self.resetTransform()
        self.fit_in_view()
        return True

    def clear(self):
        # BUG FIX: null out _pixmap_item before scene.clear()
        self._pixmap_item = None
        self._scene.clear()
        self._current_path = None

    def invalidate_cache(self, path: str):
        self._cache_remove(path)

    # ------------------------------------------------------------------ #
    #  Zoom                                                                #
    # ------------------------------------------------------------------ #

    def zoom_in(self):
        self._apply_zoom(self.ZOOM_STEP)

    def zoom_out(self):
        self._apply_zoom(1.0 / self.ZOOM_STEP)

    def fit_in_view(self):
        if self._pixmap_item:
            self.fitInView(self._pixmap_item, Qt.KeepAspectRatio)
            self._current_zoom = self.transform().m11()
            self.zoom_changed.emit(self._current_zoom)

    def _apply_zoom(self, factor: float):
        new_zoom = self._current_zoom * factor
        if self.MIN_ZOOM <= new_zoom <= self.MAX_ZOOM:
            self.scale(factor, factor)
            self._current_zoom = new_zoom
            self.zoom_changed.emit(self._current_zoom)

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

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Safe: _pixmap_item is None during scene.clear(), never dangling
        if self._pixmap_item:
            self.fit_in_view()

    # ------------------------------------------------------------------ #
    #  Properties                                                          #
    # ------------------------------------------------------------------ #

    @property
    def current_zoom(self):
        return self._current_zoom

    @property
    def current_path(self):
        return self._current_path
